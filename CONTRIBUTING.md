# Contributing

Thanks for helping. Bug reports and ideas are welcome as [issues](https://github.com/iuryt/swotsup/issues); please
open one before a large change, so we can agree on it first.

## Set up

With [pixi](https://pixi.sh):

```bash
git clone https://github.com/iuryt/swotsup
pixi install                 # the environments: default (tests, screenshots) and serve (the server alone)
pixi run -e serve serve      # http://localhost:5006
```

The server reads the SWOT stores named by `SWOT_STORE` (and `SWOT_DETAIL_STORE`, `SWOT_RASTERS`): see the top of
`app/server.py`.

## Tests

The tests build small fake stores with [swot_cloud](https://github.com/iuryt/swot_cloud). Clone it next to this
repository, then run them:

```bash
git clone https://github.com/iuryt/swot_cloud ../swot_cloud
pixi run test
```

Each test should be able to fail on a real bug. `tests/mutations.sh` lists bugs injected one at a time, and each
must make a test fail.

## Pull requests

- One issue per branch and pull request, with "Closes #N" in the description.
- Keep the code that users copy (`app/swot_view.py`) to icechunk and xarray only.
- For a visible change, add a screenshot to the pull request.
- Comments, docs and commit messages in English.
