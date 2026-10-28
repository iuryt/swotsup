import icechunk
import numpy as np
import xarray as xr

import swot_view


def test_point_series(store_path):
    path, files = store_path
    lon, lat, radius = 30.2, 0.3, 20
    series = swot_view.point_series(swot_view.open_store(path), lon, lat, radius=radius, min_pixels=1)
    np.testing.assert_array_equal(series.pass_id, [7, 20, 7])
    # same mean as the pixels of the source file within the radius
    for f, got, pixels in zip([files[0], files[1], files[3]], series.ssha_filtered.values, series.pixels.values):
        src = xr.open_dataset(f)
        near = swot_view.distance(src.latitude, src.longitude, lat, lon) <= radius
        assert pixels == near.sum() > 1
        np.testing.assert_allclose(got, src.ssha_filtered.where(near).mean(), atol=1e-3)
    # passages with too few pixels in the circle are dropped
    assert swot_view.point_series(swot_view.open_store(path), lon, lat, radius=radius, min_pixels=1000).sizes["time"] == 0


def test_snippet_gives_the_app_subset(store_path, monkeypatch):
    path, _ = store_path
    box = (29, 31, -1, 1, "2024-01-01", "2024-01-05")
    monkeypatch.setattr(icechunk, "s3_storage", lambda **kw: icechunk.local_filesystem_storage(path))
    scope = {}
    exec(swot_view.snippet(*box, target="s3://bucket/prefix"), scope)
    want = swot_view.subset(swot_view.open_store(path), *box)
    np.testing.assert_array_equal(scope["sub"].pass_id, [7, 20])
    xr.testing.assert_equal(scope["sub"].ssha_filtered, want.ssha_filtered)


def test_whole_globe(store_path):
    path, _ = store_path
    np.testing.assert_array_equal(swot_view.passes_over(swot_view.open_store(path), -180, 180, -1, 1), [7, 20, 150])


def test_snippet_for_the_whole_globe(store_path, monkeypatch):
    """No longitude filter and no latitude cut when they would keep everything; same passages as the app."""
    path, _ = store_path
    box = (-180, 180, -90, 90, "2024-01-01", "2024-01-05")
    code = swot_view.snippet(*box, target="s3://bucket/prefix")
    assert "lon_min" not in code and "lat=" not in code
    monkeypatch.setattr(icechunk, "s3_storage", lambda **kw: icechunk.local_filesystem_storage(path))
    scope = {}
    exec(code, scope)
    np.testing.assert_array_equal(scope["sub"].pass_id, swot_view.subset(swot_view.open_store(path), *box).pass_id)
    np.testing.assert_array_equal(scope["sub"].pass_id, [7, 20, 150])


def test_snippet_from_arraylake(store_path, monkeypatch):
    """A store on Earthmover: the code asks the arraylake client for that repository and gives the app's subset."""
    import arraylake

    path, _ = store_path
    asked = []

    class Client:
        def get_repo(self, name):
            asked.append(name)
            return icechunk.Repository.open(icechunk.local_filesystem_storage(path))

    monkeypatch.setattr(arraylake, "Client", Client)
    box = (29, 31, -1, 1, "2024-01-01", "2024-01-05")
    scope = {}
    exec(swot_view.snippet(*box, target="arraylake:some-org/some-repo"), scope)
    assert asked == ["some-org/some-repo"]
    xr.testing.assert_equal(scope["sub"].ssha_filtered, swot_view.subset(swot_view.open_store(path), *box).ssha_filtered)


def test_point_series_with_a_small_radius(tmp_path, make_file):
    """On a ~2 km grid like the real one, a 2 km circle holds ~3 pixels: the passage must still be there."""
    from swot_cloud.store import append

    path = str(tmp_path / "fine")
    session = icechunk.Repository.create(icechunk.local_filesystem_storage(path)).writable_session("main")
    append(session.store, make_file(3, 7, day=3, n_lines=1001, n_pixels=51), along_chunk=100, time_chunk=1)
    session.commit("fine grid")
    out = swot_view.point_series(swot_view.open_store(path), 30.1, 2.0, radius=2)
    assert out.sizes["time"] == 1 and 1 <= int(out.pixels[0]) < 5



def run_snippet(code, path, monkeypatch):
    """Run a generated snippet on a local store, without showing its figure."""
    import matplotlib

    matplotlib.use("Agg")
    monkeypatch.setattr(icechunk, "s3_storage", lambda **kw: icechunk.local_filesystem_storage(path))
    scope = {}
    exec(code, scope)
    return scope


def test_series_snippet_gives_the_app_series(fine_store_path, monkeypatch):
    """The time series code users copy gives the app's series, for a 5 km and a 2 km radius, in the swath and near
    its end (lat 10, where a circle of another size is cut on one side and its mean moves)."""
    for lat, radius in ((2.0, 5), (2.0, 2), (9.97, 5)):
        scope = run_snippet(swot_view.series_snippet(30.1, lat, ["ssha_filtered"], radius, target="s3://b/p"),
                            fine_store_path, monkeypatch)
        want = swot_view.point_series(swot_view.open_store(fine_store_path), 30.1, lat, radius=radius)
        np.testing.assert_allclose(scope["series"].values, want.ssha_filtered.values)
        assert len(want.time) == 2


def test_line_snippet_gives_every_passage_or_those_of_the_dates(fine_store_path, monkeypatch):
    """The line code: the Hovmöller has both passages, each with its own values (cycle*1000 + line 700-800 along
    lat 4-6) and none off the swath (toward lon 28); the profile of 3-4 Jan has only cycle 3's."""
    points = [(29.8, 4.0), (29.8, 6.0), (28.0, 6.0)]
    hov = run_snippet(swot_view.line_snippet(points, ["ssha_filtered"], mean="none", target="s3://b/p"),
                      fine_store_path, monkeypatch)
    assert [(p, c) for p, c, _ in hov["passages"]] == [(7, 3), (7, 5)]
    d, v = hov["distance"], hov["values"]
    on, off = d <= 222.4, d > 222.4 + 40
    for row, cycle in zip(v, (3, 5)):
        assert np.isfinite(row[on]).mean() > 0.9 and np.isnan(row[off]).all()
        assert cycle * 1000 + 699 <= np.nanmin(row[on]) and np.nanmax(row[on]) <= cycle * 1000 + 801
    profile = run_snippet(swot_view.line_snippet(points, ["ssha_filtered"], start="2024-01-03", end="2024-01-04T23:59",
                                                 target="s3://b/p"), fine_store_path, monkeypatch)
    assert [(p, c) for p, c, _ in profile["passages"]] == [(7, 3)]


def test_written_passages_and_envelopes_from_the_nadir(store_path):
    """A store being filled: the passages listed as written are those with data, and a pass without its envelopes
    (lon_min/lon_max not written yet) is still found over a box, from its nadir and the swath's half width."""
    path, _ = store_path
    session = icechunk.Repository.open(icechunk.local_filesystem_storage(path)).readonly_session("main")
    ds = swot_view.dataset(session.store)
    assert swot_view.written(ds, session.store).tolist() == [0, 1, 2, 3]  # every passage (2 chunks of 2)
    ds = ds.assign(latitude_nadir=ds.latitude.isel(cross=ds.sizes["cross"] // 2),
                   longitude_nadir=ds.longitude.isel(cross=ds.sizes["cross"] // 2))
    lon_min, lon_max = swot_view.nadir_envelope(ds, half_width=60)
    ds = ds.assign_coords(lon_min=(ds.lon_min.dims, np.full(ds.lon_min.shape, np.nan)),
                          lon_max=(ds.lon_max.dims, np.full(ds.lon_max.shape, np.nan)))
    assert len(swot_view.passes_over(ds, 29, 31, -1, 1)) == 0  # without envelopes, nothing
    ds = ds.assign_coords(lon_min=(ds.lon_min.dims, lon_min), lon_max=(ds.lon_max.dims, lon_max))
    np.testing.assert_array_equal(swot_view.passes_over(ds, 29, 31, -1, 1), [7, 20])
    np.testing.assert_array_equal(swot_view.passes_over(ds, 30.7, 31, -1, 1), [20])  # 7 ends at 30.0 + 60 km
    assert 7 in swot_view.passes_over(ds, 30.52, 30.6, -1, 1)  # ...which is east of 30.52 (the swath's edge: 30.5)
