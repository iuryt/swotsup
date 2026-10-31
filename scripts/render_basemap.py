"""The page's basemap from GEBCO (the GEBCO_2026 grid, 15 arc-seconds, public domain with attribution): shaded relief
in gray, land lighter than the sea, as Web Mercator tiles of zooms 0-ZOOM (256 px, WebP) in one MBTiles file (SQLite),
which the server serves (SWOT_BASEMAP) instead of fetching tiles from elsewhere at every view.

    python scripts/render_basemap.py GEBCO_2026.nc OUT.mbtiles [ZOOM] [WORKERS]
Each zoom samples a block mean of the grid about its pixels' size (no aliasing in the shading); workers are forked
and share the grid (~7.5 GB in memory).
"""
import io
import multiprocessing as mp
import sqlite3
import sys
import time

import numpy as np
import xarray as xr
from PIL import Image
from scipy.ndimage import map_coordinates

R, TILE, CELL = 6378137.0, 256, 1 / 240  # GEBCO: 15 arc-seconds
LEVELS = {}  # 2**k cells averaged -> (elevation, its first cell centre lat, lon, cell size), shared with the workers


def coarser(a, f):
    """Block means of f x f cells (a's sides divisible by f)."""
    return a.reshape(a.shape[0] // f, f, a.shape[1] // f, f).mean((1, 3), dtype="float32")


def shade(z, dx, exaggeration):
    """Hillshade (sun from the north-west, 45 degrees up) of elevations `z` (rows from the north) on pixels `dx` m wide."""
    gy, gx = np.gradient(z * exaggeration, dx)
    slope = np.arctan(np.hypot(gx, gy))
    aspect = np.arctan2(-gx, gy)
    az, alt = np.deg2rad(315), np.deg2rad(45)
    return np.clip(np.sin(alt) * np.cos(slope) + np.cos(alt) * np.sin(slope) * np.cos(az - aspect), 0, 1)


def tile(z, x, y):
    """One tile's WebP bytes (y from the north, as the page asks for them)."""
    n = TILE * 2**z
    k = max(0, int(np.floor(np.log2((360 / n) / CELL))))  # cells averaged: about a pixel's size at the equator
    elev, lat0, lon0, cell = LEVELS[min(k, max(LEVELS))]
    # pixel centres, with a one-pixel margin for the gradient
    py = (y * TILE + np.arange(-1, TILE + 1) + 0.5) / n
    px = (x * TILE + np.arange(-1, TILE + 1) + 0.5) / n
    lat = np.rad2deg(np.arctan(np.sinh(np.pi * (1 - 2 * py))))
    lon = px * 360 - 180
    rows = (lat0 - lat) / cell  # GEBCO rows run from the south: lat0 is the first row's, rows counted down
    cols = ((lon - lon0) / cell) % elev.shape[1]
    rr, cc = np.meshgrid(rows, cols, indexing="ij")
    h = map_coordinates(elev, [rr, cc], order=1, mode="nearest").astype("float32")
    dx = 2 * np.pi * R * np.cos(np.deg2rad(lat))[:, None] / n  # a mercator pixel's width on the ground
    hs = shade(h, np.broadcast_to(dx, h.shape).mean(), 2.0 + 0.8 * (9 - min(z, 9)))[1:-1, 1:-1]
    h = h[1:-1, 1:-1]
    sea = h < 0
    # the sea a little darker with depth, the land lighter; the relief on both
    base = np.where(sea, 0.62 - 0.12 * np.clip(-h / 6000, 0, 1), 0.82)
    g = np.clip(base * (0.55 + 0.6 * hs), 0, 1)
    buf = io.BytesIO()
    Image.fromarray((g * 255).astype("uint8"), "L").save(buf, "WEBP", quality=82, method=4)
    return buf.getvalue()


def rows_of(args):
    z, y = args
    return [(z, x, y, tile(z, x, y)) for x in range(2**z)]


def main(src, out, zoom=9, workers=32):
    t0 = time.time()
    ds = xr.open_dataset(src)
    elev = ds.elevation.values.astype("float32")  # (lat, lon) from the south-west
    lat0, lon0 = float(ds.lat[0]), float(ds.lon[0])
    print(f"GEBCO {elev.shape} read in {time.time() - t0:.0f} s", flush=True)
    LEVELS[0] = (elev, lat0, lon0, CELL)
    a = elev
    for k in range(1, 9):  # 2, 4 ... 256 cells averaged; the first cell centre of a block is the block's centre
        a = coarser(a[: a.shape[0] // 2 * 2, : a.shape[1] // 2 * 2], 2)
        LEVELS[k] = (a, lat0 + (2**k - 1) * CELL / 2, lon0 + (2**k - 1) * CELL / 2, CELL * 2**k)
    # rows count down from the north in `tile`: flip each level so row 0 is the northernmost
    for k, (a, la0, lo0, c) in list(LEVELS.items()):
        LEVELS[k] = (a[::-1].copy(), la0 + (a.shape[0] - 1) * c, lo0, c)
    db = sqlite3.connect(out)
    db.executescript("""DROP TABLE IF EXISTS tiles; DROP TABLE IF EXISTS metadata;
        CREATE TABLE metadata (name TEXT, value TEXT);
        CREATE TABLE tiles (zoom_level INTEGER, tile_column INTEGER, tile_row INTEGER, tile_data BLOB);""")
    db.executemany("INSERT INTO metadata VALUES (?, ?)", [
        ("name", "GEBCO_2026 shaded relief, gray"), ("format", "webp"), ("minzoom", "0"), ("maxzoom", str(zoom)),
        ("attribution", "GEBCO Compilation Group (2026) GEBCO_2026 Grid (doi:10.5285/4f68d5c7-45eb-f999-e063-7086abc036fa)")])
    with mp.get_context("fork").Pool(workers) as pool:
        for z in range(zoom + 1):
            t1, count = time.time(), 0
            for row in pool.imap_unordered(rows_of, [(z, y) for y in range(2**z)]):
                # MBTiles rows count from the south (TMS)
                db.executemany("INSERT INTO tiles VALUES (?, ?, ?, ?)", [(z, x, 2**z - 1 - y, b) for z, x, y, b in row])
                count += len(row)
            db.commit()
            print(f"zoom {z}: {count} tiles in {time.time() - t1:.0f} s", flush=True)
    db.execute("CREATE UNIQUE INDEX tile_index ON tiles (zoom_level, tile_column, tile_row)")
    db.commit()
    db.close()
    print(f"done in {time.time() - t0:.0f} s", flush=True)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2], *(int(v) for v in sys.argv[3:5]))
