import icechunk
import numpy as np
import pandas as pd
import pytest
import xarray as xr

from swot_icechunk.store import append


def fake_l3(path, cycle, pass_id, lon0=30.0, day=None, tilt=0.0, n_lines=200, n_pixels=9):
    """A small SWOT L3-like file (as in ../swot_icechunk/tests/conftest.py) centered on lon0, from -10 to 10 deg latitude.

    Like the real files, odd passes run south to north with pixel 0 to the west, even passes
    north to south with pixel 0 to the east. The track leans `tilt` degrees of longitude per
    degree of latitude. data = cycle*1000 + line in the file.
    """
    ascending = pass_id % 2
    nadir_lat = np.linspace(-10, 10, n_lines)[:, None]
    # cross lines perpendicular to the track, as in SWOT (flat-earth approximation near the equator)
    s = np.linspace(-0.5, 0.5, n_pixels)[None, :] / np.hypot(1, tilt)
    lat = nadir_lat - tilt * s
    lon = (lon0 + tilt * nadir_lat + s) % 360
    if not ascending:
        lat, lon = lat[::-1, ::-1], lon[::-1, ::-1]
    data = cycle * 1000.0 + np.arange(n_lines)[:, None] + 0 * lat
    # non-round start time, like real files
    start = pd.Timestamp(f"2024-01-{day or cycle:02d}T05:45:34.074870274")
    dims = ("num_lines", "num_pixels")
    ds = xr.Dataset(
        {name: (dims, data) for name in ("ssha_filtered", "ssha_unfiltered", "mdt")}
        | {"time": ("num_lines", pd.date_range(start, periods=n_lines, freq="s"))},
        coords={"latitude": (dims, lat), "longitude": (dims, lon)},
    )
    # same encoding as the real files
    ssh = {"dtype": "int32", "scale_factor": 1e-4, "_FillValue": -2147483647}
    deg = {"dtype": "int32", "scale_factor": 1e-6}
    time = {"dtype": "float64", "units": "seconds since 2000-01-01 00:00:00.0", "calendar": "gregorian"}
    name = f"SWOT_L3_LR_SSH_Basic_{cycle:03d}_{pass_id:03d}_20240101T000000_20240101T010000_v3.0.nc"
    ds.to_netcdf(path / name, encoding={"ssha_filtered": ssh, "ssha_unfiltered": ssh, "mdt": ssh,
                                        "latitude": deg, "longitude": deg, "time": time})
    return path / name


@pytest.fixture
def make_file(tmp_path):
    return lambda *args, **kw: fake_l3(tmp_path, *args, **kw)


@pytest.fixture
def store_path(tmp_path, make_file):
    """Passes 7 (ascending) and 20 (descending) over lon 30, in two cycles; pass 150 far east."""
    path = tmp_path / "store"
    session = icechunk.Repository.create(icechunk.local_filesystem_storage(str(path))).writable_session("main")
    files = [make_file(3, 7, day=3), make_file(3, 20, lon0=30.5, day=4), make_file(3, 150, lon0=200, day=5),
             make_file(5, 7, day=6)]
    for f in files:
        append(session.store, f, along_chunk=50, time_chunk=2)
    session.commit("test")
    return str(path), files
