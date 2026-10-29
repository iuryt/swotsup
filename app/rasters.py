"""Daily world rasters (#98): each day's L2 Expert passages summed into Web Mercator rasters of zooms 0-4, in layers
that add up, so that any window of days is the sum of its days: the whole globe on any dates at once.

Per zoom z, in chunks of a map tile (256 x 256) and one day:
- `z{z}/count` (day, surface, y, x), uint16: how many 2 km cells fall in each pixel, for the open ocean (surface
  class 0) and for the other water (rivers, lakes);
- `z{z}/mean` (day, layer, y, x), int16 in mm: the mean of ssha_karin_2_xover, of the DTU25 correction and of the
  calibration, for each surface (LAYERS). A window's mean is sum(count x mean) / sum(count): SSHA, plus the DTU25
  correction if chosen, minus the calibration if chosen, over the open ocean (and the other water if chosen).
Rows run from the south (as datashader's); day 0 is DAY0, so days are written independently (several jobs at once).

    python app/rasters.py OUT create            # the arrays (once, before builds run at once)
    python app/rasters.py OUT 2026-10-01 21     # build those days (each day: its passages read, binned, written)
"""
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pandas as pd
import zarr

import swot_view

ZOOM, TILE = 4, 256
DAY0, DAYS = np.datetime64("2023-01-01"), 1826  # the arrays' first day and length (to the end of 2027)
LAYERS = ("ssha", "dtu25", "calibration")  # the means, for each of the two surfaces
STORE = "arraylake:computational-oceanography-udel/swot-l2-lr-ssh-expert-d"
R = 6378137.0
WORLD = 2 * np.pi * R


def calibration(bias, slope, cross):
    """The calibration to subtract: bias + slope x cross / 60 of the pixel's side (west: cross < 0, east), `cross`
    its distance to nadir (km). `bias`, `slope`: (..., 2), each line's two sides; `cross`: broadcastable to (..., n)."""
    cross = np.asarray(cross, float)
    side = np.broadcast_to((cross > 0).astype(int), bias.shape[:-1] + cross.shape[-1:])
    return np.take_along_axis(bias, side, -1) + np.take_along_axis(slope, side, -1) * cross / 60


def create(out):
    """The arrays (metadata only), if not there yet."""
    root = zarr.open_group(out, mode="a")
    codec = zarr.codecs.BloscCodec(cname="zstd", clevel=7, shuffle="shuffle")
    for z in range(ZOOM + 1):
        size = TILE * 2**z
        for name, n, dtype in (("count", 2, "uint16"), ("mean", 2 * len(LAYERS), "int16")):
            if f"z{z}/{name}" not in root:
                root.create_array(f"z{z}/{name}", shape=(DAYS, n, size, size), dtype=dtype, chunks=(1, n, TILE, TILE),
                                  fill_value=0, compressors=codec, config={"write_empty_chunks": False})
    return root


def passage(ds, i):
    """One passage's pixels: lon, lat, SSHA, the DTU25 correction, the calibration and the surface class."""
    p = int(ds.pass_id.values[i])
    a = ds[["ssha_karin_2_xover", "latitude", "longitude", "cal_bias", "cal_slope"]].isel(time=i).load()
    s = ds[["mss_cor_dtu25", "ancillary_surface_classification_flag"]].sel(pass_number=p).load()
    cal = calibration(a.cal_bias.values, a.cal_slope.values, ds.cross.values)
    return (a.longitude.values, a.latitude.values, a.ssha_karin_2_xover.values, s.mss_cor_dtu25.values, cal,
            s.ancillary_surface_classification_flag.values)


def day_sums(ds, times):
    """A day's counts (2, size, size) and sums (6, size, size) at ZOOM, float64, rows from the south."""
    size = TILE * 2**ZOOM
    count, sums = np.zeros((2, size, size)), np.zeros((2 * len(LAYERS), size, size))
    with ThreadPoolExecutor(32) as pool:
        for lon, lat, ssha, dtu, cal, surface in pool.map(lambda i: passage(ds, i), times):
            ok = np.isfinite(ssha) & np.isfinite(lon) & np.isfinite(lat) & np.isfinite(dtu) & np.isfinite(cal)
            x = R * np.deg2rad((lon[ok] + 180) % 360 - 180)
            y = R * np.log(np.tan(np.pi / 4 + np.deg2rad(np.clip(lat[ok], -85, 85)) / 2))
            col = np.clip(np.floor((x + WORLD / 2) / WORLD * size).astype(int), 0, size - 1)
            row = np.clip(np.floor((y + WORLD / 2) / WORLD * size).astype(int), 0, size - 1)
            flat, ocean = row * size + col, surface[ok] == 0
            for k, keep in enumerate((ocean, ~ocean)):
                count[k] += np.bincount(flat[keep], None, size * size).reshape(size, size)
                for j, w in enumerate((ssha[ok], dtu[ok], cal[ok])):
                    sums[k * len(LAYERS) + j] += np.bincount(flat[keep], w[keep], size * size).reshape(size, size)
    return count, sums


def coarser(a):
    """The next zoom out: each 2 x 2 block summed (sums and counts add up)."""
    n = a.shape[-1] // 2
    return a.reshape(a.shape[0], n, 2, n, 2).sum((2, 4))


def build(out, start, days):
    root, ds = zarr.open_group(out, mode="r+"), swot_view.open_store(STORE)  # the arrays made before (`create`)
    t = ds.time.values
    for d in range(days):
        day = np.datetime64(start) + np.timedelta64(d, "D")
        times = np.flatnonzero((t >= day) & (t < day + np.timedelta64(1, "D")))
        t0 = time.time()
        count, sums = day_sums(ds, times)
        k = int((day - DAY0) / np.timedelta64(1, "D"))
        for z in range(ZOOM, -1, -1):
            with np.errstate(invalid="ignore", divide="ignore"):
                mean = sums / np.repeat(count, len(LAYERS), axis=0)
            root[f"z{z}/count"][k] = np.minimum(count, 65535).astype("uint16")
            root[f"z{z}/mean"][k] = np.where(np.isfinite(mean), np.round(mean * 1000).clip(-32767, 32767), 0).astype("int16")
            if z:
                count, sums = coarser(count), coarser(sums)
        print(f"{day}: {len(times)} passages in {time.time() - t0:.0f} s", flush=True)


class Rasters:
    """The rasters of a store, read a tile at a time."""

    def __init__(self, path):
        self.root = zarr.open_group(path, mode="r")
        # the days built: z0 is one chunk a day, a file named by the day (a local store)
        built = np.array(sorted(int(d) for d in os.listdir(os.path.join(path, "z0", "count", "c"))), int)
        self.days = set(built.tolist())
        self.first = DAY0 + np.timedelta64(int(built.min()), "D") if len(built) else None
        self.last = DAY0 + np.timedelta64(int(built.max()), "D") if len(built) else None

    def covers(self, start, days):
        """Whether every day of a window is built."""
        k = int((np.datetime64(start) - DAY0) / np.timedelta64(1, "D"))
        return all(d in self.days for d in range(k, k + days))

    def mean(self, z, rows, cols, start, days, dtu=True, cal=True, water=False, counts=False):
        """The window's mean on a block of zoom z's raster (rows from the south), NaN where nothing (`counts`: and how
        many 2 km cells each pixel has)."""
        k = int((np.datetime64(start) - DAY0) / np.timedelta64(1, "D"))
        sel = (slice(k, k + days), slice(None), rows, cols)
        count = self.root[f"z{z}/count"].get_orthogonal_selection(sel).astype("float64")  # (days, 2, ...)
        mean = self.root[f"z{z}/mean"].get_orthogonal_selection(sel).astype("float64") / 1000  # (days, 6, ...)
        signs = np.array([1.0, 1.0 if dtu else 0.0, -1.0 if cal else 0.0])
        total = n = 0
        for s in (0, 1) if water else (0,):
            value = np.tensordot(signs, mean[:, s * len(LAYERS):(s + 1) * len(LAYERS)], ([0], [1]))  # (days, ...)
            total, n = total + (value * count[:, s]).sum(0), n + count[:, s].sum(0)
        with np.errstate(invalid="ignore", divide="ignore"):
            m = np.where(n > 0, total / n, np.nan)
        return (m, n) if counts else m


    def pyramid(self, start, days, dtu=True, cal=True, water=False):
        """The window's means at every zoom ({z: (size, size) float32, rows from the south}), from ZOOM's days summed
        one at a time (each day's arrays read whole: a tile's would read each chunk on its own)."""
        k = int((np.datetime64(start) - DAY0) / np.timedelta64(1, "D"))
        signs = np.array([1.0, 1.0 if dtu else 0.0, -1.0 if cal else 0.0], "float32")
        total = n = None
        for d in range(k, k + days):
            count = self.root[f"z{ZOOM}/count"][d].astype("float32")
            mean = self.root[f"z{ZOOM}/mean"][d].astype("float32") / 1000
            for s in (0, 1) if water else (0,):
                v = np.tensordot(signs, mean[s * len(LAYERS):(s + 1) * len(LAYERS)], 1) * count[s]
                total, n = (v, count[s]) if total is None else (total + v, n + count[s])
        out = {}
        for z in range(ZOOM, -1, -1):
            with np.errstate(invalid="ignore", divide="ignore"):
                out[z] = np.where(n > 0, total / n, np.nan).astype("float32")
            if z:
                total, n = coarser(total[None])[0], coarser(n[None])[0]
        return out


if __name__ == "__main__":
    create(sys.argv[1]) if sys.argv[2] == "create" else build(sys.argv[1], sys.argv[2], int(sys.argv[3]))
