# SWOT Sea Surface Height Explorer

A web map over the SWOT L3 LR SSH (AVISO v3.0 Basic) data, read on demand from an IceChunk store in the
cloud: every swath since July 2023, without downloading files.

- Pick a region and up to 21 days on the timeline; see the swaths on a globe or a flat map.
- Click a point: its value, the passages there (pass, cycle, time), and the time series over the whole mission.
- Draw a line: the values along it, one curve per passage.
- Copy the few lines of icechunk + xarray that give you the same data in your own Python.

The store is built in [swot_icechunk](../swot_icechunk). Data: SWOT L3 LR SSH, produced by AVISO/DUACS
with CNES support (see About in the app for the citation and the license terms).

## Layout

- `app/server.py`: FastAPI; map tiles rasterized with datashader from the swaths in memory, values, time
  series, lines.
- `app/swot_view.py`: the data recipes with icechunk + xarray only (the code users copy).
- `app/static/`: the page (MapLibre GL, uPlot), no build step.
- `Dockerfile`: the server with the pixi environment `serve` from `pixi.lock`.
- `deploy/`, `slurm/ec2.sh`: the test server on EC2, which sleeps when idle and wakes on a visit.
- `tests/`: pytest on small fake stores; `slurm/mutants.sh` checks that they catch injected bugs.

## Run

```bash
pixi run serve          # http://localhost:5006 (AWS credentials that can read the store)
pixi run test
```

On the biomix cluster, through Slurm: see [CLAUDE.md](CLAUDE.md).
