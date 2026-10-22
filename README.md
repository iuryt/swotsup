# SWOT's Up

Explore the whole SWOT mission. A web map over SWOT KaRIn low-rate sea surface height swaths, read on demand from an IceChunk store in the
cloud: every swath since July 2023, without downloading files.

- Pick a region and up to 21 days on the timeline; see the swaths on a globe or a flat map.
- Click a point: its value, the passages there (pass, cycle, time), and the time series over the whole mission.
- Draw a line: the values along it, one curve per passage.
- On a phone too: the options as icons, cards as sheets, Done / Undo instead of a double click.
- Copy the few lines of icechunk + xarray that give you the same data in your own Python.

Data: SWOT LR SSH from NASA and CNES. Today the app reads the L3 product (AVISO/DUACS v3.0 Basic, produced
with CNES support); the public version will show the L2 product, and L3 too if AVISO allows it (see About in
the app for the citation and the license terms). The stores are built in
[swot_cloud](https://github.com/iuryt/swot_cloud) (private).

## Layout

- `app/server.py`: FastAPI; map tiles rasterized with datashader from the swaths in memory, values, time
  series, lines.
- `app/swot_view.py`: the data recipes with icechunk + xarray only (the code users copy).
- `app/static/`: the page (MapLibre GL, uPlot), no build step.
- `Dockerfile`: the server with the pixi environment `serve` from `pixi.lock`.
- `deploy/`, `slurm/ec2.sh`: the test server on EC2, which sleeps when idle and wakes on a visit.
- `tests/`: pytest on small fake stores; `slurm/mutants.sh` checks that they catch injected bugs.
- `img/history/`: a screenshot of every version.

## Run

```bash
pixi run serve          # http://localhost:5006 (AWS credentials that can read the store)
pixi run test
```

On the biomix cluster, through Slurm: see [CLAUDE.md](CLAUDE.md).
