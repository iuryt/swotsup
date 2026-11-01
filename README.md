# SWOT's Up

**[swotsup.org](https://swotsup.org)**: explore the whole SWOT mission in your browser. A web map over the SWOT KaRIn
Low Rate sea surface height swaths, read on demand from a single versioned cloud dataset
([Icechunk](https://icechunk.io)), without downloading files.

- **Pick a region and up to 21 days** on the timeline; see the swaths on a globe or a flat map, 2 km everywhere and
  250 m when you zoom in.
- **Click a point**: its value, the passages there, and its time series over the whole mission.
- **Draw a line**: its profile on the chosen days, or its Hovmöller over the whole mission.
- **Take it away**: the data on screen as NetCDF, the figure as PNG, or the few lines of icechunk + xarray that give
  you the same data in your own Python.
- **Share a link**: the address keeps the view (place, dates, colors, the point, the line) and reopens it.

## Data

SWOT Level-2 KaRIn Low Rate SSH, version D (NASA/JPL and CNES, through PO.DAAC): Expert (2 km,
[doi:10.24400/527896/A01-2023.015](https://doi.org/10.24400/527896/A01-2023.015)) and Unsmoothed (250 m,
[doi:10.24400/527896/A01-2023.016](https://doi.org/10.24400/527896/A01-2023.016)). The files, one per passage, are
restructured into one dataset per product on [Arraylake](https://earthmover.io) (Earthmover), with the corrections
stored as variables: the crossover correction, the DTU25 mean sea surface
([doi:10.11583/DTU.29412275](https://doi.org/10.11583/DTU.29412275), CC BY 4.0), an experimental cross-track
calibration and an editing flag (rain cells, sea ice). The map shows them applied by default; each can be switched
off. Bathymetry: GEBCO_2026 ([doi:10.5285/4f68d5c7-45eb-f999-e063-7086abc036fa](https://doi.org/10.5285/4f68d5c7-45eb-f999-e063-7086abc036fa)).

This tool is not endorsed by NASA, JPL or CNES. The site records anonymous usage only (no cookies, no addresses
kept): see [docs/usage-data.md](docs/usage-data.md).

## How it works

- `app/swot_view.py`: the data recipes (open the dataset, the passages over a box, a point's series, a line's
  values) with icechunk + xarray only. The code the app offers to copy is this code, and the tests run it.
- `app/server.py` (FastAPI): map tiles rasterized with datashader from the passages in view and colored on the
  server, so only images reach the browser; values, series, lines and Hovmöllers; NetCDF downloads. Far out, the
  globe comes from precomputed daily maps.
- `app/static/`: the page (MapLibre GL, uPlot), no build step.
- `Dockerfile`: the server with the pixi environment `serve` from `pixi.lock`.
- `deploy/`, `slurm/ec2.sh`: the server on AWS EC2, which sleeps when idle and wakes on a visit.
- `tests/`: pytest on small fake stores; `tests/mutations.sh` lists injected bugs the tests must catch.
- `img/history/`: a screenshot of every version.

## Run

With [pixi](https://pixi.sh):

```bash
pixi run -e serve serve   # http://localhost:5006; reading the SWOT stores needs access to them (SWOT_STORE, see app/server.py)
```

The tests build small fake stores with [swot_cloud](https://github.com/iuryt/swot_cloud), the code that restructures
the SWOT files, expected next to this repository (`../swot_cloud`); it will be public with its paper. Then
`pixi run test`.

## Credits

Iury Simoes-Sousa (University of Delaware, [iury@udel.edu](mailto:iury@udel.edu)). Thanks to Ryan Abernathey and
Deepak Cherian (Earthmover), Tom Farrar (Woods Hole Oceanographic Institution), and Bia Villas Bôas, who named it.

MIT License.
