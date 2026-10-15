# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Goal

**SWOT Sea Surface Height Explorer**: a web map over the SWOT L3 LR SSH (AVISO v3.0 Basic) IceChunk store,
for the SWOT conference (first week of November 2026), then a Hugging Face Space, and later hosted with
Earthmover (Cloudflare R2). People pick a region and up to 21 days, see the swaths, click a point for its
whole-mission time series, and copy the few lines of icechunk + xarray that give them the same data.

This is the private development copy of **Where is SWOT?** (github.com/iuryt/whereisswot, public, MIT,
DOI 10.5281/zenodo.13916363), with its full history merged in: `main` here = whereisswot's history + the
explorer. Publishing = pushing a branch to the `whereisswot` remote and opening a PR there; until the user
decides, nothing goes to that remote. Plan: the explorer gains the swath prediction of the current app
(future passes, CalVal phase, EEZ layer), e.g. the timeline continuing into the future with swath outlines
only; how data and swaths share the UI is still open. Test hosting on a private HF Space before choosing
where to run it (cost vs expected use).

The store itself is built in `../swot_icechunk` (layout, recipes, benchmarks; read its CLAUDE.md and the
`swot_icechunk/store.py` docstring). This repo only reads it. It split from there in Oct 2026; the
conversation that built the app can be resumed here (`claude --resume`).

## Layout

- `app/` holds the server and the page; `app/Dockerfile` builds them with the pixi environment `serve` (the server's
  dependencies, solved together with the default environment, so the image runs the versions we test), from a
  context with `pyproject.toml`, `pixi.lock` and `app/`. `app/README.md` has the Space front matter.
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
sbatch -o logs/ec2-%j.out slurm/ec2.sh up|status|update|down   # test server on EC2 (c7i.2xlarge, us-east-1),
                                   # profile swot-deploy; NetCDF off there (SWOT_NETCDF=0). It stops after 30 idle
                                   # minutes (deploy/boot.sh); the link to share is a Lambda URL (deploy/wake.py)
                                   # that starts it with a waiting page. Ask the user before update/up/down:
                                   # people may be using it.
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

## The current Where is SWOT? (legacy, still served)

Kept as is until the explorer replaces it: `app.py`, `tools.py`, `templates/`, `static/`, `data/external/`,
the root `Dockerfile` (the public HF Space builds it from whereisswot's `main`), environment `legacy`
(`pixi run -e legacy start`). Its notes:

### Project Overview

**Where is SWOT?** is a Flask web app that visualizes the orbit of the SWOT (Surface Water and Ocean Topography) satellite. Users select a date range and the app generates an interactive Folium map showing predicted satellite swath passes.

Hosted on Hugging Face Spaces: https://huggingface.co/spaces/iuryt/whereisswot
Redirects: `whereisswot.onrender.com` and `iuryt.github.io/whereisswot` both redirect to the HF Space.

### Running the App

Local development uses pixi:

```bash
pixi install
pixi run start                   # runs on http://0.0.0.0:4242
```

### Deployment

The app runs on **Hugging Face Spaces** (Docker SDK). The `Dockerfile` uses a pixi multi-stage build — dependencies come from `pyproject.toml` + `pixi.lock` (single source of truth). Gunicorn serves on port 7860. To deploy, push to the HF Space git remote.

Render (free tier) serves a redirect only — preserves the old `whereisswot.onrender.com` URL.

There is no test suite or linter configured.

### Architecture

The app has two Python files:

- **`tools.py`** — Data processing and map creation:
  - `process_orbit_data()` — Loads calval and science orbit shapefiles from `data/external/swot_swath/`, then generates predicted pass times for many cycles by applying fixed time deltas. Returns a single concatenated DataFrame with columns: TIME, CYCLE, ORBIT ("calval"/"science"), PASS, and geometry.
  - `create_map()` — Filters the pre-computed orbit data by a date range, groups passes by geometry, builds a Folium map colored by "days from start", optionally overlays EEZ boundaries, and adds a layer control + mouse position plugin.
  - `calculate_time()` — Helper that parses START_TIME strings from shapefiles and converts to absolute timestamps relative to a reference date.

- **`app.py`** — Flask routes:
  - `process_orbit_data()` and EEZ shapefile are loaded **once at module level** (expensive startup).
  - `POST /` validates form input, calls `create_map()`, saves the resulting Folium map to a temp file, stores the path in the session, and renders `index.html` with an iframe pointing to `/show_map`.
  - `GET /show_map` serves the temp map HTML file and deletes it after reading.

### Key Data Dependencies

- **Orbit shapefiles** (`data/external/swot_swath/`): calval and science orbit swath geometries. These are the source for all pass predictions.
- **EEZ boundaries** (`data/external/eez/eez_boundaries_v12.shp`): Exclusive Economic Zone boundaries displayed as an optional map layer. The `DOC_DATE` column is dropped during loading.
- **Processed data** (`data/processed/`): Contains exported KML files (not used by the app itself).

### Domain Concepts

- **Calval orbit**: Calibration/validation orbit phase with ~0.99349-day cycle period, starting 2023-01-15.
- **Science orbit**: Science phase with ~20.864549-day cycle period. Passes are dissolved by ID_PASS.
- Opacity values in the map depend on the number of days selected (<=4 days: higher opacity).
- Maximum user-selectable range is 100 days.
