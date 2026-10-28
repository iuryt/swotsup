# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Goal

**SWOT's Up** (the name, decided 2026-10-22; the repo is still `swot_explorer`): a web map over SWOT KaRIn low-rate (LR) sea surface height swaths
kept in an IceChunk store in the cloud, for the SWOT meeting (first week of November 2026). People pick a
region and up to 21 days, see the swaths on a globe or a flat map, click a point (its value, passages and
whole-mission time series), draw a line (values along it), and copy the few lines of icechunk + xarray that
give them the same data.

Products: without configuration the server reads **L2** LR SSH: Expert (2 km) as the base and Unsmoothed (250 m)
from zoom 9 (`SWOT_STORE` + `SWOT_PRODUCT=l3` for **L3**, AVISO/DUACS v3.0 Basic). For the meeting the plan is to show
L2 only, and add L3 if AVISO allows it: the AVISO License (Issue 20, art. 3.2) forbids
redistributing the unmodified product through a web portal without CNES's written authorization; maps,
series and lines are derived work, the NetCDF download is not (off on the public server, `SWOT_NETCDF=0`).
Asking AVISO (aviso@altimetry.fr) is pending, on the user's word. Keep the code product-neutral where it is
cheap: product names, variables and the store come from configuration, not from the page.

Hosting: an EC2 test server that sleeps when idle (`slurm/ec2.sh`); compare with a Hugging Face Space and,
later, Earthmover (Cloudflare R2) storage. Private repo github.com/iuryt/swot_explorer; work is tracked
in its issues. It started inside a copy of **Where is SWOT?** (github.com/iuryt/whereisswot, the public
swath-prediction app, untouched); on 2026-10-16 the history was rewritten to keep only the explorer.
Never push anything to whereisswot.

The stores are built in `../swot_icechunk` (layout, recipes, benchmarks; read its CLAUDE.md and the
`swot_icechunk/store.py` docstring). This repo only reads them. The conversation that built the app can
be resumed here (`claude --resume`).

## Layout

- `app/swot_view.py`: the data recipes (open the store, passes over a box, subset, point series, code
  snippet) with **icechunk + xarray only**, so the snippet users copy is the code the app runs
  (`tests/test_view.py` executes it). Moving to Earthmover's storage should only touch `open_store`.
- `app/server.py` (FastAPI): reads the store (`SWOT_STORE`, credentials from `AWS_*`) and serves
  - map tiles: each passage a curvilinear mesh rasterized with datashader (`Loaded.mean_on`), colored
    here, so only images go to the browser; overlapping passages are averaged; tile URLs carry a hash of
    the server code, so browsers never keep tiles of an older version. A view is kept as float32 values
    (~28 B/pixel), the modes as one number subtracted (medians: a few L2 pixels pass their flag km off), colors
    from a 4 M-pixel sample. Where a screen pixel (on the ground) is wider than a cell, everything in view is drawn
    as datashader points in one call: 4x4 / 16x16 block means (sorted by y, a tile takes its band), or the cells;
    closer, each mesh in view (found from an array of their bounds) as a quadmesh. 12 days of the whole globe in
    L2 Expert (228 M px) take ~7 GB; a tile ~0.06-0.2 s in process (a server busy with many is GIL-bound);
  - detail tiles (`/api/detail`, SWOT_DETAIL_STORE, e.g. Unsmoothed over Expert): from SWOT_DETAIL_ZOOM - 1 on,
    read tile by tile, chunks of lines of every passage at once (LRU cache), only the lines whose latitudes reach
    the tile, shifted by what the base view subtracts so colors match; the page fades them in over a zoom level,
    the base showing where they have none. A first look at a place reads ~5 s from Arraylake, then ~0.2 s. Once a
    region's view is loaded, its detail is read ahead in the background (`read_ahead`, if it fits in half the
    SWOT_DETAIL_BLOCKS kept, default 1024 ~3.5 GB): zooming in then finds it in memory (12 tiles in 0.7 s);
  - reading a view: envelopes are kept in memory (`open_product`; the detail store takes the base's), and `read`
    fetches one array and passage at a time, all at once: a vectorized read of a subset fetches its chunks nearly one
    by one (the Gulf Stream, 12 days of Expert: ~35 s -> 1.6 s from biomix). From AWS the stores read ~10x faster
    than from biomix (Unsmoothed, 2 days of the Gulf Stream, 644 MB: 0.6 s on a c7i.2xlarge in us-east-1);
  - L2 products keep values only where `ssha_karin_2_qual == 0` (over land and ice they reach kilometres);
  - `/api/timeline` (days with passages over the region, from the per-pass geometry, no data read),
    `/api/load`, `/api/value` (at a click: the value and its passages, with the time of their nearest line),
    `/api/series`, `/api/transect` (along a drawn polyline: points every 2 km on great circles, each passage's
    nearest pixel within 1.5 km, so the nadir gap stays empty; each value with its line's time),
    `/api/hovmoller` (the same line over the whole mission: each passage's nearest pixels, found once per pass
    since its grid and line times are the same every cycle, then every passage's pixels in one vectorized read,
    each chunk fetched once: the equatorial Pacific, 12k passages, 18 s from biomix instead of 80 s),
    `/api/netcdf`, `/api/world.png`.
  Every request names its view (box, dates, variable); views are kept in memory up to `SWOT_CACHE_GB`
  and read again if they were pushed out. The opening view (`DEFAULT`): for L2 Expert the whole globe on the latest
  complete cycle (`latest_cycle`, ~21 days, ~400 M pixels: too big to keep), served by an `Overview`: built in the
  background at start, a batch of 32 passages at a time (from biomix 123 s, 4.9 GB at most), into world rasters of
  zoom 4 (sum and count of every cell as a point, per mode: the colors and modes change without reading again), kept
  on disk (`SWOT_CACHE_DIR`, by commit and view: a restart reads them back); tiles up to zoom 4 come from them, closer
  tiles, clicks and lines from the passages read a chunk at a time where needed (`from_blocks`, `base_block`, LRU
  `SWOT_BASE_BLOCKS`). For L3, the whole globe on 2 Nov 2024 (1 day), loaded at start.
- `app/static/`: the page, no build step (MapLibre GL 5, uPlot, lucide, Prism from CDNs, pinned): map full
  screen without axes, globe by default with a button for the flat map (remembered per browser), floating
  panels, a canvas timeline (orange band of up to 21 days; the dragged edge stops at the limit, moving keeps
  the length), an Equal Earth overview on the flat map, the mouse position, a card tied to a clicked point
  (value, passages, time series on request; clicking a passage moves the map to its date), and a card for a
  drawn line: its profile on the selected dates (one curve per passage; the hover is a table, a marker follows it
  along the line) or its Hovmöller (distance x time over the whole mission, each passage a row at its own time,
  reaching halfway to the next passage at most 4 px, so it looks filled until zoomed; minus the mean over time at
  each point by default (points with few passages left empty), or as stored (done in the page, no new read);
  box zoom, wheel on the time, double-click back; a click moves the map to that passage's date).
  Each chart card has buttons for its figure (PNG, from the chart's canvas), its numbers (NetCDF:
  `/api/series.nc`, `/api/transect.nc`, `/api/hovmoller.nc`, under the same switch as the region's NetCDF) and the
  code that reproduces it (`/api/code/series`, `/api/code/line`: `swot_view.series_snippet` / `line_snippet`,
  icechunk + xarray + matplotlib (+ scipy for lines), run by the tests).
  Small screens (`max-width: 640px` or `max-height: 500px`, `narrow()` in app.js), after Windy: the name as a
  small label top left (with About; the full panel opens as a sheet from the layers icon), the options as icons
  bottom left, the color bar and the timeline at the bottom. The timeline has no grips there: a finger on the band
  moves it, elsewhere draws a new one; buttons move it by its length or change it by a day, the date opens the
  calendar, two fingers zoom. The cards are 230 px sheets in the color bar's place, made tall by their grab bar.
  Touch draws the box, taps place a line's points and Done ends it (no double click on a touch screen), a finger
  moves the charts' cursor. While drawing, a bar shows what to do, with Undo / Done / Cancel (on the desktop too).
- `Dockerfile`: the server with the pixi environment `serve` (the server's dependencies, solved together
  with the default environment, so the image runs the versions we test).
- `deploy/boot.sh` (runs on the EC2 instance at every boot: build, serve behind Caddy, stop after 30 idle
  minutes) and `deploy/wake.py` (the Lambda link: starts the instance and shows a waiting page).
- `scripts/screenshot.py` (scenarios: open, click, series, line (and line-250m), hovmoller, flat, dateline, phone, wide, zoom), `scripts/bench_server.py`.

Decisions worth keeping:
- Dark only for now; basemap GEBCO grayscale from NOAA NCEI tiles (no key), darkened.
- The spatial mean of the box (or of each passage) is subtracted by default, otherwise regional sea level
  rise makes everything one color. Colors default to the 2-98 % percentiles, symmetric for anomalies.
- Longitudes of each passage are unwrapped (`continuous`) so swaths crossing 180 are not torn into quads
  spanning the world; tiles also draw each mesh shifted by ±360. Each run of lines with data is its own
  mesh, so no quads are drawn across land.
- `west == east` (e.g. -180 to 180) means the whole globe in `passes_over`.
- History: a Panel/HoloViews version came first; full-screen map, floating panels and the popup tied to
  a point were fragile there (Bokeh document locks, CSS overlays), hence MapLibre + FastAPI.

## Workflow

Work is tracked in the repo's issues. For each issue:
1. A branch named after it (`12-time-order`), from `main`.
2. Commits with tests, mutations and screenshots as usual (screenshots into `img/history` from the
   branch's commits).
3. A PR with "Closes #N": what changed, the test and mutation results, the screenshot paths.
4. The version goes up in the PR (`pyproject.toml`, semver while in 0.x): a feature bumps the minor
   (0.1.0 -> 0.2.0), a fix the patch (0.2.0 -> 0.2.1); docs-only changes keep it.
5. Merge only after the user's ok, with a **merge commit** (not squash: `img/history` names carry the
   branch's commit hashes), then tag `vX.Y.Z` on `main`.
6. The EC2 server gets only what is on `main`, and only when the user asks.

## Commands

Run everything through Slurm, never on the login node (`biomix`):

```bash
sbatch slurm/app.sbatch            # serve on a compute node (reloads by polling); tunnel command in logs/app-*.out
sbatch slurm/screenshot.sbatch     # serve and run every screenshot scenario: img/app-*.png, copies in img/history
sbatch slurm/screenshot_versions.sbatch HASH...   # screenshots of past commits into img/history
sbatch slurm/test.sbatch           # pytest; extra args go to pytest
sbatch slurm/mutants.sh [PATTERN]  # every injected bug (tests/mutations.sh) must be "caught"; each in its own copy,
                                   # 8 at once (~4 min for all); PATTERN: only the labels matching (a bash regex)
sbatch slurm/pixi_install.sbatch   # after changing dependencies (also installs chromium for playwright)
sbatch -c 8 -o logs/bench-%j.out slurm/run.sbatch scripts/bench_server.py   # timings (AWS_PROFILE=swot-read)
sbatch -o logs/ec2-%j.out slurm/ec2.sh up|status|update|down   # test server on EC2 (c7i.2xlarge, us-east-1),
                                   # profile swot-deploy. Ask the user before update/up/down: people may be using it
```

Screenshots: after every visible change, take them with `slurm/screenshot.sbatch` and give the user their
paths. `img/history` keeps a dated copy of every version (`<date>_<commit>_app-<scenario>.png`); never
delete or overwrite anything there.

Gotchas on biomix: files edited on the login node reach the compute nodes over NFS without file events
(hence polling reload) and with cached sizes for a few seconds (a page loaded right after an edit can come
truncated: load it again). Several jobs starting `pixi run` right after `pixi install` can collide.

## Data

- L3 store on Arraylake (Earthmover, Cloudflare R2), private, icechunk format v2:
  `SWOT_STORE=arraylake:computational-oceanography-udel/swot-l3-lr-ssh-basic-v3`; the arraylake client vends
  the R2 credentials from a token (`arraylake auth login` on biomix, `ARRAYLAKE_TOKEN` on a server). The EC2
  server reads it when the token is in SSM Parameter Store (`/swot-explorer/arraylake-token`, put there by the
  user), else the S3 copy. From biomix it was slower than S3 (whole globe, one day: 16 s vs 5 s, 2026-10-20).
- L2 Expert (2 km, the whole mission, envelopes) on Arraylake: `arraylake:computational-oceanography-udel/swot-l2-lr-ssh-
  expert-d`, the default (`SWOT_PRODUCT=expert`), with the Unsmoothed store as `SWOT_DETAIL_STORE`: the map turns into
  250 m from zoom 9. The EC2 server reads them with the Arraylake token (`deploy/boot.sh`; without it, the L3 copy on S3).
  12 global days need `SWOT_MAX_PIXELS` ~250e6 (from biomix: read ~50-170 s, build ~30 s).
- L2 Unsmoothed (250 m) on Arraylake, being uploaded (Oct 2026): `SWOT_STORE=arraylake:computational-oceanography-
  udel/swot-l2-lr-ssh-unsmoothed-d SWOT_PRODUCT=unsmoothed` (needs `pcodec`). The server pins the latest commit at start
  (shown in the subtitle; restart for a newer one), keeps only the passages with a `line_time` chunk (listing them reads
  no data), fills the envelopes not written yet from each pass's nadir, and opens on the latest day, 6 x 6 degrees
  around a passage. Its nadir gap and edge pixels have no position: `across()` interpolates the positions (only)
  across each line so the meshes have every corner. 64 times L3's pixels: small regions only (MAX_PIXELS).
- L3 store on S3 (us-east-1): `s3://iuryt-shared-east/icechunk/ocean/swot_l3_lr_ssh_basic_v3_r2`, read with
  the `swot-read` AWS profile (`swot-write` can write). Local copy:
  `/mnt/maximus/data/iury/processed/swot_icechunk/basic`.
- L2: being prepared in `../swot_icechunk` (samples, L2 vs L3 comparison in its `tools/l2_l3`).

## Tests

Only tests that can fail on a real bug, each with its mutation in `tests/mutations.sh` (`slurm/mutants.sh` runs them
in copies of app/ and tests/ with swot_cloud as of its last commit, so the repository is never edited). Fixtures
(`tests/conftest.py`) build small fake L3 files and stores with `swot_icechunk` (an editable path
dependency), once per session (tests only read them); `tests/test_server.py` imports the server on such a store.
