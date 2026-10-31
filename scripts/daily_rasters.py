"""Prototype of daily world rasters (#98): each day's L2 Expert passages summed into Web Mercator rasters of zooms
0-4, in layers that add up (a count and sums of SSHA, the DTU25 correction and the calibration, for the open ocean
and for the other water), so that any window of days is the sum of its days.

    python scripts/daily_rasters.py build 2026-10-01 3 OUT   # 3 days from 2026-10-01 into the zarr OUT
    python scripts/daily_rasters.py bench OUT                 # serve tiles of a 21-day window (the days repeated)
"""
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd
import zarr

sys.path.insert(0, str(Path(__file__).parent.parent / "app"))
import swot_view  # noqa: E402

zarr.config.set({"async.concurrency": 256})
STORE = "arraylake:computational-oceanography-udel/swot-l2-lr-ssh-expert-d"
ZOOM, TILE = 4, 256
LAYERS = ["count", "ssha", "dtu25", "calibration"]  # x 2: the open ocean (surface class 0), the other water
R = 6378137.0
WORLD = 2 * np.pi * R


def calibration(bias, slope, cross):
    """bias + slope x cross / 60 of each pixel's side of nadir (as the server's)."""
    side = np.broadcast_to((cross > 0).astype(int), bias.shape[:-1] + cross.shape[-1:])
    return np.take_along_axis(bias, side, -1) + np.take_along_axis(slope, side, -1) * cross / 60


def passage(ds, i):
    """One passage's pixels: lon, lat, SSHA, the DTU25 correction, the calibration and the surface class."""
    p = int(ds.pass_id.values[i])
    a = ds[["ssha_karin_2_xover", "latitude", "longitude", "cal_bias", "cal_slope"]].isel(time=i).load()
    s = ds[["mss_cor_dtu25", "ancillary_surface_classification_flag"]].sel(pass_number=p).load()
    cal = calibration(a.cal_bias.values, a.cal_slope.values, ds.cross.values)
    return (a.longitude.values, a.latitude.values, a.ssha_karin_2_xover.values, s.mss_cor_dtu25.values, cal,
            s.ancillary_surface_classification_flag.values)


def day_raster(ds, times):
    """A day's layers at ZOOM: (8, size, size) float32, rows from the south."""
    size = TILE * 2**ZOOM
    out = np.zeros((2 * len(LAYERS), size, size), "float32")
    with ThreadPoolExecutor(32) as pool:
        for lon, lat, ssha, dtu, cal, surface in pool.map(lambda i: passage(ds, i), times):
            ok = np.isfinite(ssha) & np.isfinite(lon) & np.isfinite(lat) & np.isfinite(dtu) & np.isfinite(cal)
            x = R * np.deg2rad((lon[ok] + 180) % 360 - 180)
            y = R * np.log(np.tan(np.pi / 4 + np.deg2rad(np.clip(lat[ok], -85, 85)) / 2))
            col = np.floor((x + WORLD / 2) / WORLD * size).astype(int)
            row = np.floor((y + WORLD / 2) / WORLD * size).astype(int)
            flat = np.clip(row, 0, size - 1) * size + np.clip(col, 0, size - 1)
            ocean = surface[ok] == 0
            for k, keep in enumerate((ocean, ~ocean)):
                for j, w in enumerate((None, ssha[ok], dtu[ok], cal[ok])):
                    out[k * len(LAYERS) + j] += np.bincount(flat[keep], None if w is None else w[keep],
                                                            size * size).reshape(size, size)
    return out


def coarser(a):
    """The next zoom out: each 2 x 2 block summed (sums and counts add up)."""
    n = a.shape[-1] // 2
    return a.reshape(a.shape[0], n, 2, n, 2).sum((2, 4))


def build(start, days, out):
    ds = swot_view.open_store(STORE)
    t = pd.to_datetime(ds.time.values)
    root = zarr.open_group(out, mode="w")
    arrays = {z: root.create_array(f"z{z}", shape=(days, 2 * len(LAYERS), TILE * 2**z, TILE * 2**z), dtype="float32",
                                   chunks=(1, 2 * len(LAYERS), TILE, TILE), fill_value=0.0,
                                   compressors=zarr.codecs.BloscCodec(cname="zstd", clevel=5, shuffle="shuffle"),
                                   config={"write_empty_chunks": False})
              for z in range(ZOOM + 1)}
    for d in range(days):
        day = pd.Timestamp(start) + pd.Timedelta(days=d)
        times = np.flatnonzero((t >= day) & (t < day + pd.Timedelta(days=1)))
        t0 = time.time()
        a = day_raster(ds, times)
        t1 = time.time()
        for z in range(ZOOM, -1, -1):
            arrays[z][d] = a
            a = coarser(a) if z else a
        print(f"{day.date()}: {len(times)} passages, read and binned in {t1 - t0:.0f} s, written in {time.time() - t1:.1f} s, "
              f"{int((arrays[ZOOM][d][0] > 0).sum())} ocean pixels at z{ZOOM}", flush=True)
    sizes = {z: sum(f.stat().st_size for f in Path(out, f"z{z}").rglob("*") if f.is_file()) for z in range(ZOOM + 1)}
    print("bytes per day by zoom:", {z: f"{s / days / 1e6:.2f} MB" for z, s in sizes.items()},
          f"total per day {sum(sizes.values()) / days / 1e6:.2f} MB", flush=True)


def tile(root, z, x, y, days, dtu=True, cal=True, water=False):
    """The mean of a map tile (rows from the north, as the map) over `days` (their indices): a read of one chunk a day."""
    a = root[f"z{z}"]
    n = 2**z
    rows = slice((n - 1 - y) * TILE, (n - y) * TILE)  # rows from the south in the rasters
    s = a.get_orthogonal_selection((days, slice(None), rows, slice(x * TILE, (x + 1) * TILE))).sum(0)
    signs = np.array([0, 1, 1 if dtu else 0, -1 if cal else 0], "float32")
    count, total = s[0], np.tensordot(signs, s[:4], 1)
    if water:
        count, total = count + s[4], total + np.tensordot(signs, s[4:], 1)
    with np.errstate(invalid="ignore", divide="ignore"):
        return (total / count)[::-1]


def bench(out):
    root = zarr.open_group(out, mode="r")
    days = root["z0"].shape[0]
    window = np.arange(21) % days  # a 21-day window, the days built repeated
    for z, (x, y) in ((0, (0, 0)), (2, (1, 1)), (3, (2, 3)), (4, (4, 6))):
        t0 = time.time()
        for _ in range(5):
            m = tile(root, z, x, y, window)
        print(f"tile z{z}: {(time.time() - t0) / 5 * 1000:.0f} ms, {int(np.isfinite(m).sum())} pixels with data, "
              f"mean {np.nanmean(m):.3f} m", flush=True)


if __name__ == "__main__":
    if sys.argv[1] == "build":
        build(sys.argv[2], int(sys.argv[3]), sys.argv[4])
    else:
        bench(sys.argv[2])
