"""Time what a visitor waits for, in-process (no browser): server start, loads that miss the cache,
a point series and map tiles. Run it with the CPUs of the target machine (e.g. sbatch -c 2 for a free
Hugging Face Space); ZARR_ASYNC__CONCURRENCY sets how many chunks zarr fetches at once (default 10).
Usage: bench_server.py"""
import os
import sys
import time

sys.path.insert(0, "app")
t = time.perf_counter()
import server  # opens the store and loads the opening view
import zarr
from fastapi.testclient import TestClient

client = TestClient(server.app)
print(f"cpus {os.cpu_count()} (allowed {len(os.sched_getaffinity(0))}), zarr concurrency "
      f"{zarr.config.get('async.concurrency')}", flush=True)
print(f"start (open store + whole globe, 1 day): {time.perf_counter() - t:.1f} s", flush=True)

CASES = [("whole globe, 1 day", dict(west=-180, east=180, south=-90, north=90, start="2024-11-03", days=1)),
         ("Indian Ocean 15x13, 21 days", dict(west=55, east=70, south=-20, north=-7, start="2024-06-01", days=21)),
         ("Gulf Stream 15x10, 7 days", dict(west=-75, east=-60, south=32, north=42, start="2025-06-01", days=7))]
for name, box in CASES:
    t = time.perf_counter()
    d = client.get("/api/load", params=box | dict(variable="ssha_unfiltered")).json()
    print(f"load {name}: {time.perf_counter() - t:.1f} s, {d['passages']} passages, {d['pixels'] / 1e6:.1f} M pixels",
          flush=True)

t = time.perf_counter()
s = client.get("/api/series", params=dict(lon=-59.3, lat=-40.5, variable="ssha_unfiltered")).json()
print(f"point series, whole mission: {time.perf_counter() - t:.1f} s, {len(s['time'])} passages", flush=True)

# tiles of the whole globe on one day, as a first screen asks for them (zoom 2: 16 tiles; zoom 4: a region)
d = client.get("/api/load", params=CASES[0][1] | dict(variable="ssha_unfiltered")).json()
for z, tiles in [(2, [(x, y) for x in range(4) for y in range(4)]), (4, [(x, y) for x in range(8, 12) for y in range(5, 9)])]:
    t = time.perf_counter()
    for x, y in tiles:
        client.get(f"/api/tile/{d['id']}/{z}/{x}/{y}.png")
    dt = time.perf_counter() - t
    print(f"tiles zoom {z}: {len(tiles)} in {dt:.1f} s ({dt / len(tiles) * 1000:.0f} ms each, one after another)", flush=True)
