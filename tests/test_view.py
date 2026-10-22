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
