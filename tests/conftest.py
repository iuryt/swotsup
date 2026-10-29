import icechunk
import numpy as np
import pandas as pd
import pytest
import xarray as xr

from swot_cloud.store import append


def fake_l3(path, cycle, pass_id, lon0=30.0, day=None, tilt=0.0, n_lines=200, n_pixels=9):
    """A small SWOT L3-like file (as in ../swot_cloud/tests/conftest.py) centered on lon0, from -10 to 10 deg latitude.

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


@pytest.fixture(scope="session")
def store_path(tmp_path_factory):
    """Passes 7 (ascending) and 20 (descending) over lon 30, in two cycles; pass 150 far east. Built once for all the
    tests (they only read it)."""
    files_dir = tmp_path_factory.mktemp("files")
    path = tmp_path_factory.mktemp("store")
    session = icechunk.Repository.create(icechunk.local_filesystem_storage(str(path))).writable_session("main")
    files = [fake_l3(files_dir, 3, 7, day=3), fake_l3(files_dir, 3, 20, lon0=30.5, day=4),
             fake_l3(files_dir, 3, 150, lon0=200, day=5), fake_l3(files_dir, 5, 7, day=6)]
    for f in files:
        append(session.store, f, along_chunk=50, time_chunk=2)
    session.commit("test")
    return str(path), files


@pytest.fixture(scope="session")
def fine_store_path(tmp_path_factory):
    """Pass 7 on a ~2 km grid like the real one (lines every 0.02 deg from lat -10, 51 pixels over lon 29.5-30.5),
    cycles 3 (3 Jan) and 5 (6 Jan), data = cycle*1000 + line, one line a second. Built once (read only)."""
    files_dir = tmp_path_factory.mktemp("fine_files")
    path = str(tmp_path_factory.mktemp("fine"))
    session = icechunk.Repository.create(icechunk.local_filesystem_storage(path)).writable_session("main")
    for cycle, day in ((3, 3), (5, 6)):
        append(session.store, fake_l3(files_dir, cycle, 7, day=day, n_lines=1001, n_pixels=51), along_chunk=100, time_chunk=1)
    session.commit("fine grid")
    return path


@pytest.fixture(scope="session")
def dtu_store_path(tmp_path_factory):
    """Passes 7 (3 Jan) and 20 (4 Jan, lon 30.5) of cycle 3 on a ~2.5 km grid (not fine_store_path's), with a fake
    mss_cor_dtu25 (pass_number, along, cross) as L2 Expert's: pass / 100 + 0.001 per km of along, linear so that
    interpolating it is exact. Built once (read only)."""
    files_dir = tmp_path_factory.mktemp("dtu_files")
    path = str(tmp_path_factory.mktemp("dtu"))
    session = icechunk.Repository.create(icechunk.local_filesystem_storage(path)).writable_session("main")
    for pass_id, lon0, day in ((7, 30.0, 3), (20, 30.5, 4)):
        append(session.store, fake_l3(files_dir, 3, pass_id, lon0=lon0, day=day, n_lines=901, n_pixels=45),
               along_chunk=100, time_chunk=1)
    session.commit("passes")
    session = icechunk.Repository.open(icechunk.local_filesystem_storage(path)).writable_session("main")
    ds = xr.open_zarr(session.store, consolidated=False, chunks=None)
    cor = ds.pass_number / 100 + 0.001 * ds.along + 0 * ds.cross
    cor = cor.astype("float32").transpose("pass_number", "along", "cross").reset_coords(drop=True)
    xr.Dataset({"mss_cor_dtu25": cor}).to_zarr(session.store, mode="a", consolidated=False)
    session.commit("dtu25")
    return path
