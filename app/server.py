"""SWOT viewer: a small API over the IceChunk store, and the page in static/.
The swaths are drawn here, as map tiles: each passage is a curvilinear mesh rasterized with datashader,
so only images of the screen's size go to the browser.
Run: uvicorn server:app --app-dir app --port 5006   (SWOT_STORE, credentials from AWS_* in the environment)"""
import io
import os
import threading
import time
import warnings
from collections import OrderedDict
from functools import lru_cache
from pathlib import Path

import datashader as dsh
import matplotlib
import numpy as np
import pandas as pd
import xarray as xr
import zarr
from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from PIL import Image

import swot_view

matplotlib.use("agg")
# zarr fetches 10 chunks at once by default; S3 answers each in tens of ms, so more at once is faster
# (whole globe, 1 day: 15 s -> 5 s with 8 CPUs; scripts/bench_server.py)
zarr.config.set({"async.concurrency": 64})
MAX_DAYS = 21
MAX_PIXELS = 60e6  # ~2 days of the whole globe; keeps a load under ~1 GB of memory
MAX_BYTES = float(os.environ.get("SWOT_CACHE_GB", 4)) * 1e9  # loaded views kept in memory, beyond the newest
VARIABLES = {"ssha_unfiltered": ("SSHA, unfiltered", ["ssha_unfiltered"]),
             "ssha_filtered": ("SSHA, filtered", ["ssha_filtered"]),
             "adt": ("ADT = SSHA + MDT", ["ssha_filtered", "mdt"])}
MODES = ("box", "passage", "none")  # spatial mean subtracted: of the box, of each passage, nothing
# opening view: the whole globe on one day (kept in memory)
DEFAULT = dict(west=-180, east=180, south=-90, north=90, start="2024-11-02", days=1, variable="ssha_unfiltered")
R = 6378137.0  # web mercator
WORLD = 2 * np.pi * R
CMAP = matplotlib.colormaps["RdBu_r"]

ds = swot_view.open_store(os.environ.get("SWOT_STORE", swot_view.STORE))
index = ds[["cycle", "pass_id"]].reset_coords().load()  # when each passage was, for the timeline
geometry = ds[["lon_min", "lon_max"]].load()  # which passes cross a box, without reading data


def mercator(lon, lat):
    return R * np.deg2rad(lon), R * np.log(np.tan(np.pi / 4 + np.deg2rad(lat) / 2))


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


class Loaded:
    """One subset in memory: the passages over a box in a time window, ready to draw."""

    def __init__(self, sub, names, west, east):
        self.sub, self.names = sub, names
        values = sum(sub[n] for n in names).values
        lon = sub.longitude.values
        w, e = box_lons(west, east)
        inside = np.where((lon - w) % 360 <= e - w, values, np.nan)
        center = (w + e) / 2
        with warnings.catch_warnings(action="ignore"):  # all-NaN passages give NaN means
            self.values = {"none": values, "box": values - np.nanmean(inside),
                           "passage": values - np.nanmean(inside, axis=(1, 2), keepdims=True)}
        # meshes: each run of consecutive lines with all corners (a mesh needs them) and some data. Joining
        # runs across the lines between them (land, missing lines) would draw long thin quads over the gap.
        self.meshes = []
        lat = sub.latitude.values
        for i in range(len(values)):
            usable = np.isfinite(lon[i]).all(1) & np.isfinite(lat[i]).all(1) & np.isfinite(values[i]).any(1)
            edges = np.flatnonzero(np.diff(np.r_[0, usable.astype(int), 0]))
            for a, b in zip(edges[::2], edges[1::2]):
                if b - a < 2:  # one line has no cells
                    continue
                rows = slice(a, b)
                xi, yi = mercator(continuous(lon[i][rows], center), lat[i][rows])
                # lines run south to north: a view needs only the block of lines across its latitudes
                self.meshes.append((i, rows, xi, yi, yi.min(1), yi.max(1), (xi.min(), xi.max(), yi.min(), yi.max())))
        self.nbytes = (sub.nbytes + sum(v.nbytes for v in self.values.values())
                       + sum(m[2].nbytes + m[3].nbytes for m in self.meshes))
        self.quantiles = {}
        for mode, v in self.values.items():
            v = v[np.isfinite(v)]
            self.quantiles[mode] = np.quantile(v, np.linspace(0, 1, 1001)).round(4).tolist() if len(v) else []

    def mean_on(self, mode, x_range, y_range, width, height):
        """Mean of the passages on a width x height grid, NaN where none."""
        canvas = dsh.Canvas(plot_width=width, plot_height=height, x_range=x_range, y_range=y_range)
        total, count = np.zeros((height, width)), np.zeros((height, width))
        for i, rows, x, y, line_y0, line_y1, (x0, x1, y0, y1) in self.meshes:
            seen = np.flatnonzero((line_y1 >= y_range[0]) & (line_y0 <= y_range[1]))
            if len(seen) == 0:
                continue
            block = slice(max(seen[0] - 1, 0), seen[-1] + 2)  # one more line each side: whole cells
            v = self.values[mode][i][rows][block]
            for shift in (-WORLD, 0, WORLD):  # the map repeats the world east and west
                if x1 + shift < x_range[0] or x0 + shift > x_range[1]:
                    continue
                mesh = xr.DataArray(v, name="v", dims=("along", "cross"),
                                    coords={"x": (("along", "cross"), x[block] + shift),
                                            "y": (("along", "cross"), y[block])})
                m = canvas.quadmesh(mesh, x="x", y="y").values
                total, count = total + np.where(np.isfinite(m), m, 0), count + np.isfinite(m)
        with np.errstate(invalid="ignore"):
            return total / count


loaded = OrderedDict()  # views in memory, least recently used first; the opening view stays
cache_lock = threading.Lock()  # guards `loaded` and `loading` (requests run in threads)
loading = {}  # one lock per view being read: a second request for it waits instead of reading it again


def load(west, east, south, north, start, days, variable):
    """The view's data and the seconds spent reading it (0 if it was in memory)."""
    key = (west, east, south, north, start, days, variable)
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
        names = VARIABLES[variable][1]
        sub = swot_view.subset(ds, west, east, south, north, start, end)
        if sub.sizes["time"] * sub.sizes["along"] * sub.sizes["cross"] > MAX_PIXELS:
            raise HTTPException(413, f"{sub.sizes['time']} passages is too much at once: use fewer days or a smaller box")
        data = Loaded(sub[names].load(), names, west, east)
        with cache_lock:
            loaded[key] = data
            loading.pop(key, None)
            keep = {key, tuple(DEFAULT.values())}
            while sum(d.nbytes for d in loaded.values()) > MAX_BYTES and len(loaded) > len(keep & loaded.keys()):
                del loaded[next(k for k in loaded if k not in keep)]
    return data, time.perf_counter() - t0


def view(west: float, east: float, south: float, north: float, start: str, days: int, variable: str):
    """The data of the view a request names, read again if it left memory: every request (tiles, values,
    downloads) carries the view itself, so many visitors cannot push each other's views out from under them."""
    return load(west, east, south, north, start, int(np.clip(days, 1, MAX_DAYS)), variable)[0]


app = FastAPI()
STATIC = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
def page():
    """The page, with each of our files versioned by its modification time, so browsers never keep an old one."""
    html = (STATIC / "index.html").read_text()
    for name in ("style.css", "app.js"):
        html = html.replace(f"static/{name}", f"static/{name}?v={int((STATIC / name).stat().st_mtime)}")
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})


@app.get("/api/info")
def info():
    return dict(first=str(pd.Timestamp(ds.time.values[0])), last=str(pd.Timestamp(ds.time.values[-1])),
                passages=ds.sizes["time"], passes=ds.sizes["pass_number"], max_days=MAX_DAYS, default=DEFAULT,
                variables={k: v[0] for k, v in VARIABLES.items()}, world=world()[1:])


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
    end = pd.Timestamp(start) + pd.Timedelta(days=days - 1)
    return dict(passages=data.sub.sizes["time"], seconds=round(seconds, 2),
                pixels=int(np.isfinite(data.values["none"]).sum()), quantiles=data.quantiles,
                code=swot_view.snippet(west, east, south, north, start, str(end.date()), data.names[0]))


EMPTY = io.BytesIO()
Image.new("RGBA", (256, 256)).save(EMPTY, format="png")


@app.get("/api/tile/{z}/{x}/{y}.png")
def tile(z: int, x: int, y: int, mode: str = "box", vmin: float = -0.2, vmax: float = 0.2, data=Depends(view)):
    size = WORLD / 2**z
    x0, y1 = -WORLD / 2 + x * size, WORLD / 2 - y * size
    mean = data.mean_on(mode, (x0, x0 + size), (y1 - size, y1), 256, 256)
    if not np.isfinite(mean).any():
        return Response(EMPTY.getvalue(), media_type="image/png")
    rgba = (CMAP(np.clip((mean - vmin) / (vmax - vmin), 0, 1)) * 255).astype(np.uint8)
    rgba[..., 3] = np.where(np.isfinite(mean), 255, 0)
    out = io.BytesIO()
    Image.fromarray(rgba[::-1]).save(out, format="png")  # rows from north to south
    return Response(out.getvalue(), media_type="image/png", headers={"Cache-Control": "max-age=3600"})


@app.get("/api/value")
def value(lon: float, lat: float, mode: str = "box", data=Depends(view)):
    """The value under the mouse: the mean of the passages on a ~1 km cell there."""
    x, y = mercator(lon, lat)
    v = data.mean_on(mode, (x - 500, x + 500), (y - 500, y + 500), 1, 1)[0, 0]
    return dict(value=None if np.isnan(v) else round(float(v), 4))


@app.get("/api/series")
def series(lon: float, lat: float, variable: str, radius: float = 5):
    names = VARIABLES[variable][1]
    s = sum(swot_view.point_series(ds, lon, lat, n, radius)[n] for n in names)
    return dict(time=(s.time.to_index().astype("int64") // 10**6).tolist(), value=s.values.round(4).tolist())


@app.get("/api/netcdf")
def netcdf(data=Depends(view)):
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


load(**DEFAULT)  # the opening view, in memory before the first visitor
