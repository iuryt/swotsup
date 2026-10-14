# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Goal

**SWOT Sea Surface Height Explorer**: a web map over the SWOT L3 LR SSH (AVISO v3.0 Basic) IceChunk store,
for the SWOT conference (first week of November 2026), then a Hugging Face Space, and later hosted with
Earthmover (Cloudflare R2). People pick a region and up to 21 days, see the swaths, click a point for its
whole-mission time series, and copy the few lines of icechunk + xarray that give them the same data.

The store itself is built in `../swot_icechunk` (layout, recipes, benchmarks; read its CLAUDE.md and the
`swot_icechunk/store.py` docstring). This repo only reads it. It split from there in Oct 2026; the
conversation that built the app can be resumed here (`claude --resume`).

## Layout

- `app/` is the Space root (`Dockerfile`, `requirements.txt`, `README.md` with the Space front matter).
- `app/swot_view.py`: the data recipes (open the store, passes over a box, subset, point series, code
  snippet) with **icechunk + xarray only**, so the snippet users copy is the code the app runs
  (`tests/test_view.py` executes it). Moving to Earthmover's storage should only touch `open_store`.
- `app/server.py` (FastAPI): reads the store (`SWOT_STORE`, credentials from `AWS_*`) and serves
  - map tiles: each passage a curvilinear mesh rasterized with datashader (`Loaded.mean_on`), colored
    here, so only images go to the browser; overlapping passages are averaged;
  - `/api/timeline` (days with passages over the region, from the per-pass geometry, no data read),
    `/api/load`, `/api/value` (under the mouse), `/api/series`, `/api/netcdf`, `/api/world.png`.
  Loaded subsets are kept in memory (a few, LRU); the opening view (whole globe, 2 Nov 2024, 1 day,
  `DEFAULT`) is loaded when the server starts.
- `app/static/`: the page, no build step (MapLibre GL, uPlot, lucide from jsDelivr, pinned):
  map full screen without axes, floating panels, a canvas timeline (orange band of up to 21 days; the
  dragged edge stops at the limit, moving keeps the length), an Equal Earth overview (projected in JS),
  the mouse position and value, and a card tied to a clicked point by a line, with its series.

Decisions worth keeping:
- Dark only for now; basemap GEBCO grayscale from NOAA NCEI tiles (no key), darkened.
- The spatial mean of the box (or of each passage) is subtracted by default, otherwise regional sea level
  rise makes everything one color. Colors default to the 2-98 % percentiles, symmetric for anomalies.
- Longitudes of each passage are unwrapped (`continuous`) so swaths crossing 180 are not torn into quads
  spanning the world; tiles also draw each mesh shifted by ±360.
- `west == east` (e.g. -180 to 180) means the whole globe in `passes_over`.
- History: a Panel/HoloViews version came first; full-screen map, floating panels and the popup tied to
  a point were fragile there (Bokeh document locks, CSS overlays), hence MapLibre + FastAPI.

## Commands

Run everything through Slurm, never on the login node (`biomix`):

```bash
sbatch slurm/app.sbatch            # serve on a compute node, reloads on changes; tunnel command in logs/app-*.out
sbatch slurm/screenshot.sbatch     # serve and screenshot in headless chromium: img/app-*.png, copies in img/history
sbatch slurm/test.sbatch           # pytest; extra args go to pytest
sbatch -p batch -c 2 --mem 8G -t 60 -o logs/mutants-%j.out slurm/mutants.sh   # every injected bug must be "caught";
                                   # it edits app/ in place: run nothing else on the repo meanwhile
sbatch slurm/pixi_install.sbatch   # after changing dependencies (also installs chromium for playwright)
```

Look at the app through screenshots after every visible change (`scripts/screenshot.py` waits for the data
and clicks a point); `img/history` keeps one dated copy per version.

## Data

- Store on S3 (us-east-1): `s3://iuryt-shared-east/icechunk/ocean/swot_l3_lr_ssh_basic_v3_r2`, read with
  the `swot-read` AWS profile (`swot-write` can write). Local copy:
  `/mnt/maximus/data/iury/processed/swot_icechunk/basic`.

## Tests

Only tests that can fail on a real bug, each with its mutation in `slurm/mutants.sh`. Fixtures
(`tests/conftest.py`) build small fake L3 files and stores with `swot_icechunk` (an editable path
dependency); `tests/test_server.py` imports the server on such a store.
