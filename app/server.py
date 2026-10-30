"""SWOT viewer: a small API over the IceChunk store, and the page in static/.
The swaths are drawn here, as map tiles: each passage is a curvilinear mesh rasterized with datashader,
so only images of the screen's size go to the browser.
Run: uvicorn server:app --app-dir app --port 5006   (SWOT_STORE, credentials from AWS_* in the environment)"""
import hashlib
import io
import itertools
import json
import os
import threading
import time
import tomllib
import traceback
import warnings
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from types import SimpleNamespace
from pathlib import Path

import datashader as dsh
import cmocean  # noqa: F401 (registers its colormaps as cmo.*)
import matplotlib
import numpy as np
import pandas as pd
import xarray as xr
import zarr
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from PIL import Image
from scipy.spatial import cKDTree

import rasters
import swot_view
from rasters import calibration  # noqa: F401 (the server's too)

matplotlib.use("agg")
# zarr fetches 10 chunks at once by default; S3 answers each in tens of ms, so more at once is faster
# (whole globe, 1 day: 15 s -> 5 s with 8 CPUs; scripts/bench_server.py)
zarr.config.set({"async.concurrency": 256})
MAX_DAYS = 21
# pixels in one view (passages x lines x pixels across): 60 M is ~2 days of the whole globe in L3, ~4 GB in memory
MAX_BYTES = float(os.environ.get("SWOT_CACHE_GB", 4)) * 1e9  # loaded views kept in memory, beyond the newest
class Variable:
    """What a map shows: the arrays summed (`names`), their quality flag (values kept where it is 0), in dB or not,
    its default colormap and kind ("anomaly": a spatial mean subtracted, colors symmetric; "level": as stored)."""

    def __init__(self, label, names, flag=None, db=False, cmap="RdBu_r", kind="anomaly", units="m", surface=None,
                 twin=None, mss=None, calibrated=False, cal=None):
        self.label, self.names, self.flag, self.db = label, list(names), flag, db
        self.cmap, self.kind, self.units = cmap, kind, units
        # the surface classes kept (SURFACE: 0 open ocean, 2 continental water...), and the variable that is the same
        # with another choice of them (the Display menu's "Rivers and lakes" switches between the two); `mss`: the
        # same with the other mean sea surface (the Display menu's "Mean sea surface"); `calibrated`: minus the
        # calibration (`calibration`), and `cal` the same with the other choice (the Display menu's "Calibration")
        self.surface, self.twin, self.mss, self.calibrated, self.cal = surface, twin, mss, calibrated, cal

    @property
    def dtu(self):
        """Whether it uses DTU25MSS (its credit is then shown)."""
        return DTU in self.names

    @property
    def arrays(self):
        """Every array the values need."""
        return (self.names + ([self.flag] if self.flag else []) + ([SURFACE] if self.surface else [])
                + (list(CAL) if self.calibrated else []))

    def values(self, get):
        """The values, from `get(name)` -> each array: summed, minus the calibration if so (`get("cross")`: the pixels'
        distance to nadir), NaN where the flag is not 0 or the surface is not one kept, in dB if so."""
        v = masked(sum(get(n) for n in self.names), get(self.flag) if self.flag else None)
        if self.calibrated:
            v = v - calibration(get(CAL[0]), get(CAL[1]), get("cross")).reshape(v.shape)
        if self.surface:
            v = np.where(np.isin(get(SURFACE), self.surface), v, np.nan)
        if self.db:
            with np.errstate(divide="ignore", invalid="ignore"):
                v = np.where(v > 0, 10 * np.log10(v), np.nan)
        return v


# L2: values kept only where their quality flag is 0 (over land and ice SSHA reaches kilometres; until #3 lets the
# user choose how strict). Sigma0 is in dB; SWH and wind are only at 2 km.
# SSHA with the crossover correction, on water (swot_cloud: open ocean or continental water, no bad_* bit; NaN
# elsewhere): one array instead of ssha_karin_2, height_cor_xover, their flag and the surface classification
# (its rivers and lakes are metres above the mean sea surface: the ocean only by default, they on request)
SURFACE = "ancillary_surface_classification_flag"  # Expert: one per pass (pass_number), Unsmoothed: per passage
# by default the mean sea surface is DTU25MSS (swot_cloud: the correction from the L2's CNES_CLS22 to it, one per pass
# in Expert; it removes geoid errors at 15-70 km, ~20 % of SSHA's power there); Unsmoothed has no MSS: the
# correction comes from Expert, interpolated at its pixels (`from_base`)
DTU = "mss_cor_dtu25"
# by default also calibrated (swot_cloud, experimental v0.1: Expert's cal_bias and cal_slope, a bias and a cross-track
# slope per line and side; Unsmoothed takes Expert's, `from_base`)
CAL = ("cal_bias", "cal_slope")


def ssha_l2():
    """SSHA with the crossover correction, by each choice of mean sea surface (DTU25 or the L2's), calibration and
    surfaces (the open ocean, or rivers and lakes too): the defaults first; each a twin of the others by one choice."""
    out = {}
    for dtu, cal, water in itertools.product((True, False), (True, False), (False, True)):
        key = lambda d, c, w: "ssha_" + ("dtu25" if d else "karin_2_xover") + ("_cal" if c else "") + ("_water" if w else "")  # noqa: E731
        label = ("SSHA (KaRIn 2) + crossover correction" + ("" if dtu else ", L2 MSS") + (", calibrated" if cal else "")
                 + (", rivers and lakes too" if water else ""))
        out[key(dtu, cal, water)] = Variable(label, ["ssha_karin_2_xover"] + ([DTU] if dtu else []),
                                             surface=None if water else (0,), twin=key(dtu, cal, not water),
                                             mss=key(not dtu, cal, water), calibrated=cal, cal=key(dtu, not cal, water))
    return out


SSHA_L2 = ssha_l2() | {
    # its flag alone keeps nearly all the land in Unsmoothed: the open ocean only, as the default
    "ssha_karin_2": Variable("SSHA (KaRIn 2), no correction", ["ssha_karin_2"], "ssha_karin_2_qual", surface=(0,))}
SIGMA0 = Variable("Sigma0 (KaRIn 2)", ["sig0_karin_2"], "sig0_karin_2_qual", db=True, cmap="cmo.gray", kind="level",
                  units="dB")
# the product the store holds (SWOT_PRODUCT): its name, variables and opening view
PRODUCTS = {
    "l3": dict(label="L3 LR SSH v3.0", credit="AVISO/DUACS, CNES", pixel=2.0, variables={
        "ssha_unfiltered": Variable("SSHA, unfiltered", ["ssha_unfiltered"]),
        "ssha_filtered": Variable("SSHA, filtered", ["ssha_filtered"]),
        "adt": Variable("ADT = SSHA + MDT", ["ssha_filtered", "mdt"])},
        # the whole globe on one day (kept in memory)
        default=dict(west=-180, east=180, south=-90, north=90, start="2024-11-02", days=1)),
    # the L2 product on the same 2 km grid as L3 (the whole mission, with envelopes)
    "expert": dict(label="L2 LR SSH Expert, 2 km", credit="NASA/JPL, CNES", pixel=2.0, max_pixels=250e6, variables=SSHA_L2 | {
        "sig0_karin_2": SIGMA0,
        "swh_karin": Variable("Significant wave height (KaRIn)", ["swh_karin"], "swh_karin_qual", cmap="cmo.dense",
                              kind="level"),
        "wind_speed_karin_2": Variable("Wind speed (KaRIn 2)", ["wind_speed_karin_2"], "wind_speed_karin_2_qual",
                                       cmap="cmo.speed", kind="level", units="m/s")},
        default="latest", overview=True),  # the whole globe on the latest 21 days, as an overview
    # 250 m, 64 times the pixels of L3: small regions only; being uploaded, so the passages with data only
    "unsmoothed": dict(label="L2 LR SSH Unsmoothed, 250 m", credit="NASA/JPL, CNES", pixel=0.25, partial=True,
                       variables=SSHA_L2 | {"sig0_karin_2": SIGMA0},
                       default=None),  # the latest day with data, around a passage (below)
}
# the colormaps a map can take (any variable, any of them), diverging first (for anomalies), then sequential
COLORMAPS = {name: matplotlib.colormaps[name] for name in (
    "RdBu_r", "cmo.balance", "cmo.curl",
    "cmo.gray", "gray", "cmo.dense", "cmo.amp", "magma", "cmo.deep_r", "cmo.speed", "viridis", "cmo.tempo", "cmo.haline",
    "cividis", "cmo.turbid")}
DIVERGING = 3
# without configuration: L2 Expert, with L2 Unsmoothed from a zoom on (detail, below); a store named (SWOT_STORE)
# is L3 unless SWOT_PRODUCT says otherwise
L2 = "arraylake:computational-oceanography-udel/swot-l2-lr-ssh-"
CONFIGURED = "SWOT_STORE" in os.environ
PRODUCT = PRODUCTS[os.environ.get("SWOT_PRODUCT", "l3" if CONFIGURED else "expert")]
MAX_PIXELS = float(os.environ.get("SWOT_MAX_PIXELS", PRODUCT.get("max_pixels", 60e6)))
VARIABLES = PRODUCT["variables"]


SCREEN_SAMPLE = 1_000_000  # pixels kept with their positions, for the colors of what is on screen


def screen_limits(screen, shift, west, east, south, north):
    """The robust limits of each mode from the pixels of a sample (lon, lat, value, passage) inside a box; None with
    too few there."""
    lon, lat, v, at = screen
    width = (east - west) % 360 or 360
    inside = ((lon - west) % 360 <= width) & (lat >= south) & (lat <= north)
    if inside.sum() < 500:
        return None
    return {mode: color_limits((v[inside] - s[at[inside]]).astype("float64"))[1] for mode, s in shift.items()}


def color_limits(v):
    """The colors' limits for values: their quantiles (0 to 100 % by 0.1) and a robust range, the median ± 2 x 1.4826
    x MAD (two standard deviations for normal values): in coastal scenes rivers and lakes push the 2-98 % percentiles
    to metres and the ocean fades, not this one."""
    if not len(v):
        return [], []
    median = np.median(v)
    spread = 2 * 1.4826 * np.median(np.abs(v - median))
    return (np.quantile(v, np.linspace(0, 1, 1001)).round(4).tolist(),
            [round(float(median - spread), 4), round(float(median + spread), 4)])


def masked(values, flag):
    """Values where the quality flag is 0 (good), NaN elsewhere; unchanged without a flag."""
    return values if flag is None else np.where(flag == 0, values, np.nan)
MODES = ("box", "passage", "none")  # spatial mean subtracted: of the box, of each passage, nothing
R = 6378137.0  # web mercator
WORLD = 2 * np.pi * R

def open_product(target, partial=False, geometry=None):
    """A store at its latest commit (kept: a store being written changes while people look): the dataset, when each
    passage was (`index`) and the passes' envelopes (`geometry`, which passes cross a box without reading data; kept
    in memory, a view would read them all again). A store being filled (`partial`) shows only the passages with data,
    and envelopes not written yet come from each pass's nadir and the swath's half width. A product over the same
    passes can take another's `geometry` (the Unsmoothed one, a line every 250 m, is 400 MB)."""
    repo = swot_view.open_repo(target)
    commit = next(iter(repo.ancestry(branch="main")))
    session = repo.readonly_session(snapshot_id=commit.id)
    ds = swot_view.dataset(session.store)
    if partial:
        ds = ds.isel(time=swot_view.written(ds, session.store))
    if partial and geometry is None:
        have = np.isfinite(ds.lon_min.values).any(1, keepdims=True)
        missing = np.setdiff1d(np.unique(ds.pass_id.values), ds.pass_number.values[have[:, 0]])
        print(f"{target}: {ds.sizes['time']} passages with data, {len(missing)} passes without envelopes", flush=True)
        if len(missing):
            lon_min, lon_max = swot_view.nadir_envelope(ds, passes=missing)
            ds = ds.assign_coords(lon_min=(ds.lon_min.dims, np.where(have, ds.lon_min.values, lon_min)),
                                  lon_max=(ds.lon_max.dims, np.where(have, ds.lon_max.values, lon_max)))
    index = ds[["cycle", "pass_id"]].reset_coords().load()
    if geometry is None:
        geometry = ds[["lon_min", "lon_max"]].load()
        ds = ds.assign_coords(lon_min=geometry.lon_min, lon_max=geometry.lon_max)
    ds = ds.assign_coords(cycle=index.cycle, pass_id=index.pass_id)
    return SimpleNamespace(ds=ds, index=index, geometry=geometry, commit=commit)


def latest_days(times):
    """The MAX_DAYS days up to the day of the latest passage (part of two cycles, often): their first day and number."""
    first = times.max().astype("datetime64[D]") - np.timedelta64(MAX_DAYS - 1, "D")
    return dict(start=str(first), days=MAX_DAYS)


STORE = os.environ.get("SWOT_STORE", L2 + "expert-d")
base = open_product(STORE, PRODUCT.get("partial"))
ds, index, geometry, COMMIT = base.ds, base.index, base.geometry, base.commit
BASE_ROWS = {(c, p): i for i, (c, p) in enumerate(zip(index.cycle.values.tolist(), index.pass_id.values.tolist()))}
# a finer product over the same passages, drawn over the map from a zoom on (SWOT_DETAIL_STORE: e.g. L2 Unsmoothed
# at 250 m over L2 Expert at 2 km); read tile by tile, a chunk at a time, never as a whole view
DETAIL_STORE = os.environ.get("SWOT_DETAIL_STORE", None if CONFIGURED else L2 + "unsmoothed-d")
DETAIL_PRODUCT = PRODUCTS[os.environ.get("SWOT_DETAIL_PRODUCT", "unsmoothed")]
DETAIL_ZOOM = int(os.environ.get("SWOT_DETAIL_ZOOM", 9))
detail = open_product(DETAIL_STORE, DETAIL_PRODUCT.get("partial"), geometry) if DETAIL_STORE else None
# the passages it has, (cycle, pass): under it, from its zoom on, the base is drawn inside its swath (see `inner`)
DETAIL_PASSAGES = frozenset(zip(detail.index.cycle.values.tolist(), detail.index.pass_id.values.tolist())) if detail else frozenset()
DEFAULT = PRODUCT["default"]
if DEFAULT is None:  # the latest passage's day, 6 x 6 degrees around its nadir at the latitude nearest the equator
    last = int(np.argmax(index.time.values))
    nadir = ds[["latitude_nadir", "longitude_nadir"]].isel(time=last).load()
    k = int(np.nanargmin(np.abs(nadir.latitude_nadir.values)))
    lat0, lon0 = float(nadir.latitude_nadir.values[k]), (float(nadir.longitude_nadir.values[k]) + 180) % 360 - 180
    DEFAULT = dict(west=round(lon0 - 3, 2), east=round(lon0 + 3, 2), south=round(lat0 - 3, 2), north=round(lat0 + 3, 2),
                   start=str(pd.Timestamp(index.time.values[last]).date()), days=1)
elif DEFAULT == "latest":  # the whole globe on the latest days
    DEFAULT = dict(west=-180, east=180, south=-90, north=90) | latest_days(index.time.values)
DEFAULT = DEFAULT | dict(variable=next(iter(VARIABLES)))
CACHE_DIR = os.environ.get("SWOT_CACHE_DIR")  # where overviews are kept between restarts


def mercator(lon, lat):
    return R * np.deg2rad(lon), R * np.log(np.tan(np.pi / 4 + np.deg2rad(lat) / 2))


def unit(lon, lat):
    """Points on the unit sphere, (..., 3)."""
    lon, lat = np.deg2rad(lon), np.deg2rad(lat)
    return np.stack([np.cos(lat) * np.cos(lon), np.cos(lat) * np.sin(lon), np.sin(lat)], axis=-1)


def along_line(lon, lat, step, most=5000):
    """Points every `step` km (or more, to keep at most `most`) along the great circles between the vertices:
    their lon, lat, distance from the first vertex (km), and the step used."""
    v = unit(lon, lat)
    angles = np.arccos(np.clip((v[:-1] * v[1:]).sum(-1), -1, 1))  # of each segment
    length = angles.sum() * swot_view.EARTH_RADIUS
    step = max(step, length / most)
    dist = np.r_[np.arange(0, length, step), length]  # and the last vertex
    starts = np.r_[0, np.cumsum(angles)]
    k = np.clip(np.searchsorted(starts, dist / swot_view.EARTH_RADIUS, side="right") - 1, 0, len(angles) - 1)
    a = angles[k]  # each point's segment, and how far along it (0 to 1)
    t = np.divide(dist / swot_view.EARTH_RADIUS - starts[k], a, out=np.zeros_like(dist), where=a > 0)
    sin_a = np.where(a > 0, np.sin(a), 1)
    w0 = np.where(a > 0, np.sin((1 - t) * a) / sin_a, 1)  # slerp; a zero-length segment stays at its vertex
    w1 = np.where(a > 0, np.sin(t * a) / sin_a, 0)
    x, y, z = (w0[:, None] * v[k] + w1[:, None] * v[k + 1]).T
    return np.rad2deg(np.arctan2(y, x)), np.rad2deg(np.arcsin(np.clip(z, -1, 1))), dist, step


def box_lons(west, east):
    """West and east of the box as continuous longitudes (east > west), also across 0/360 and 180."""
    west = (west + 180) % 360 - 180
    return west, west + ((east - west) % 360 or 360)


def continuous(lon, center):
    """Longitudes of one passage without jumps of 360 (along each line and along the track), placed
    nearest `center`: a swath crossing 180 stays one piece instead of quads spanning the world."""
    c = lon.shape[1] // 2
    nadir = np.unwrap(lon[:, c], period=360)
    lon = np.unwrap(lon, period=360, axis=1)
    lon = lon + (nadir - lon[:, c])[:, None]
    return lon + 360 * np.round((center - np.median(lon)) / 360)


def continuous_all(lon, center):
    """`continuous` for each passage of a (passages, lines, pixels) array (lines without positions stay NaN)."""
    out = np.full(lon.shape, np.nan)
    for i in range(len(lon)):
        ok = np.isfinite(lon[i]).all(1)
        if ok.sum() > 1:
            out[i][ok] = continuous(lon[i][ok], center)
    return out


def inner(v):
    """A passage's values (lines x cross) without the first and last cell of each run of data along every line: its
    swath's outer edges and the sides of the nadir gap. The 2 km and the 250 m span the same distances from nadir,
    but a 2 km cell is drawn 2 km wide, ~1 km past the 250 m's outer edge and ~1.5 km into its nadir gap."""
    ok = np.pad(np.isfinite(v), ((0, 0), (1, 1)))  # past a line's ends: no data
    return np.where(ok[:, 1:-1] & ok[:, :-2] & ok[:, 2:], v, np.nan)


def across(a):
    """Positions missing within a line (the nadir gap of L2 Unsmoothed, its edge pixels) interpolated across it from
    the nearest ones on each side: the grid is regular across, and a mesh needs every corner (the values there stay
    NaN). At a line's ends, the nearest position; lines without any stay NaN. Only the columns with something
    missing are worked on (with one known column on each side), vectorized over the lines. The step between the two
    sides is taken within -180..180, so a line crossing 180 is not interpolated around the world."""
    a = np.array(a, float)
    flat = a.reshape(-1, a.shape[-1])
    gaps = np.flatnonzero(np.isnan(flat).any(0))
    for run in np.split(gaps, np.flatnonzero(np.diff(gaps) > 1) + 1) if len(gaps) else []:
        c0, c1 = max(run[0] - 1, 0), min(run[-1] + 2, flat.shape[1])
        block = flat[:, c0:c1]
        ok = np.isfinite(block)
        n = block.shape[1]
        x = np.broadcast_to(np.arange(n), block.shape)
        left = np.maximum.accumulate(np.where(ok, x, -1), axis=1)  # the nearest position on each side
        right = np.minimum.accumulate(np.where(ok, x, n)[:, ::-1], axis=1)[:, ::-1]
        lo, hi = np.where(left < 0, right, left).clip(0, n - 1), np.where(right >= n, left, right).clip(0, n - 1)
        rows = np.arange(len(block))[:, None]
        with np.errstate(invalid="ignore"):
            a0, a1 = block[rows, lo], block[rows, hi]
            step = (a1 - a0 + 180) % 360 - 180
            frac = np.where(hi > lo, (x - lo) / np.maximum(hi - lo, 1), 0)
            flat[:, c0:c1] = np.where(ok, block, a0 + step * frac)
    return a


def coarse(a, k, mean=True):
    """A (passages, lines, pixels) array k x k times coarser: block means (NaN only where a whole block is), or for
    positions each block's centre pixel."""
    n, rows, cols = a.shape
    if not mean:
        return a[:, k // 2::k, k // 2::k][:, :rows // k, :cols // k]
    b = a[:, :rows // k * k, :cols // k * k].reshape(n, rows // k, k, cols // k, k)
    with warnings.catch_warnings(action="ignore"):
        return np.nanmean(b, axis=(2, 4)).astype(a.dtype)


def build_meshes(values, lon, lat, center):
    """Each run of consecutive lines with all corners (a mesh needs them) and some data: (passage, its lines, x, y,
    each line's lowest and highest y, the mesh's bounds). Joining runs across the lines between them (land, missing
    lines) would draw long thin quads over the gap."""
    meshes = []
    for i in range(len(values)):
        usable = np.isfinite(lon[i]).all(1) & np.isfinite(lat[i]).all(1) & np.isfinite(values[i]).any(1)
        edges = np.flatnonzero(np.diff(np.r_[0, usable.astype(int), 0]))
        for a, b in zip(edges[::2], edges[1::2]):
            if b - a < 2:  # one line has no cells
                continue
            rows = slice(a, b)
            xi, yi = mercator(continuous(lon[i][rows], center), lat[i][rows])  # float64: datashader draws float32
            # cells (or unevenly rounded ones) with their edges at the outer centres, half a cell short
            # lines run south to north: a view needs only the block of lines across its latitudes
            meshes.append((i, rows, xi, yi, yi.min(1), yi.max(1), (xi.min(), xi.max(), yi.min(), yi.max())))
    return meshes


class Loaded:
    """One subset in memory: the passages over a box in a time window, ready to draw. The values are kept once, as
    float32; the modes subtract one number (the box's median, or each passage's) when drawn. Where a screen pixel
    holds many of the product's cells (the whole globe at 2 km is hundreds of millions), coarser copies (4 x 4 and
    16 x 16 block means at their centres) are drawn as points, all passages in one go: as meshes, each passage's
    would be its own call, and a world tile would take seconds."""

    LEVELS = (1, 4, 16)

    def __init__(self, sub, var, west, east, line_time=None):
        var = var if isinstance(var, Variable) else Variable("", var)  # names of values already combined
        self.names = var.names
        self.line_time = line_time  # (time, along): when each line was measured
        values = var.values(lambda n: sub[n].values).astype("float32")
        # positions as float32 (~1 m), the arrays dropped: what the clicks and lines need
        self.sub = sub.drop_vars(var.arrays).assign_coords(latitude=sub.latitude.astype("float32"),
                                                           longitude=sub.longitude.astype("float32"))
        lon, lat = sub.longitude.values, sub.latitude.values
        w, e = box_lons(west, east)
        center = (w + e) / 2
        # what the modes subtract: medians (robust: a few L2 pixels that pass the quality flag are kilometres off,
        # and one of them moves a mean by decimetres), of the box (from a sample) and of each passage (every 7th
        # pixel of its part in the box)
        rng = np.random.default_rng(0)
        idx = rng.integers(0, max(values.size, 1), min(values.size, 4_000_000))
        in_box = lambda lo: (lo - w) % 360 <= e - w  # noqa: E731
        sample = values.ravel()[idx]
        inside = sample[in_box(lon.ravel()[idx]) & np.isfinite(sample)]
        keep = idx[:SCREEN_SAMPLE][np.isfinite(sample[:SCREEN_SAMPLE])]  # with positions: the colors of a screen
        self.screen = (((lon.ravel()[keep] + 180) % 360 - 180).astype("float32"), lat.ravel()[keep].astype("float32"),
                       values.ravel()[keep], (keep // max(int(np.prod(values.shape[1:])), 1)).astype("int32"))
        per = [np.where(in_box(lon[i].ravel()[::7]), values[i].ravel()[::7], np.nan) for i in range(len(values))]
        with warnings.catch_warnings(action="ignore"):  # passages without data in the box: NaN
            self.shift = {"none": np.zeros(len(values), "float32"),
                          "box": np.full(len(values), np.median(inside) if len(inside) else np.nan, "float32"),
                          "passage": np.array([np.nanmedian(v) for v in per], "float32")}
        self.raw = values
        lon, lat = across(lon), across(lat)  # meshes need every corner; the nadir gap's are interpolated
        self.levels = {1: (values, build_meshes(values, lon, lat, center))}
        self.bounds = np.array([m[6] for m in self.levels[1][1]]).reshape(-1, 4)  # a view finds its meshes at once
        self.points = {}  # level -> x, y, value and passage of each block with data
        for k in self.LEVELS[1:]:
            if min(values.shape[1:]) < 2 * k:
                continue
            v = coarse(values, k)
            x, y = mercator(continuous_all(coarse(lon, k, False), center), coarse(lat, k, False))
            ok = np.isfinite(v) & np.isfinite(x) & np.isfinite(y)
            order = np.argsort(y[ok])  # sorted by y: a tile takes its band of latitudes with a search
            self.points[k] = tuple(a[order] for a in (x[ok], y[ok], v[ok], np.nonzero(ok)[0].astype("int32")))
        # the product's spacing on the ground (median, m; mercator stretches it by 1 / cos(lat)): which level a
        # view needs, at the view's latitude
        widths = [np.nanmedian(np.hypot(np.diff(m[2], axis=1), np.diff(m[3], axis=1))
                               * np.cos(2 * np.arctan(np.exp(m[3] / R)) - np.pi / 2)[:, 1:]) for m in self.levels[1][1][:50]]
        self.spacing = float(np.nanmedian(widths)) if widths else 2000.0
        self.nbytes = (self.sub.nbytes + values.nbytes + sum(m[2].nbytes + m[3].nbytes for m in self.levels[1][1])
                       + sum(a.nbytes for p in self.points.values() for a in p))
        # colors: quantiles of each mode from the sample (sorting all would take most of a load)
        ok = np.isfinite(sample)
        passage = idx[ok] // max(values[0].size if len(values) else 1, 1)
        self.quantiles, self.robust = {}, {}
        for mode, shift in self.shift.items():
            v = sample[ok] - shift[passage]
            self.quantiles[mode], self.robust[mode] = color_limits(v[np.isfinite(v)])

    def value(self, mode, i, lines=slice(None)):
        """A passage's values (on some of its lines), minus what the mode subtracts."""
        return self.raw[i][lines] - self.shift[mode][i]

    @property
    def pixels(self):
        return int(np.isfinite(self.raw).sum())

    def blocks(self, x_range, y_range):
        """Each mesh's lines in view, with the world's copies (the map repeats it east and west): (passage, its
        lines in view, their x and y)."""
        x0, x1, y0, y1 = self.bounds.T
        for shift in (-WORLD, 0, WORLD):
            hit = (x1 + shift >= x_range[0]) & (x0 + shift <= x_range[1]) & (y1 >= y_range[0]) & (y0 <= y_range[1])
            for m in np.flatnonzero(hit):
                i, rows, x, y, line_y0, line_y1, _ = self.levels[1][1][m]
                # lines run south to north: the first reaching the view from the south and the last from the north
                # (a small view can fall between two lines: then these two are the cell around it)
                above, below = np.flatnonzero(line_y1 >= y_range[0]), np.flatnonzero(line_y0 <= y_range[1])
                if len(above) == 0 or len(below) == 0:
                    continue
                a, b = above[0], below[-1]
                block = slice(max(min(a, b) - 1, 0), max(a, b) + 2)  # one more line each side: whole cells
                lines = slice(rows.start + block.start, rows.start + min(block.stop, len(x)))
                yield i, lines, x[block] + shift, y[block]

    def rasters(self, mode, x_range, y_range, width, height, trim=frozenset()):
        """Each mesh in view on a width x height grid: (passage, first line of the block, its x and y, raster); the
        passages in `trim` ((cycle, pass) pairs) without the cells at the ends of their lines' data (`inner`)."""
        canvas = dsh.Canvas(plot_width=width, plot_height=height, x_range=x_range, y_range=y_range)
        trimmed = {i for i, key in enumerate(zip(self.sub.cycle.values.tolist(), self.sub.pass_id.values.tolist()))
                   if key in trim} if trim else set()
        for i, lines, xs, ys in self.blocks(x_range, y_range):
            v = self.value(mode, i, lines)
            mesh = xr.DataArray(inner(v) if i in trimmed else v, name="v", dims=("along", "cross"),
                                coords={"x": (("along", "cross"), xs), "y": (("along", "cross"), ys)})
            yield i, lines.start, xs, ys, canvas.quadmesh(mesh, x="x", y="y").values

    def mean_on(self, mode, x_range, y_range, width, height, trim=frozenset()):
        """Mean of the passages on a width x height grid, NaN where none. Where the cells are smaller than a pixel
        (on the ground, at the view's middle latitude), all of them in view are drawn as points in one go: from the
        coarsest level whose blocks still are, or the cells themselves (their lines in view, from the meshes).
        Zoomed in further, each passage's cells are drawn as meshes (those in `trim` inside their swath: `rasters`)."""
        pixel = (x_range[1] - x_range[0]) / width * np.cos(2 * np.arctan(np.exp(np.mean(y_range) / R)) - np.pi / 2)
        if self.spacing <= pixel:
            level = max([k for k in self.points if k * self.spacing <= pixel], default=1)
            xs, ys, vs = [], [], []
            if level > 1:
                x, y, v, i = self.points[level]
                band = slice(*np.searchsorted(y, y_range))
                x, y, v = x[band], y[band], v[band] - self.shift[mode][i[band]]
                for shift in (-WORLD, 0, WORLD):  # the map repeats the world east and west
                    keep = (x + shift >= x_range[0]) & (x + shift <= x_range[1])
                    xs.append(x[keep] + shift), ys.append(y[keep]), vs.append(v[keep])
            else:
                for i, lines, x, y in self.blocks(x_range, y_range):
                    xs.append(x.ravel()), ys.append(y.ravel()), vs.append(self.value(mode, i, lines).ravel())
            canvas = dsh.Canvas(plot_width=width, plot_height=height, x_range=x_range, y_range=y_range)
            if not xs:
                return np.full((height, width), np.nan)
            frame = pd.DataFrame({"x": np.concatenate(xs), "y": np.concatenate(ys), "v": np.concatenate(vs)})
            return canvas.points(frame, "x", "y", agg=dsh.mean("v")).values
        total, count = np.zeros((height, width)), np.zeros((height, width))
        for *_, m in self.rasters(mode, x_range, y_range, width, height, trim):
            total, count = total + np.where(np.isfinite(m), m, 0), count + np.isfinite(m)
        with np.errstate(invalid="ignore"):
            return total / count

    def along(self, mode, lon, lat, radius=1.5):
        """Each passage's values at the points: those of its nearest pixel within `radius` km (a 2 km cell has
        its center within 1.4 km of any point in it), else NaN. With the line of each nearest pixel (-1: none)."""
        pts = unit(lon, lat)
        near = (self.sub.lat.values >= lat.min() - 2) & (self.sub.lat.values <= lat.max() + 2)  # lines near them
        lines = np.flatnonzero(near)
        values, rows = [], []
        for i in range(len(self.raw)):
            v = self.value(mode, i)[near]
            ok = np.isfinite(v) & np.isfinite(self.sub.latitude.values[i][near])
            out, row = np.full(len(pts), np.nan), np.full(len(pts), -1)
            if ok.any():
                tree = cKDTree(unit(self.sub.longitude.values[i][near][ok], self.sub.latitude.values[i][near][ok]))
                d, j = tree.query(pts, distance_upper_bound=radius / swot_view.EARTH_RADIUS)
                hit = np.isfinite(d)
                out[hit] = v[ok][j[hit]]
                row[hit] = lines[np.nonzero(ok)[0][j[hit]]]
            values.append(out)
            rows.append(row)
        return np.array(values), np.array(rows)

    def near(self, west, east, south, north):
        """The data around a box, for clicks and lines (a view in memory has it all)."""
        return self

    def passages_at(self, mode, x, y):
        """The passages with data on the ~1 km cell at (x, y): (value, passage, the line nearest the point)."""
        found = []
        for i, first, xs, ys, m in self.rasters(mode, (x - 500, x + 500), (y - 500, y + 500), 1, 1):
            if np.isfinite(m[0, 0]):
                found.append((m[0, 0], i, first + np.unravel_index(np.nanargmin((xs - x) ** 2 + (ys - y) ** 2), xs.shape)[0]))
        return found


def from_blocks(read, src, times, west, east, south, north, shifts):
    """The passages `times` of a store over a box, read a block at a time (`read`), as a small view in memory whose
    modes subtract `shifts` (mode -> one number per passage of `times`) instead of its own."""
    over, rows, chunks = lines_over(src, times, west, east, south, north)
    if not len(over) or not len(rows):
        return None
    values, lat, lon, line_time = lines_of(read[0], src, over, chunks, read[1], rows)
    with warnings.catch_warnings(action="ignore"):  # of those, the lines whose own latitudes reach the box
        lo, hi = np.nanmin(lat, axis=(0, 2)), np.nanmax(lat, axis=(0, 2))
    near = np.flatnonzero((hi >= south) & (lo <= north))
    if not len(near):
        return None
    near = slice(max(near[0] - 1, 0), near[-1] + 2)
    dims = ("time", "along", "cross")
    sub = xr.Dataset({"v": (dims, values[:, near])},
                     coords={"latitude": (dims, lat[:, near]), "longitude": (dims, lon[:, near]),
                             "lat": ("along", src.ds.lat.values[rows][near]),
                             "cycle": ("time", src.index.cycle.values[over]), "pass_id": ("time", src.index.pass_id.values[over])})
    data = Loaded(sub, ["v"], west, east, line_time[:, near])
    at = np.searchsorted(times, over)
    data.shift = {mode: shift[at] for mode, shift in shifts.items()}
    return data


class Overview:
    """A view too big to keep in memory (the latest cycle of the whole globe, ~400 M pixels of L2 Expert): its
    passages are summed, a batch at a time, into world rasters for zooms up to ZOOM (sum and count of every cell
    as a point; the modes subtract one number per passage or for the box, so the colors and modes change without
    reading again); closer, and for clicks and lines, the passages are read a chunk at a time where they are needed
    (kept, as the detail blocks). The rasters are kept on disk (SWOT_CACHE_DIR) by commit and view."""

    ZOOM, BATCH = 4, 32

    def __init__(self, view, variable):
        end = pd.Timestamp(view["start"]) + pd.Timedelta(days=view["days"]) - pd.Timedelta(seconds=1)
        sub = swot_view.subset(ds, view["west"], view["east"], view["south"], view["north"], view["start"], end)
        self.times = np.flatnonzero(np.isin(ds.time.values, sub.time.values))  # the passages over the box
        first = int(np.searchsorted(ds.lat.values, sub.lat.values[0])) if sub.sizes["along"] else 0
        self.lines = slice(first, first + sub.sizes["along"])  # their lines in its latitudes
        self.var, self.names, self.variable = VARIABLES[variable], VARIABLES[variable].names, variable
        self.sub = index.isel(time=self.times)  # cycle and pass_id of each passage: what the modes subtract
        self.ready = threading.Event()
        size = 256 * 2**self.ZOOM
        self.sum = {mode: np.zeros((size, size), "float32") for mode in ("none", "passage")}
        self.count = np.zeros((size, size), "float32")
        self._shift = {mode: np.zeros(len(self.times), "float32") for mode in ("none", "box", "passage")}
        self.samples, self.quantiles, self.robust, self.pixels, self.seconds = [], {}, {}, 0, 0.0
        self.screen = None  # a sample of pixels with their positions (the colors of a screen), once read
        self.done, self.started = 0, None  # passages read so far, and when the build started (progress)
        self.error = None  # why the build failed, if it did (then it is "ready", with nothing to show)
        box = "_".join(f"{view[k]:g}" for k in ("west", "east", "south", "north"))
        # with what the variable is made of: a change of it (arrays, flag, surfaces, dB) is another overview
        spec = hashlib.sha1(repr((self.var.arrays, self.var.surface, self.var.db)).encode()).hexdigest()[:6]
        self.file = (os.path.join(CACHE_DIR, f"overview-{COMMIT.id}-{box}-{view['start']}-{view['days']}-{variable}-{spec}.npz")
                     if CACHE_DIR else None)

    @property
    def shift(self):
        """What each mode subtracts per passage: known once the whole view is read (tiles drawn before would keep
        colors of their own)."""
        self.ready.wait()
        return self._shift

    @property
    def nbytes(self):
        return sum(a.nbytes for a in self.sum.values()) + self.count.nbytes

    def add(self, at, values, lon, lat):
        """A batch of passages (`at`: their positions in `times`): summed into the rasters (each cell as a point)."""
        rng = np.random.default_rng(len(self.samples))
        with warnings.catch_warnings(action="ignore"):
            per = np.array([np.nanmedian(v.ravel()[::7]) for v in values], "float32")
        self._shift["passage"][at] = per
        ok = np.isfinite(values) & np.isfinite(lon) & np.isfinite(lat)
        x, y = mercator((lon[ok] + 180) % 360 - 180, lat[ok])
        v, which = values[ok], np.broadcast_to(np.arange(len(at))[:, None, None], values.shape)[ok]
        self.pixels += int(ok.sum())
        pick = rng.integers(0, max(len(v), 1), min(len(v), 200_000))
        self.samples.append((v[pick], at[which[pick]], ((lon[ok][pick] + 180) % 360 - 180).astype("float32"),
                             lat[ok][pick].astype("float32")))
        # the raster's pixel of each cell (rows from the south), summed with one count each (as datashader's points)
        size = self.count.shape[0]
        col = np.floor((x + WORLD / 2) / WORLD * size).astype(int)
        row = np.floor((y + WORLD / 2) / WORLD * size).astype(int)
        inside = (col >= 0) & (col < size) & (row >= 0) & (row < size)
        flat = row[inside] * size + col[inside]
        add = lambda w=None: np.bincount(flat, w, size * size).reshape(size, size).astype("float32")  # noqa: E731
        self.count += add()
        self.sum["none"] += add(v[inside])
        self.sum["passage"] += add((v - per[which])[inside])

    def finish(self):
        """What the box mode subtracts (the median), and the colors' quantiles, from the samples."""
        v = np.concatenate([a for a, *_ in self.samples]) if self.samples else np.zeros(0, "float32")
        at = np.concatenate([b for _, b, *_ in self.samples]) if self.samples else np.zeros(0, int)
        lon = np.concatenate([c for *_, c, _ in self.samples]) if self.samples else np.zeros(0, "float32")
        lat = np.concatenate([d for *_, d in self.samples]) if self.samples else np.zeros(0, "float32")
        keep = np.random.default_rng(0).permutation(len(v))[:SCREEN_SAMPLE]
        self.screen = (lon[keep], lat[keep], v[keep], at[keep].astype("int32"))
        self._shift["box"][:] = np.median(v) if len(v) else np.nan
        for mode, shift in self._shift.items():
            w = v - shift[at]
            self.quantiles[mode], self.robust[mode] = color_limits(w[np.isfinite(w)])
        self.samples = []
        self.ready.set()
        if self.file:  # kept for a restart (those of older commits go); if it cannot be, it is only not kept
            try:
                for old in Path(CACHE_DIR).glob(f"overview-*-{self.variable}-*.npz"):
                    if not old.name.startswith(f"overview-{COMMIT.id}-"):
                        old.unlink()
                np.savez(self.file, none=self.sum["none"], passage=self.sum["passage"], count=self.count,
                         shift=self._shift["passage"], box=self._shift["box"][:1], pixels=self.pixels,
                         quantiles=np.array([json.dumps(self.quantiles)]), robust=np.array([json.dumps(self.robust)]),
                         screen_lon=self.screen[0], screen_lat=self.screen[1], screen_v=self.screen[2], screen_at=self.screen[3])
            except OSError as e:
                print(f"overview not kept on disk: {e}", flush=True)

    def restore(self):
        """The rasters kept on disk, if any: True if read."""
        if not self.file or not os.path.exists(self.file):
            return False
        with np.load(self.file) as f:
            self.sum = {"none": f["none"], "passage": f["passage"]}
            self.count, self._shift["passage"] = f["count"], f["shift"]
            self._shift["box"][:] = f["box"][0]
            self.pixels = int(f["pixels"])  # read now from disk, not from the cloud: no seconds to show
            self.quantiles = json.loads(str(f["quantiles"][0]))
            self.robust = json.loads(str(f["robust"][0])) if "robust" in f else {}
            if "screen_v" in f:
                self.screen = (f["screen_lon"], f["screen_lat"], f["screen_v"], f["screen_at"])
        self.ready.set()
        return True

    def tile(self, x_range, y_range, width, height):
        """The zoom of a map tile within the rasters' (256 x 256, aligned), or None."""
        z = np.log2(WORLD / (x_range[1] - x_range[0]))
        if width != 256 or height != 256 or abs(z - round(z)) > 1e-6 or round(z) > self.ZOOM:
            return None
        return int(round(z))

    def mean_on(self, mode, x_range, y_range, width, height, trim=frozenset()):
        z = self.tile(x_range, y_range, width, height)
        if z is None:  # closer: the passages that cross the view, a chunk at a time
            data = self.near(*lonlat_box(x_range, y_range))
            return np.full((height, width), np.nan) if data is None else data.mean_on(mode, x_range, y_range, width, height, trim)
        self.ready.wait()
        k, size = 2 ** (self.ZOOM - z), WORLD / 2**z
        c0 = int(round((x_range[0] + WORLD / 2) / size)) * 256 * k
        r0 = int(round((y_range[0] + WORLD / 2) / size)) * 256 * k  # rows from the south, as datashader's
        cut = lambda a: a[r0:r0 + 256 * k, c0:c0 + 256 * k].reshape(256, k, 256, k).sum((1, 3))  # noqa: E731
        with np.errstate(invalid="ignore", divide="ignore"):
            mean = cut(self.sum["passage" if mode == "passage" else "none"]) / cut(self.count)
        return mean - (self._shift["box"][0] if mode == "box" else 0)

    def near(self, west, east, south, north):
        """The passages over a box, a chunk at a time, as a small view in memory (the modes as the whole view's)."""
        return from_blocks((base_block, self.variable), base, self.times, west, east, south, north, self.shift)


def lonlat_box(x_range, y_range):
    """A mercator box as (west, east, south, north) in degrees."""
    lon = np.rad2deg(np.array(x_range) / R)
    lat = np.rad2deg(2 * np.arctan(np.exp(np.array(y_range) / R)) - np.pi / 2)
    return lon[0], lon[1], lat[0], lat[1]


def build_overviews(view, overviews):
    """Read a view's passages a batch at a time (every variable's arrays at once) into its overviews. If that fails,
    each says why (and is ready): requests waiting for it get the error instead of waiting forever."""
    try:
        build(view, overviews)
    except Exception as e:
        print(f"overview failed: {e!r}\n{traceback.format_exc()}", flush=True)
        for o in overviews:
            if not o.ready.is_set():
                o.error = repr(e)
                o.ready.set()
        with cache_lock:  # read again on the next request
            for key in [k for k, o in OVERVIEWS.items() if o.error and k not in OPENING]:
                del OVERVIEWS[key]


def build(view, overviews):
    t0 = time.perf_counter()
    todo = [o for o in overviews if not o.restore()]
    if not todo:
        return
    names = sorted({n for o in todo for n in o.var.arrays})
    times = todo[0].times
    for o in todo:
        o.started = t0
    batches = [np.arange(b, min(b + Overview.BATCH, len(times))) for b in range(0, len(times), Overview.BATCH)]
    fetch = lambda at: read(ds.isel(time=times[at], along=todo[0].lines), names)[0]  # noqa: E731
    with ThreadPoolExecutor(1) as ahead:  # the next batch is read while this one is summed
        coming = ahead.submit(fetch, batches[0]) if batches else None
        for k, at in enumerate(batches):
            sub = coming.result()
            if k + 1 < len(batches):
                coming = ahead.submit(fetch, batches[k + 1])
            lon, lat = sub.longitude.values, sub.latitude.values
            for o in todo:
                o.add(at, o.var.values(lambda n: sub[n].values).astype("float32"), lon, lat)
                o.done = int(at[-1]) + 1
            print(f"overview: {at[-1] + 1} of {len(times)} passages, {time.perf_counter() - t0:.0f} s", flush=True)
    for o in todo:
        o.seconds = time.perf_counter() - t0
        o.finish()


loaded = OrderedDict()  # views in memory, least recently used first; the opening view stays
cache_lock = threading.Lock()  # guards `loaded` and `loading` (requests run in threads)
loading = {}  # one lock per view being read: a second request for it waits instead of reading it again


def read(sub, names):
    """A subset's arrays `names` (passages x lines x pixels, not loaded yet) in memory, with their positions, and when
    each line was: one read per array and passage, all at once. A read of the whole subset fetches its chunks nearly
    one at a time (the Gulf Stream, 12 days of L2 Expert: 17-45 s from AWS, ~1 s this way)."""
    n = sub.sizes["time"]
    arrays = names + ["latitude", "longitude", "line_time"]
    with ThreadPoolExecutor(64) as pool:
        parts = list(pool.map(lambda job: of_passage(sub, job[0], job[1]), [(a, k) for a in arrays for k in range(n)]))
    out = {a: np.stack(parts[j * n:(j + 1) * n]) if n else sub[a].values for j, a in enumerate(arrays)}
    timed = [a for a in names if "time" in sub[a].dims]  # the others are one per pass (Expert's surface classes)
    data = sub[timed]
    data = data.drop_vars([c for c in data.coords if c not in data.indexes and c not in ("latitude", "longitude", "cycle", "pass_id")])
    dims = sub["latitude"].dims  # (time, along, cross): not every array's (the calibration's are per line and side)
    data = data.copy(data={a: out[a] for a in timed}).assign_coords(latitude=(dims, out["latitude"]),
                                                                     longitude=(dims, out["longitude"]))
    return data.assign({a: (dims, out[a]) for a in names if a not in timed}).load(), out["line_time"]


def of_passage(sub, name, k):
    """An array of a subset's passage `k`: its own, or its pass's for arrays kept once per pass."""
    a = sub[name]
    return (a.isel(time=k) if "time" in a.dims else a.sel(pass_number=int(sub.pass_id.values[k]))).values


def load(west, east, south, north, start, days, variable):
    """The view's data and the seconds spent reading it (0 if it was in memory)."""
    key = (west, east, south, north, start, days, variable)
    if key in OVERVIEWS:  # a view too big to keep: its overview (ready or being built)
        return OVERVIEWS[key], 0.0
    if from_rasters(*key):
        return raster_view(key), 0.0
    with cache_lock:
        if key in loaded:
            loaded.move_to_end(key)
            return loaded[key], 0.0
        lock = loading.setdefault(key, threading.Lock())
    with lock:
        with cache_lock:
            if key in loaded:  # read meanwhile by another request
                return loaded[key], 0.0
        t0 = time.perf_counter()
        end = pd.Timestamp(start) + pd.Timedelta(days=days) - pd.Timedelta(seconds=1)
        var = VARIABLES[variable]
        sub = swot_view.subset(ds, west, east, south, north, start, end)
        if PRODUCT.get("overview") and sub.sizes["time"] * sub.sizes["along"] * sub.sizes["cross"] > OVERVIEW_PIXELS:
            with cache_lock:
                loading.pop(key, None)
            return overview_of(key), 0.0
        if sub.sizes["time"] * sub.sizes["along"] * sub.sizes["cross"] > MAX_PIXELS:
            raise HTTPException(413, f"{sub.sizes['time']} passages is too much at once: use fewer days or a smaller box")
        values, line_time = read(sub, var.arrays)
        data = Loaded(values, var, west, east, line_time)
        with cache_lock:
            loaded[key] = data
            loading.pop(key, None)
            keep = {key, tuple(DEFAULT.values())}
            while sum(d.nbytes for d in loaded.values()) > MAX_BYTES and len(loaded) > len(keep & loaded.keys()):
                del loaded[next(k for k in loaded if k not in keep)]
    return data, time.perf_counter() - t0


OVERVIEW_PIXELS = float(os.environ.get("SWOT_OVERVIEW_PIXELS", 60e6))  # views bigger than this: overviews
OVERVIEWS_KEPT = int(os.environ.get("SWOT_OVERVIEWS", 6))  # besides the opening view's (~0.2 GB each)


BEYOND = 2  # zooms past the rasters' still drawn from them (their pixels 2 x 2, 4 x 4)
FILLING = threading.Lock()  # one window's rasters summed at a time


class RasterView(Overview):
    """The whole globe on dates the daily rasters have (#98): tiles up to their zoom are the window's days summed, at
    once (nothing read); closer, clicks and lines as an overview's (the passages a chunk at a time)."""

    def __init__(self, view, variable):
        t0 = time.perf_counter()
        end = pd.Timestamp(view["start"]) + pd.Timedelta(days=view["days"]) - pd.Timedelta(seconds=1)
        sub = swot_view.subset(ds, view["west"], view["east"], view["south"], view["north"], view["start"], end)
        self.times = np.flatnonzero(np.isin(ds.time.values, sub.time.values))
        self.lines = slice(0, ds.sizes["along"])
        self.var, self.names, self.variable, self.view = VARIABLES[variable], VARIABLES[variable].names, variable, view
        self.sub = index.isel(time=self.times)
        self.flags = dict(dtu=self.var.dtu, cal=self.var.calibrated, water=not self.var.surface)
        self.ready, self.error, self.file, self.started, self.done = threading.Event(), None, None, None, len(self.times)
        # the colors: the window's means at zoom 2 (~40 km: 16 chunks a day), each pixel at its position (what the
        # screen's colors sample)
        m, n = RASTERS.mean(2, slice(None), slice(None), view["start"], view["days"], counts=True, **self.flags)
        size = m.shape[0]
        row, col = np.nonzero(np.isfinite(m))
        v = m[row, col].astype("float32")
        lon = (col + 0.5) / size * 360 - 180
        lat = np.rad2deg(2 * np.arctan(np.exp((row + 0.5) / size * 2 * np.pi - np.pi)) - np.pi / 2)
        median = float(np.median(v)) if len(v) else 0.0
        k = max(len(self.times), 1)
        self._shift = {"none": np.zeros(k, "float32"), "passage": np.zeros(k, "float32"), "box": np.full(k, median, "float32")}
        self.quantiles, self.robust = {}, {}
        for mode, shift in (("none", 0.0), ("passage", 0.0), ("box", median)):
            self.quantiles[mode], self.robust[mode] = color_limits(v - shift)
        self.screen = (lon.astype("float32"), lat.astype("float32"), v, np.zeros(len(v), "int32"))
        self.pixels, self.seconds = int(n.sum()), time.perf_counter() - t0
        self.ready.set()
        # the window's means at every zoom, summed in the background once kept (`raster_view`): then a tile is a slice
        # (till then, read alone)
        self.levels = None

    def fill(self):
        """One window summed at a time (several at once, a few dates tried in a row, held the CPU for a minute), and
        not one already left (pushed out of OVERVIEWS): till then its tiles are read from the rasters, a tile each."""
        with FILLING:
            if self not in OVERVIEWS.values():
                return
            t0 = time.perf_counter()
            self.levels = RASTERS.pyramid(self.view["start"], self.view["days"], **self.flags)
        print(f"rasters: {self.view['start']} + {self.view['days']} days summed in {time.perf_counter() - t0:.1f} s", flush=True)

    @property
    def nbytes(self):
        return sum(a.nbytes for a in self.screen) + sum(a.nbytes for a in (self.levels or {}).values())

    def mean_on(self, mode, x_range, y_range, width, height, trim=frozenset()):
        z = np.log2(WORLD / (x_range[1] - x_range[0]))
        if width != 256 or height != 256 or abs(z - round(z)) > 1e-6 or round(z) > rasters.ZOOM + BEYOND:
            # closer: the passages that cross the view, a chunk at a time
            return super().mean_on(mode, x_range, y_range, width, height, trim)
        # a map tile: its raster's block, or up to BEYOND zooms beyond them, theirs drawn larger (instead of reading
        # passages: the globe asks for tiles a zoom or two finer than the map's near its centre)
        z = int(round(z))
        level = min(z, rasters.ZOOM)
        k = 2 ** (z - level)
        n, size = 256 // k, WORLD / 2**z
        c0 = int(round((x_range[0] + WORLD / 2) / size)) * n
        r0 = int(round((y_range[0] + WORLD / 2) / size)) * n  # rows from the south, as datashader's
        rows, cols = slice(r0, r0 + n), slice(c0, c0 + n)
        m = (self.levels[level][rows, cols] if self.levels else
             RASTERS.mean(level, rows, cols, self.view["start"], self.view["days"], **self.flags))
        m = np.repeat(np.repeat(m, k, 0), k, 1)
        return m - (self._shift["box"][0] if mode == "box" else 0)


# the daily rasters (SWOT_RASTERS, app/rasters.py): the whole globe on the days they have, for SSHA (#98)
RASTERS = rasters.Rasters(os.environ["SWOT_RASTERS"]) if os.environ.get("SWOT_RASTERS") else None
RASTER_VARIABLES = {k for k, v in VARIABLES.items() if v.names[0] == "ssha_karin_2_xover"} if RASTERS else set()


def from_rasters(west, east, south, north, start, days, variable):
    """Whether a view comes from the daily rasters: the whole globe, an SSHA they hold, every day built."""
    return (variable in RASTER_VARIABLES and (east - west) % 360 == 0 and south <= -90 and north >= 90
            and RASTERS.covers(start, days))


def raster_view(key):
    """The RasterView of a view, made once (requests asking for it meanwhile wait); the oldest go beyond
    OVERVIEWS_KEPT (~90 MB each), the opening view's stay."""
    with cache_lock:
        if key in OVERVIEWS:
            return OVERVIEWS[key]
        lock = loading.setdefault(key, threading.Lock())
    with lock:
        with cache_lock:
            if key in OVERVIEWS:
                return OVERVIEWS[key]
        made = RasterView(dict(zip(("west", "east", "south", "north", "start", "days"), key[:6])), key[6])
        with cache_lock:
            OVERVIEWS[key] = made
            loading.pop(key, None)
            others = [k for k in OVERVIEWS if k not in OPENING]
            for old in others[:max(len(others) - OVERVIEWS_KEPT, 0)]:
                del OVERVIEWS[old]
        threading.Thread(target=made.fill, daemon=True).start()
        return made


def overview_of(key):
    """The overview of a view too big to keep (`key`), built in the background the first time (or read back from
    disk); the oldest go beyond OVERVIEWS_KEPT, the opening view's stay."""
    with cache_lock:
        if key in OVERVIEWS:
            return OVERVIEWS[key]
        view = dict(zip(("west", "east", "south", "north", "start", "days", "variable"), key))
        OVERVIEWS[key] = overview = Overview(view, view["variable"])
        others = [k for k in OVERVIEWS if k not in OPENING]
        for old in others[:max(len(others) - OVERVIEWS_KEPT, 0)]:
            del OVERVIEWS[old]
    threading.Thread(target=build_overviews, args=(view, [overview]), daemon=True).start()
    return overview


def view(west: float, east: float, south: float, north: float, start: str, days: int, variable: str):
    """The data of the view a request names, read again if it left memory: every request (tiles, values,
    downloads) carries the view itself, so many visitors cannot push each other's views out from under them."""
    return load(west, east, south, north, start, int(np.clip(days, 1, MAX_DAYS)), variable)[0]


# SWOT_NETCDF=0 leaves out the download of the subset (the original product, which the AVISO License does not
# let us redistribute through a public site without CNES's authorization)
NETCDF = os.environ.get("SWOT_NETCDF", "1") != "0"

app = FastAPI()
STATIC = Path(__file__).parent / "static"
# the release (pyproject.toml, also in the Docker image), shown in About
RELEASE = tomllib.loads((Path(__file__).parent.parent / "pyproject.toml").read_text())["project"]["version"]
# in the tile URLs: browsers keep tiles an hour, so a new server must not get the old ones
VERSION = hashlib.sha1(b"".join((Path(__file__).parent / f).read_bytes() for f in ("server.py", "swot_view.py"))).hexdigest()[:8]
app.mount("/static", StaticFiles(directory=STATIC), name="static")


# --- anonymous usage (#16; About says what): events from the page and the time of every request, a file per day in
# SWOT_TELEMETRY_DIR (none: nothing is recorded). Never kept: the address, a hash of it, browser details, any id
# that lasts more than a day. From the address, at once and then dropped: the country (SWOT_GEOIP, DB-IP Lite) and
# the visitor of the day (a hash with a salt that changes every day and is never kept: unique visitors per day, and
# no way back to the address nor across days).
TELEMETRY = os.environ.get("SWOT_TELEMETRY_DIR")
GEOIP = os.environ.get("SWOT_GEOIP")
EVENTS = {"open", "view", "point", "series", "line", "zoom_250m", "code", "download", "link", "close"}
FIELDS = ("session", "event", "screen", "from_link", "west", "east", "south", "north", "start", "days", "variable",
          "lon", "lat", "zoom", "projection", "mode", "cmap", "point_lon", "point_lat", "line", "line_km", "line_kind",
          "step", "what")
telemetry_lock, salts = threading.Lock(), {}
# of the tile requests (thousands a visit), one in TILE_SAMPLE is kept, with "sample": TILE_SAMPLE (1 on the dev server)
TILE_SAMPLE = int(os.environ.get("SWOT_TILE_SAMPLE", 10))
tiles_seen = itertools.count()


def record(kind, row):
    """A row (JSON) at the end of the day's file of its kind (events, requests)."""
    if not TELEMETRY:
        return
    line = json.dumps(row, separators=(",", ":")) + "\n"
    with telemetry_lock, open(os.path.join(TELEMETRY, f"{kind}-{row['time'][:10]}.jsonl"), "a") as f:
        f.write(line)


def visitor_day(address, agent, day):
    """The same for one address and browser on one day; the salt of a day goes when the next comes."""
    with telemetry_lock:
        if day not in salts:
            salts.clear()
            salts[day] = os.urandom(16)
        salt = salts[day]
    return hashlib.sha256(salt + address.encode() + agent.encode()).hexdigest()[:16]


@lru_cache(maxsize=1)
def geoip():
    import maxminddb

    return maxminddb.open_database(GEOIP)


def country(address):
    """The country code of an address (DB-IP Lite), "" if unknown."""
    if not GEOIP or not address:
        return ""
    try:
        return ((geoip().get(address) or {}).get("country") or {}).get("iso_code", "")
    except Exception:
        return ""


def address_of(request):
    """The visitor's address: the first of X-Forwarded-For behind a proxy (Caddy), else the connection's."""
    forwarded = request.headers.get("x-forwarded-for", "")
    return forwarded.split(",")[0].strip() or (request.client.host if request.client else "")


DEBUG_DIR = os.environ.get("SWOT_DEBUG_DIR")  # a test server's: what pages opened with ?debug=1 report of the map


@app.post("/api/debug")
async def debug_log(request: Request):
    """A batch of the map's events from a page opened with ?debug=1 (tiles asked, loaded, failed; layers; colors),
    appended to a file of the day, when SWOT_DEBUG_DIR is set (a test server)."""
    if DEBUG_DIR:
        body = json.loads(await request.body())  # the page's clock (sent, origin) and its events
        line = json.dumps(dict(at=time.time(), **(body if isinstance(body, dict) else dict(events=body))))
        with open(os.path.join(DEBUG_DIR, f"debug-{time.strftime('%Y-%m-%d')}.jsonl"), "a") as f:
            f.write(line + "\n")
    return Response(status_code=204)


@app.post("/api/event")
async def event(request: Request):
    """One event from the page, with what it says of the view (only the fields kept), the time, the visitor of the
    day and the country; nothing else of the request is kept."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    if TELEMETRY and isinstance(body, dict) and body.get("event") in EVENTS:
        now = pd.Timestamp.now("UTC")
        row = {k: body[k] for k in FIELDS if isinstance(body.get(k), (str, int, float, bool)) and len(str(body[k])) < 2000}
        address = address_of(request)
        row |= dict(time=now.isoformat(), version=RELEASE, country=country(address),
                    visitor_day=visitor_day(address, request.headers.get("user-agent", ""), str(now.date())))
        record("events", row)
    return Response(status_code=204)


@app.middleware("http")
async def timing(request: Request, call_next):
    """The time of every request to the API: its endpoint (the route, not the values asked), seconds and status."""
    t0 = time.perf_counter()
    response = await call_next(request)
    # how long the server took, for the page (?debug=1 tells it apart from the network's and the drawing's)
    response.headers["Server-Timing"] = f"app;dur={(time.perf_counter() - t0) * 1000:.1f}"
    if TELEMETRY and request.url.path.startswith("/api/") and request.url.path != "/api/event":
        route = getattr(request.scope.get("route"), "path", request.url.path)
        tile = route.endswith("{y}.png")
        if not tile or next(tiles_seen) % TILE_SAMPLE == 0:
            record("requests", dict(time=pd.Timestamp.now("UTC").isoformat(), endpoint=route, sample=TILE_SAMPLE if tile else 1,
                                    seconds=round(time.perf_counter() - t0, 3), status=response.status_code)
                   | (dict(z=int(request.path_params["z"])) if tile else {}))
    return response


@app.get("/")
def page():
    """The page, with each of our files versioned by its modification time, so browsers never keep an old one."""
    html = (STATIC / "index.html").read_text()
    for name in ("style.css", "app.js"):
        html = html.replace(f"static/{name}", f"static/{name}?v={int((STATIC / name).stat().st_mtime)}")
    if not NETCDF:
        html = "\n".join(line for line in html.splitlines() if 'id="download"' not in line)
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})


@app.get("/api/limits")
def limits(lon0: float, lon1: float, lat0: float, lat1: float, data=Depends(view)):
    """The robust color limits of each mode from the view's pixels on screen (the box lon0-lon1, lat0-lat1): red and
    blue at any zoom; the whole view's where too few pixels are on screen."""
    screen = getattr(data, "screen", None)
    out = screen_limits(screen, data.shift, lon0, lon1, lat0, lat1) if screen is not None else None
    return out or data.robust


@app.get("/api/progress")
def progress(west: float, east: float, south: float, north: float, start: str, days: int, variable: str):
    """How far the overview of a view being built is (passages read, of how many, seconds so far); {} otherwise."""
    o = OVERVIEWS.get((west, east, south, north, start, int(np.clip(days, 1, MAX_DAYS)), variable))
    if o is None or o.ready.is_set() or o.started is None:
        return {}
    return dict(done=o.done, total=len(o.times), seconds=round(time.perf_counter() - o.started, 1))


@app.get("/api/info")
def info():
    return dict(first=str(pd.Timestamp(ds.time.values[0])), last=str(pd.Timestamp(ds.time.values[-1])),
                passages=ds.sizes["time"], passes=ds.sizes["pass_number"], max_days=MAX_DAYS, default=DEFAULT,
                variables={k: v.label for k, v in VARIABLES.items()}, world=world()[1:], version=VERSION, release=RELEASE,
                # each variable's kind, units and default colormap; the colormaps on offer as 16 stops each
                calibration=ds[CAL[0]].attrs.get("version") if CAL[0] in ds else None,  # its version
                rasters=dict(first=str(RASTERS.first), last=str(RASTERS.last)) if RASTERS and RASTERS.first else None,
                looks={k: dict(kind=v.kind, units=v.units, cmap=v.cmap, fine=has_detail(k), twin=v.twin, water=not v.surface,
                                   mss=v.mss, dtu=v.dtu, cal=v.cal, calibrated=v.calibrated, rasters=k in RASTER_VARIABLES)
                       for k, v in VARIABLES.items()},
                colormaps={name: ["#%02x%02x%02x" % tuple(int(255 * c) for c in cm(x)[:3]) for x in np.linspace(0, 1, 16)]
                           for name, cm in COLORMAPS.items()}, diverging=list(COLORMAPS)[:DIVERGING],
                netcdf=NETCDF, product=PRODUCT["label"], credit=PRODUCT["credit"],
                public_url=os.environ.get("SWOT_PUBLIC_URL"),  # what a copied link starts with (the EC2's wake link)
                telemetry=bool(TELEMETRY),
                detail=None if detail is None else dict(zoom=DETAIL_ZOOM, product=DETAIL_PRODUCT["label"],
                                                        commit=detail.commit.id[:8],  # and the dates it covers so far
                                                        first=str(pd.Timestamp(detail.index.time.values.min())),
                                                        last=str(pd.Timestamp(detail.index.time.values.max()))),
                commit=dict(id=COMMIT.id[:8], time=str(pd.Timestamp(COMMIT.written_at).tz_convert(None)), message=COMMIT.message))


@app.get("/api/timeline")
def timeline(west: float, east: float, south: float, north: float):
    """Days with passages over the box, for the whole mission."""
    passes = swot_view.passes_over(geometry, west, east, south, north)
    over = index.isel(time=np.isin(index.pass_id.values, passes))
    per_day = over.cycle.to_series().groupby(over.time.to_index().floor("D")).agg(["size", "first"])
    return dict(day=(per_day.index.astype("int64") // 10**6).tolist(), n=per_day["size"].tolist(),
                cycle=per_day["first"].tolist())


@app.get("/api/load")
def load_view(west: float, east: float, south: float, north: float, start: str, days: int, variable: str):
    days = int(np.clip(days, 1, MAX_DAYS))
    data, seconds = load(west, east, south, north, start, days, variable)
    if isinstance(data, Overview):  # being built: its colors come at the end
        data.ready.wait()
        if data.error:
            raise HTTPException(500, f"Reading this view failed: {data.error}")
        seconds = data.seconds
    end = pd.Timestamp(start) + pd.Timedelta(days=days - 1)
    return dict(passages=data.sub.sizes["time"], seconds=round(seconds, 2),
                pixels=data.pixels, quantiles=data.quantiles, robust=data.robust,
                code=swot_view.snippet(west, east, south, north, start, str(end.date()), stored(VARIABLES[variable])[0],
                                       dtu=VARIABLES[variable].dtu, cal=VARIABLES[variable].calibrated))


def colormap(name, variable):
    """A colormap asked for by name, else the variable's own."""
    return name if name in COLORMAPS else VARIABLES[variable].cmap


EMPTY = io.BytesIO()
Image.new("RGBA", (256, 256)).save(EMPTY, format="png")


def png(mean, vmin, vmax, cmap, values=False):
    """A tile's means as a PNG in a colormap (transparent where NaN), or the empty tile. `values`: the means
    themselves for the page to color (VALUE_SCALE: 24 bits in red, green and blue, 0.1 mm from -838 to 838)."""
    if not np.isfinite(mean).any():
        return Response(EMPTY.getvalue(), media_type="image/png")
    if values:
        v = np.clip(np.round(np.nan_to_num(mean) * VALUE_SCALE) + 2**23, 0, 2**24 - 1).astype(np.uint32)
        rgba = np.stack([v >> 16, (v >> 8) & 255, v & 255, np.where(np.isfinite(mean), 255, 0)], -1).astype(np.uint8)
    else:
        rgba = (COLORMAPS[cmap](np.clip((mean - vmin) / (vmax - vmin), 0, 1)) * 255).astype(np.uint8)
        rgba[..., 3] = np.where(np.isfinite(mean), 255, 0)
    out = io.BytesIO()
    Image.fromarray(rgba[::-1]).save(out, format="png")  # rows from north to south
    return Response(out.getvalue(), media_type="image/png", headers={"Cache-Control": "max-age=3600"})


VALUE_SCALE = 1e4  # a value tile's units per unit of the variable


def tile_ranges(z, x, y):
    size = WORLD / 2**z
    x0, y1 = -WORLD / 2 + x * size, WORLD / 2 - y * size
    return (x0, x0 + size), (y1 - size, y1)


@app.get("/api/tile/{z}/{x}/{y}.png")
def tile(z: int, x: int, y: int, variable: str, mode: str = "box", vmin: float = -0.2, vmax: float = 0.2,
         cmap: str = None, values: bool = False, data=Depends(view)):
    # where the detail product shows (from the zoom before its own, on its passages), the base is drawn inside its
    # swath: always under it, never past its edges, so the page needs no fading between the two
    trim = DETAIL_PASSAGES if z >= DETAIL_ZOOM - 1 and has_detail(variable) else frozenset()
    return png(data.mean_on(mode, *tile_ranges(z, x, y), 256, 256, trim), vmin, vmax, colormap(cmap, variable), values)


# --- the detail product, tile by tile: each passage's lines near the tile, read a chunk at a time and kept

def block(src, product, i, c, variable):
    """A passage's (time index `i`) chunk `c` of lines of a store (`src`): a variable's values (as its product says)
    and positions, as float32, and when each line was."""
    n = src.ds.line_time.encoding["chunks"][1]
    var, p, cycle = product["variables"][variable], int(src.index.pass_id.values[i]), int(src.index.cycle.values[i])
    own = [k for k in var.arrays if k in src.ds]  # the others from the base store (`from_base`)
    shifted = var.calibrated and CAL[0] not in src.ds and SHIFT in src.ds  # its lines moved off the base's nadir
    sub = src.ds[own + ["latitude", "longitude", "line_time"] + ([SHIFT] if shifted else [])].isel(
        time=i, along=slice(c * n, (c + 1) * n))
    if "pass_number" in sub.dims:  # arrays kept once per pass: this passage's pass
        sub = sub.sel(pass_number=p)
    sub = sub.load()

    def get(k):
        if k == "cross":
            return to_nadir(sub.cross.values, sub[SHIFT].values[:, None] if shifted else 0)
        return sub[k].values if k in sub else from_base(k, p, sub.along.values, sub.cross.values, cycle)

    values = var.values(get)
    return (values.astype("float32"), sub.latitude.values.astype("float32"),
            sub.longitude.values.astype("float32"), sub.line_time.values)


def from_base(name, p, along, cross, cycle=None):
    """An array of the base store at a finer store's pixels of pass `p`, which lacks it: one kept once per pass
    (Expert's DTU25 MSS correction), or per passage and line (its calibration: the passage of the same `cycle`; NaN
    if the base has none), interpolated at their along and cross (both stores count them in km from the same
    reference track), linearly, and beyond the base's outermost pixels (the swath's edges). `along` and `cross`: the
    lines' and pixels' of a grid, or DataArrays of points (dims "pixel")."""
    a = base.ds[name]
    if "time" in a.dims:
        row = BASE_ROWS.get((cycle, p))
        if row is None:
            return np.full(np.shape(along) + a.shape[2:], np.nan)
        a = a.isel(time=row)
    else:
        a = a.sel(pass_number=p)
    a = a.sel(along=slice(float(np.min(along)) - 4, float(np.max(along)) + 4))
    return a.interp({"along": along} | ({"cross": cross} if "cross" in a.dims else {}), kwargs={"fill_value": None}).values


SHIFT = "cross_shift"  # Unsmoothed: whole 250 m columns each line was moved across track, onto its pass's reference


def to_nadir(cross, shift):
    """The pixels' distance to the satellite's nadir (km), from their cross and their line's shift in 250 m columns
    (NaN: none)."""
    return cross - 0.25 * np.nan_to_num(shift)


def single_flight(cached):
    """A cached reader that several requests may call at once for the same block (neighbouring tiles): one reads it,
    the others wait for it and take it from the cache (lru_cache alone lets each of them read it)."""
    locks, guard = {}, threading.Lock()

    def call(*args):
        with guard:
            lock = locks.setdefault(args, threading.Lock())
        with lock:
            out = cached(*args)
        with guard:
            if locks.get(args) is lock and not lock.locked():
                del locks[args]
        return out

    call.__wrapped__, call.cache_info, call.cache_clear = cached.__wrapped__, cached.cache_info, cached.cache_clear
    return call


@single_flight
@lru_cache(maxsize=int(os.environ.get("SWOT_DETAIL_BLOCKS", 1024)))  # ~3.4 MB each at 512 x 551 (float32)
def detail_block(i, c, variable):
    return block(detail, DETAIL_PRODUCT, i, c, variable)


@single_flight
@lru_cache(maxsize=int(os.environ.get("SWOT_BASE_BLOCKS", 4096)))  # ~0.4 MB each at 493 x 69 (L2 Expert)
def base_block(i, c, variable):
    return block(base, PRODUCT, i, c, variable)


def lines_of(read, src, times, chunks, variable, rows):
    """Passages (time indices) of a store on some of their lines (`rows`, in `chunks`), read a block at a time with
    `read`, all at once: values, latitudes, longitudes (passages x lines x pixels) and when each line was."""
    n = src.ds.line_time.encoding["chunks"][1]
    with ThreadPoolExecutor(32) as pool:
        reads = [[pool.submit(read, int(i), c, variable) for c in chunks] for i in times]
    blocks = [[r.result() for r in rs] for rs in reads]
    first = rows[0] - chunks[0] * n
    return [np.stack([np.concatenate([b[k] for b in bs])[first:first + len(rows)] for bs in blocks]) for k in range(4)]


def has_detail(variable):
    """Whether the detail product (250 m) has this variable (Unsmoothed: SSHA and sigma0, not SWH or wind)."""
    return detail is not None and variable in DETAIL_PRODUCT["variables"]


def detail_passages(times, chunks, variable, rows, cached=True):
    """The detail store's passages on some of their lines (as `lines_of`). Uncached (`cached=False`), for reads that
    would push a region's blocks out (a point's whole mission)."""
    return lines_of(detail_block if cached else detail_block.__wrapped__, detail, times, chunks, variable, rows)


def lines_over(src, times, west, east, south, north):
    """Of a store's passages (time indices), those whose lines may reach the box, and those lines (their nadir within
    0.7 degree of its latitudes: a line's edges lean up to ~0.6 degree off its nadir, and a small tile can hold only
    those of lines whose nadir is outside it), with the chunks they are in."""
    over = np.isin(src.index.pass_id.values[times], swot_view.passes_over(
        geometry, (west + 180) % 360 - 180, (east + 180) % 360 - 180, south - 0.7, north + 0.7))
    rows = np.flatnonzero((src.ds.lat.values >= south - 0.7) & (src.ds.lat.values <= north + 0.7))
    n = src.ds.line_time.encoding["chunks"][1]
    return times[over], rows, range(rows[0] // n, rows[-1] // n + 1) if len(rows) else range(0)


def detail_lines(west, east, south, north, start, days):
    """The detail store's passages in the dates whose lines may reach the box, and those lines (as `lines_over`)."""
    t0 = np.datetime64(start)
    times = np.flatnonzero((detail.index.time.values >= t0) & (detail.index.time.values < t0 + np.timedelta64(days, "D")))
    return lines_over(detail, times, west, east, south, north)


def offsets(data, mode):
    """What the base view subtracts (mode): one number for the box, or one per passage ((cycle, pass) -> mean), so
    the detail tiles take the same colors as the base's."""
    if mode == "none":
        return 0.0, {}
    shift = data.shift[mode]
    box = float(np.nanmean(shift))
    keys = zip(data.sub.cycle.values.tolist(), data.sub.pass_id.values.tolist())
    return box, {k: float(v) for k, v in zip(keys, shift) if np.isfinite(v)}


@app.get("/api/detail/{z}/{x}/{y}.png")
def detail_tile(z: int, x: int, y: int, start: str, days: int, variable: str, mode: str = "box",
                vmin: float = -0.2, vmax: float = 0.2, cmap: str = None, encode: bool = Query(False, alias="values"),
                data=Depends(view)):
    """A tile of the detail product (from DETAIL_ZOOM - 1 on), over the base's: its passages in the view's dates
    that cross the tile, minus what the base subtracts. Transparent where it has no data, so the base shows."""
    if z < DETAIL_ZOOM - 1 or not has_detail(variable):
        return Response(EMPTY.getvalue(), media_type="image/png")
    xr_, yr_ = tile_ranges(z, x, y)
    lon = np.rad2deg(np.array(xr_) / R)
    lat = np.rad2deg(2 * np.arctan(np.exp(np.array(yr_) / R)) - np.pi / 2)
    west, east, south, north = lon[0], lon[1], lat[0], lat[1]
    times, rows, chunks = detail_lines(west, east, south, north, start, days)
    if not len(times) or not len(rows):
        return Response(EMPTY.getvalue(), media_type="image/png")
    values, lat, lon, _ = detail_passages(times, chunks, variable, rows)
    # of those, the lines whose own latitudes reach the tile (and one more each side: whole cells)
    with warnings.catch_warnings(action="ignore"):
        lo, hi = np.nanmin(lat, axis=(0, 2)), np.nanmax(lat, axis=(0, 2))
    near = np.flatnonzero((hi >= south) & (lo <= north))
    if not len(near):
        return Response(EMPTY.getvalue(), media_type="image/png")
    near = slice(max(near[0] - 1, 0), near[-1] + 2)
    dims = ("time", "along", "cross")
    sub = xr.Dataset({"v": (dims, values[:, near])},
                     coords={"latitude": (dims, lat[:, near]), "longitude": (dims, lon[:, near])})
    tiles = Loaded(sub, ["v"], west, east)
    box, per = offsets(data, mode)
    cycles, passes = detail.index.cycle.values[times], detail.index.pass_id.values[times]
    shift = np.array([per.get((int(c), int(p)), box) for c, p in zip(cycles, passes)], "float32")
    tiles.shift["shifted"] = shift
    return png(tiles.mean_on("shifted", xr_, yr_, 256, 256), vmin, vmax, colormap(cmap, variable), encode)


@app.get("/api/value")
def value(lon: float, lat: float, mode: str = "box", quick: bool = False, data=Depends(view)):
    """The value under the mouse (the mean of the passages on a ~1 km cell there) and those passages, with
    the time of their line nearest the point. `quick` (the mouse over a map zoomed out): from the daily rasters' window
    means when the view has them (its ~10 km pixel, no passages), nothing read."""
    if quick and isinstance(data, RasterView) and data.levels:
        m = data.levels[rasters.ZOOM]
        size = m.shape[0]
        x, y = mercator(lon, lat)
        v = m[int(np.clip((y + WORLD / 2) / WORLD * size, 0, size - 1)), int(np.clip((x + WORLD / 2) / WORLD * size, 0, size - 1))]
        v = v - (data._shift["box"][0] if mode == "box" else 0)
        return dict(value=None if np.isnan(v) else round(float(v), 4), passages=[])
    data = data.near(lon - 0.05, lon + 0.05, lat - 0.05, lat + 0.05)
    found = [] if data is None else data.passages_at(mode, *mercator(lon, lat))
    passages = sorted((str(pd.Timestamp(data.line_time[i, line]).round("s")), int(data.sub.pass_id[i]),
                       int(data.sub.cycle[i])) for _, i, line in found)
    return dict(value=round(float(np.mean([v for v, *_ in found])), 4) if found else None,
                passages=[dict(time=t, pass_id=p, cycle=c) for t, p, c in passages])


# steps below FINE km read the detail product (L2 Unsmoothed: 250 m), each passage's nearest pixel within FINE_RADIUS
# (a 250 m cell has its centre within 0.18 km of any point in it); its Hovmöller only on lines up to FINE_LENGTH km
FINE, FINE_RADIUS, FINE_LENGTH = 1.0, 0.2, 300.0


def line_box(lon, lat):
    """The box of a polyline's points, with a swath's half width (~0.6 degrees) around it."""
    ulon = np.unwrap(lon, period=360)
    west, east = (-180.0, 180.0) if np.ptp(ulon) > 356 else (ulon.min() - 1, ulon.max() + 1)
    return west, east, max(lat.min() - 1, -90), min(lat.max() + 1, 90)


def detail_profile(lon, lat, mode, start, days, variable, data):
    """The detail store's passages in the view's dates along the points: each one's nearest pixel within FINE_RADIUS,
    minus what the base view subtracts (as the detail tiles), with when its line was."""
    if not has_detail(variable):
        return []
    times, rows, chunks = detail_lines(*line_box(lon, lat), start, days)
    if not len(times) or not len(rows):
        return []
    values, la, lo, line_time = detail_passages(times, chunks, variable, rows)
    box, per = offsets(data, mode)
    pts, out = unit(lon, lat), []
    for k, i in enumerate(times):
        ok = np.isfinite(values[k]) & np.isfinite(la[k]) & np.isfinite(lo[k])
        if not ok.any():
            continue
        d, j = cKDTree(unit(lo[k][ok], la[k][ok])).query(pts, distance_upper_bound=FINE_RADIUS / swot_view.EARTH_RADIUS)
        hit = np.isfinite(d)
        if not hit.any():
            continue
        flat = np.flatnonzero(ok)[j[hit]]
        row = np.unravel_index(flat, ok.shape)[0]
        cycle, pass_id = int(detail.index.cycle.values[i]), int(detail.index.pass_id.values[i])
        value = np.full(len(pts), np.nan)
        value[hit] = values[k].ravel()[flat] - per.get((cycle, pass_id), box)
        ms = np.full(len(pts), -1, "int64")
        ms[hit] = line_time[k][row].astype("datetime64[ms]").astype("int64")
        out.append(dict(time=str(pd.Timestamp(line_time[k][row[len(row) // 2]]).round("s")), pass_id=pass_id, cycle=cycle,
                        value=[None if np.isnan(v) else round(float(v), 4) for v in value],
                        line_time=[None if m < 0 else int(m) for m in ms]))
    return out


@app.get("/api/transect")
def transect(points: str, mode: str = "box", step: float = 2.0, start: str = None, days: int = 1, variable: str = None,
             data=Depends(view)):
    """Values along a polyline ("lon,lat;lon,lat;..."), every `step` km (2: the L3 grid) along great circles,
    one curve per passage that has any, with the time of the line each value comes from (ms) and the time of
    the line at its middle point. Below FINE km (250 m), from the detail store."""
    lon, lat = np.array([p.split(",") for p in points.split(";")], float).T
    fine = step < FINE and has_detail(variable)
    lon, lat, dist, step = along_line(lon, lat, max(step, 0.25 if fine else 0.5))
    if fine:
        return dict(distance=dist.round(3).tolist(), lon=lon.round(4).tolist(), lat=lat.round(4).tolist(),
                    step=round(float(step), 3), passages=sorted(detail_profile(lon, lat, mode, start, days, variable, data),
                                                                key=lambda q: q["time"]))
    data = data.near(*line_box(lon, lat))
    if data is None:
        return dict(distance=dist.round(3).tolist(), lon=lon.round(4).tolist(), lat=lat.round(4).tolist(),
                    step=round(float(step), 3), passages=[])
    values, rows = data.along(mode, lon, lat)
    passages = []
    for i in np.flatnonzero(np.isfinite(values).any(1)):
        seen = rows[i][rows[i] >= 0]
        t = pd.Timestamp(data.line_time[i, seen[len(seen) // 2]]).round("s")
        times = data.line_time[i, rows[i].clip(0)].astype("datetime64[ms]").astype("int64")
        passages.append(dict(time=str(t), pass_id=int(data.sub.pass_id[i]), cycle=int(data.sub.cycle[i]),
                             value=[None if np.isnan(v) else round(float(v), 4) for v in values[i]],
                             line_time=[None if r < 0 else int(ms) for r, ms in zip(rows[i], times)]))
    return dict(distance=dist.round(3).tolist(), lon=lon.round(4).tolist(), lat=lat.round(4).tolist(),
                step=round(float(step), 3), passages=sorted(passages, key=lambda q: q["time"]))


@app.get("/api/hovmoller")
def hovmoller(points: str, variable: str, step: float = 2.0):
    """A polyline's values over the whole mission: for each passage that crosses it, the values of its nearest
    pixels within 1.5 km (as /api/transect), on the points every `step` km, and the time of its line at the middle.
    The grid of a pass is the same in every cycle (a repeat orbit), and so is when each line is measured relative
    to the passage's time: both are read once, on its first passage; then only the values of the lines near the
    polyline are read, for all its passages. Below FINE km (250 m), from the detail store, on lines up to FINE_LENGTH."""
    fine = step < FINE and has_detail(variable)
    product, src = (DETAIL_PRODUCT, detail) if fine else (PRODUCT, base)
    ds, index, var, radius = src.ds, src.index, product["variables"][variable], FINE_RADIUS if fine else 1.5
    lon, lat = np.array([p.split(",") for p in points.split(";")], float).T
    lon, lat, dist, step = along_line(lon, lat, max(step, 0.25 if fine else 0.5))
    if fine and dist[-1] > FINE_LENGTH:
        raise HTTPException(413, f"At 250 m, the Hovmöller takes lines up to {FINE_LENGTH:g} km: draw a shorter one")
    pts = unit(lon, lat)
    west, east, south, north = line_box(lon, lat)  # the passes over the line's box
    near = np.flatnonzero((ds.lat.values >= south - 1) & (ds.lat.values <= north + 1))  # lines run south to north
    if not len(near):
        return dict(distance=dist.round(3).tolist(), lon=lon.round(4).tolist(), lat=lat.round(4).tolist(),
                    step=round(float(step), 3), passages=[])
    r0, r1 = near[0], near[-1] + 1

    def nearest(p):  # a pass: its passages, and the line and pixel nearest each point it covers
        times = np.flatnonzero(index.pass_id.values == p)
        if not len(times):
            return None
        geo = ds[["latitude", "longitude", "line_time"]].isel(time=times[0], along=slice(r0, r1)).load()
        la, lo = geo.latitude.values, geo.longitude.values
        ok = np.isfinite(la) & np.isfinite(lo)
        if not ok.any():
            return None
        d, j = cKDTree(unit(lo[ok], la[ok])).query(pts, distance_upper_bound=radius / swot_view.EARTH_RADIUS)
        hit = np.flatnonzero(np.isfinite(d))
        if not len(hit):
            return None
        row, col = np.unravel_index(np.flatnonzero(ok)[j[hit]], la.shape)
        offset = geo.line_time.values[row] - index.time.values[times[0]]  # of each line hit, from its passage's time
        return p, times, hit, r0 + row, col, offset

    passes = swot_view.passes_over(geometry, (west + 180) % 360 - 180, (east + 180) % 360 - 180, south, north)
    with ThreadPoolExecutor(32) as pool:
        found = [f for f in pool.map(nearest, passes) if f]
    # the values of every passage at its pixels, in one read: zarr fetches each passage's chunk once, all at
    # once (asking pass by pass cost ~10 ms of Python per chunk: 50-80 s for a line across the Pacific)
    at = [np.repeat(times, len(hit)) for _, times, hit, *_ in found]
    rows = [np.tile(row, len(times)) for _, times, _, row, *_ in found]
    cols = [np.tile(col, len(times)) for _, times, _, _, col, _ in found]
    if found:
        where = {dim: xr.DataArray(np.concatenate(v), dims="pixel") for dim, v in (("time", at), ("along", rows), ("cross", cols))}
        per_pass = {**where, "pass_number": xr.DataArray(np.searchsorted(ds.pass_number.values, index.pass_id.values[where["time"]]), dims="pixel")}
        per_pass.pop("time")
        passes = index.pass_id.values[where["time"]]

        cycles = index.cycle.values[where["time"]]
        along, cross = ds.along.values[where["along"].values], ds.cross.values[where["cross"].values]
        shifted = var.calibrated and CAL[0] not in ds and SHIFT in ds  # its lines moved off the base's nadir

        def get(n):
            if n == "cross":
                return to_nadir(cross, ds[SHIFT].isel(time=where["time"], along=where["along"]).values if shifted else 0)[:, None]
            if n in ds:
                return ds[n].isel(where if "time" in ds[n].dims else per_pass, missing_dims="ignore").values
            # only in the base store: at the pixels' along and cross, by pass (or passage: the calibration)
            keys = np.stack([cycles if "time" in base.ds[n].dims else 0 * cycles, passes], 1)
            out = None
            for c, p in np.unique(keys, axis=0):
                k = np.flatnonzero((keys == (c, p)).all(1))
                v = from_base(n, int(p), xr.DataArray(along[k], dims="pixel"), xr.DataArray(cross[k], dims="pixel"), int(c))
                out = np.empty((len(passes),) + v.shape[1:]) if out is None else out
                out[k] = v
            return out

        values = var.values(get)
    out, k0 = [], 0
    for p, times, hit, row, col, offset in found:
        v = values[k0:k0 + len(times) * len(hit)].reshape(len(times), len(hit))  # (passages, points hit)
        k0 += v.size
        for k, i in enumerate(times):
            good = np.isfinite(v[k])
            if not good.any():
                continue
            line = np.full(hit[-1] - hit[0] + 1, np.nan)
            line[hit - hit[0]] = v[k]
            middle = index.time.values[i] + offset[good][good.sum() // 2]
            out.append(dict(pass_id=int(p), cycle=int(index.cycle.values[i]), first=int(hit[0]),
                            time=int(middle.astype("datetime64[ms]").astype("int64")),
                            value=[None if np.isnan(x) else round(float(x), 4) for x in line]))
    return dict(distance=dist.round(3).tolist(), lon=lon.round(4).tolist(), lat=lat.round(4).tolist(),
                step=round(float(step), 3), passages=sorted(out, key=lambda q: q["time"]))


def detail_series(lon, lat, variable, radius):
    """The mean of the detail store's pixels within `radius` km of the point, per passage (as `point_series`), each
    passage read and reduced on its own (the whole mission at 250 m would not fit in memory at once)."""
    km = swot_view.EARTH_RADIUS * np.pi / 180
    dlat, dlon = radius / km + 0.5, radius / (km * np.cos(np.deg2rad(lat)))  # swath edges lean ~0.3 degree off nadir
    passes = swot_view.passes_over(geometry, lon - dlon, lon + dlon, lat - dlat, lat + dlat)
    times = np.flatnonzero(np.isin(detail.index.pass_id.values, passes))
    rows = np.flatnonzero(np.abs(detail.ds.lat.values - lat) <= dlat)
    if not len(times) or not len(rows):
        return dict(time=[], value=[])
    n = detail.ds.line_time.encoding["chunks"][1]
    chunks = range(rows[0] // n, rows[-1] // n + 1)
    least = max(1, round(np.pi * radius**2 / DETAIL_PRODUCT["pixel"] ** 2 / 4))  # a quarter of the circle's pixels

    def mean(i):
        values, la, lo, _ = detail_passages([i], chunks, variable, rows, cached=False)
        v = np.where(swot_view.distance(la, lo, lat, lon) <= radius, values, np.nan)
        return np.nanmean(v) if np.isfinite(v).sum() >= least else np.nan

    with ThreadPoolExecutor(16) as pool, warnings.catch_warnings(action="ignore"):
        means = np.array(list(pool.map(mean, times)))
    keep = np.isfinite(means)
    t = detail.index.time.values[times[keep]]
    order = np.argsort(t)
    return dict(time=(t[order].astype("datetime64[ms]").astype("int64")).tolist(), value=means[keep][order].round(4).tolist())


@app.get("/api/series")
def series(lon: float, lat: float, variable: str, radius: float = 5, fine: bool = False):
    """The mean around a point, per passage: within `radius` km, or (`fine`) of the detail store's 250 m pixels."""
    if fine:
        if not has_detail(variable):
            raise HTTPException(404, "no 250 m product for this variable")
        return detail_series(lon, lat, variable, radius)
    var = VARIABLES[variable]
    s = swot_view.point_series(ds, lon, lat, var.names, radius, mask=var.flag, db=var.db, surface=var.surface,
                               calibrate=var.calibrated)["value"]
    return dict(time=(s.time.to_index().astype("int64") // 10**6).tolist(), value=s.values.round(4).tolist())


# --- each chart's numbers as NetCDF, and the code that reproduces it

def as_netcdf(dataset, name):
    if not NETCDF:
        raise HTTPException(404)
    buf = io.BytesIO()
    dataset.to_netcdf(buf, engine="h5netcdf")
    return Response(buf.getvalue(), media_type="application/x-netcdf",
                    headers={"Content-Disposition": f'attachment; filename="{name}.nc"'})


def line_dataset(result, var):
    """A profile's or a Hovmöller's passages as (passage, distance) values, with their time, pass and cycle."""
    n = len(result["distance"])
    values = np.full((len(result["passages"]), n), np.nan)
    for k, q in enumerate(result["passages"]):
        first = q.get("first", 0)
        values[k, first:first + len(q["value"])] = [np.nan if v is None else v for v in q["value"]]
    times = [pd.Timestamp(q["time"], unit="ms") if isinstance(q["time"], int) else pd.Timestamp(q["time"])
             for q in result["passages"]]
    return xr.Dataset(
        {"value": (("passage", "distance"), values, {"long_name": var.label, "units": var.units})},
        coords={"distance": ("distance", result["distance"], {"units": "km", "long_name": "along the line"}),
                "lon": ("distance", result["lon"]), "lat": ("distance", result["lat"]),
                "time": ("passage", times), "pass_number": ("passage", [q["pass_id"] for q in result["passages"]]),
                "cycle": ("passage", [q["cycle"] for q in result["passages"]])},
        attrs={"comment": f"each passage's nearest pixel within {FINE_RADIUS if result['step'] < FINE else 1.5:g} km of "
                          "points every `step` km along great circles", "step_km": result["step"]})


FINE_SERIES_RADIUS = 1.0  # km: the 250 m series' circle (~50 pixels)


@app.get("/api/series.nc")
def series_nc(lon: float, lat: float, variable: str, radius: float = 5):
    """The series at a point, and with a detail store, its 250 m series within FINE_SERIES_RADIUS (time_250m)."""
    s = series(lon, lat, variable, radius)
    var = VARIABLES[variable]
    out = xr.Dataset({"value": ("time", s["value"], {"long_name": var.label, "units": var.units,
                                                     "comment": f"{PRODUCT['label']}, mean within {radius:g} km"})},
                     coords={"time": pd.to_datetime(s["time"], unit="ms")},
                     attrs={"lon": lon, "lat": lat, "radius_km": radius,
                            "comment": "mean of the pixels within radius_km of (lon, lat), per passage"})
    if has_detail(variable):
        f = series(lon, lat, variable, FINE_SERIES_RADIUS, fine=True)
        out["value_250m"] = ("time_250m", f["value"], {"long_name": var.label, "units": var.units,
                                                       "comment": f"{DETAIL_PRODUCT['label']}, mean within {FINE_SERIES_RADIUS:g} km"})
        out = out.assign_coords(time_250m=pd.to_datetime(f["time"], unit="ms"))
    return as_netcdf(out, "swot_series")


@app.get("/api/transect.nc")
def transect_nc(points: str, variable: str, mode: str = "box", step: float = 2.0, start: str = None, days: int = 1,
                data=Depends(view)):
    return as_netcdf(line_dataset(transect(points, mode, step, start, days, variable, data), VARIABLES[variable]),
                     "swot_profile")


@app.get("/api/hovmoller.nc")
def hovmoller_nc(points: str, variable: str, step: float = 2.0):
    return as_netcdf(line_dataset(hovmoller(points, variable, step), VARIABLES[variable]), "swot_hovmoller")


@app.get("/api/code/series")
def series_code(lon: float, lat: float, variable: str, radius: float = 5):
    var, fine = VARIABLES[variable], None
    if has_detail(variable):
        f = DETAIL_PRODUCT["variables"][variable]
        fine = dict(target=DETAIL_STORE, names=stored(f), radius=FINE_SERIES_RADIUS, mask=f.flag, db=f.db,
                    pixel=DETAIL_PRODUCT["pixel"], label="250 m pixels", dtu=f.dtu, cal=f.calibrated)
    return dict(code=swot_view.series_snippet(lon, lat, stored(var), radius, f"{var.label}, ", target=STORE, mask=var.flag,
                                              db=var.db, fine=fine, units=var.units, dtu=var.dtu, cal=var.calibrated))


def stored(var):
    """A variable's arrays summed in the code people copy, the DTU25 correction aside (its own lines there)."""
    return [n for n in var.names if n != DTU]


@app.get("/api/code/line")
def line_code(points: str, variable: str, step: float = 2.0, start: str = None, days: int = 1, mean: str = "time"):
    """The profile of the passages from `start` for `days` days, or (no start) the Hovmöller of the mission."""
    var = VARIABLES[variable]
    label = var.label
    vertices = [tuple(map(float, p.split(","))) for p in points.split(";")]
    end = None if start is None else str(pd.Timestamp(start) + pd.Timedelta(days=days) - pd.Timedelta(seconds=1))
    title = f"{label}, " + (f"{start} to {end[:10]}" if start else "whole mission")
    if step < FINE and has_detail(variable):  # 250 m
        f = DETAIL_PRODUCT["variables"][variable]
        return dict(code=swot_view.line_snippet(vertices, stored(f), step, start, end, mean, title, target=DETAIL_STORE,
                                                radius=FINE_RADIUS, mask=f.flag, db=f.db, units=f.units,
                                                dtu=(STORE if DTU not in detail.ds else "here") if f.dtu else None,
                                                cal=(STORE if CAL[0] not in detail.ds else "here") if f.calibrated else None))
    return dict(code=swot_view.line_snippet(vertices, stored(var), step, start, end, mean, title, target=STORE,
                                            mask=var.flag, db=var.db, units=var.units, dtu="here" if var.dtu else None,
                                            cal="here" if var.calibrated else None))


@app.get("/api/netcdf")
def netcdf(data=Depends(view)):
    if not NETCDF:
        raise HTTPException(404)
    if isinstance(data, Overview):
        raise HTTPException(413, "The whole cycle is too big for one file: select a region")
    buf = io.BytesIO()
    data.sub.to_netcdf(buf, engine="h5netcdf")
    return Response(buf.getvalue(), media_type="application/x-netcdf",
                    headers={"Content-Disposition": 'attachment; filename="swot_subset.nc"'})


@lru_cache
def world():
    """Equal Earth world map for the overview (PNG), and its half width and height on the unit sphere."""
    import cartopy.crs as ccrs
    import cartopy.feature as cfeature
    import matplotlib.pyplot as plt

    radius = 6371000.0
    proj = ccrs.EqualEarth(globe=ccrs.Globe(ellipse="sphere", semimajor_axis=radius, semiminor_axis=radius))
    fig = plt.figure(figsize=(4.1, 2), dpi=100)
    ax = fig.add_axes([0, 0, 1, 1], projection=proj)
    ax.set_global()
    ax.add_feature(cfeature.LAND, facecolor="#64748b", edgecolor="none")
    ax.spines["geo"].set_edgecolor("#94a3b8")
    ax.set_facecolor("#111827")
    fig.patch.set_alpha(0)
    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    plt.close(fig)
    return buf.getvalue(), proj.x_limits[1] / radius, proj.y_limits[1] / radius


@app.get("/api/world.png")
def world_png():
    return Response(world()[0], media_type="image/png", headers={"Cache-Control": "max-age=86400"})


# the opening view, ready before the first visitor: in memory, or for a product too big to keep (`overview`), its
# overview, built in the background (or read back from SWOT_CACHE_DIR); other variables on it are built when asked
OVERVIEWS, OPENING = {}, set()  # views too big to keep: their overviews (OrderedDict order: oldest first)
if PRODUCT.get("overview") and RASTERS and from_rasters(*DEFAULT.values()):
    OPENING.add(key := tuple(DEFAULT.values()))
    load(**DEFAULT)
elif PRODUCT.get("overview"):
    OPENING.add(key := tuple(DEFAULT.values()))
    OVERVIEWS[key] = Overview(DEFAULT, DEFAULT["variable"])
    threading.Thread(target=build_overviews, args=(DEFAULT, [OVERVIEWS[key]]), daemon=True).start()
else:
    load(**DEFAULT)
