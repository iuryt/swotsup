---
title: SWOT SSH Viewer
emoji: 🌊
colorFrom: blue
colorTo: indigo
sdk: docker
app_port: 7860
pinned: false
---

# SWOT L3 SSH viewer

Pick a box and up to 21 days of SWOT L3 LR SSH (AVISO v3.0 Basic), see the swaths, and click a
point for its time series over the whole mission. Data are read on demand from an IceChunk store;
the "same subset in your own Python" panel shows the few lines of icechunk + xarray that do it.

- `swot_view.py`: the data recipes (subset, point time series, code snippet); icechunk + xarray only.
- `server.py`: the API and map tiles (swaths rasterized with datashader); `static/`: the MapLibre page.

Local run (from the repo root): `pixi run serve` (`slurm/app.sbatch` on biomix).
