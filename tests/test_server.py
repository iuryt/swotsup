import io
import sys

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient
from PIL import Image


@pytest.fixture
def client(store_path, monkeypatch):
    monkeypatch.setenv("SWOT_STORE", store_path[0])
    sys.modules.pop("server", None)  # it opens the store when imported
    import server
    return TestClient(server.app)


PASS_7 = dict(west=29, east=31, south=-10, north=10, start="2024-01-03", days=1, variable="ssha_unfiltered")


def alpha(client, view, lon, lat, z=6):
    """Opacity of the map tile pixel at (lon, lat) for a view."""
    fx = (lon + 180) / 360 * 2**z
    fy = (1 - np.log(np.tan(np.pi / 4 + np.deg2rad(lat) / 2)) / np.pi) / 2 * 2**z
    png = client.get(f"/api/tile/{z}/{int(fx)}/{int(fy)}.png", params=view | dict(mode="none")).content
    return np.asarray(Image.open(io.BytesIO(png)).convert("RGBA"))[int(fy % 1 * 256), int(fx % 1 * 256), 3]


def test_tile_is_where_the_swath_is(client):
    """Pass 7 spans lon 29.5-30.5, lat -10-10 on 2024-01-03: its pixels must land there on the map tiles."""
    assert client.get("/api/load", params=PASS_7).json()["passages"] == 1
    assert alpha(client, PASS_7, 30.0, 5.0) == 255  # on the swath
    assert alpha(client, PASS_7, 31.0, 5.0) == 0  # east of it
    assert alpha(client, PASS_7, 30.0, 10.5) == 0  # north of its end
    assert alpha(client, PASS_7, 30.0, -9.5) == 255


def test_tiles_of_a_view_that_left_memory(client, monkeypatch):
    """Other visitors' views push a view out of memory; its tiles must still be drawn, from the data."""
    import server

    monkeypatch.setattr(server, "MAX_BYTES", 0)  # keep only the newest view
    pass_150 = PASS_7 | dict(west=199, east=201, start="2024-01-05")
    client.get("/api/load", params=PASS_7)
    client.get("/api/load", params=pass_150)
    assert tuple(PASS_7.values()) not in server.loaded
    assert alpha(client, PASS_7, 30.0, 5.0) == 255


def test_swath_east_of_180_where_the_map_shows_it(client):
    """A box across 180 (170 to -150) keeps its passage at lon 200 (continuous, x past the map's east edge); the map
    shows it at -160: drawn there from the world's copy."""
    pass_150 = PASS_7 | dict(west=170, east=-150, start="2024-01-05")
    assert alpha(client, pass_150, -160.0, 5.0) == 255


def test_timeline_days_over_the_box(client):
    days = client.get("/api/timeline", params=dict(west=29, east=31, south=-1, north=1)).json()["day"]
    assert [np.datetime64(d, "ms").astype("datetime64[D]").astype(str) for d in days] == \
        ["2024-01-03", "2024-01-04", "2024-01-06"]  # passes 7 and 20, not 150


def test_swath_across_180_stays_one_piece(client):  # client: server imported on the fake store
    import server

    lon = np.array([[178.0, 179.0, -180.0, -179.0], [178.5, 179.5, -179.5, -178.5]])
    out = server.continuous(lon, center=-170)
    np.testing.assert_allclose(np.diff(out, axis=1), 1)  # no jump of 360 within a line
    assert -200 < out.mean() < -170  # placed next to the requested center


def test_nothing_drawn_across_a_gap(client):
    """Lines without data (land) between two runs of ocean stay empty: the runs are not bridged by quads."""
    import server
    import xarray as xr

    lat = np.repeat(np.linspace(-5, 5, 21)[:, None], 3, axis=1)[None]  # one passage, 21 lines, 3 pixels
    lon = np.broadcast_to([29.9, 30.0, 30.1], lat.shape).copy()
    values = np.ones(lat.shape)
    values[0, 8:13] = np.nan  # land from -1 to 1
    dims = ("time", "along", "cross")
    sub = xr.Dataset({"ssha_unfiltered": (dims, values)}, coords={"latitude": (dims, lat), "longitude": (dims, lon)})
    data = server.Loaded(sub, ["ssha_unfiltered"], 29, 31)

    # a view from -2 to 2, as a tile would see it: ocean lines on both sides of the land
    (x0, x1), (y0, y1) = server.mercator(np.array([29.95, 30.05]), np.array([-2, 2]))
    drawn = np.isfinite(data.mean_on("none", (x0, x1), (y0, y1), 4, 8)).all(axis=1)  # rows south to north, 0.5 deg
    assert drawn[0] and drawn[-1]  # ocean, south and north
    assert not drawn[2:6].any()  # -1 to 1: land (the rows next to it touch the last ocean cells)


def test_click_names_the_passages_and_the_time_of_their_line(client):
    """Passes 7 (ascending, 3 Jan) and 20 (descending, 4 Jan) overlap at lon 30.25. Their files start at
    05:45:34 with one line a second; at lat 5 pass 7 is at its line 149 (counted from the south) and pass 20
    at its line 50 (counted from the north)."""
    both = PASS_7 | dict(days=2)
    v = client.get("/api/value", params=both | dict(lon=30.25, lat=5.0, mode="none")).json()
    assert [(q["pass_id"], q["cycle"]) for q in v["passages"]] == [(7, 3), (20, 3)]
    assert [q["time"] for q in v["passages"]] == ["2024-01-03 05:48:03", "2024-01-04 05:46:24"]


def test_line_follows_great_circles(client):
    """Points every `step` km along great circles: two segments meet at their vertex, and a segment across
    180 is 222 km long, not around the world."""
    import server

    lon, lat, dist, step = server.along_line(np.array([0.0, 1.0, 1.0]), np.array([0.0, 0.0, 1.0]), 2.0)
    assert step == 2.0 and np.allclose(np.diff(dist)[:-1], 2.0)
    assert dist[-1] == pytest.approx(2 * 111.195, abs=0.01) and (lon[-1], lat[-1]) == pytest.approx((1, 1))
    first = dist <= 111.19
    assert np.allclose(lat[first], 0, atol=1e-9) and np.allclose(lon[~first], 1, atol=1e-6)
    lon, lat, dist, _ = server.along_line(np.array([179.0, -179.0]), np.array([0.0, 0.0]), 2.0)
    assert dist[-1] == pytest.approx(222.39, abs=0.01) and np.all((lon >= 179) | (lon <= -179))


def test_transect_takes_the_nearest_pixel_within_its_cell(client):
    """A swath of 0.01 deg pixels from lon 29.9 to 30.1 whose values are their longitude, with a 3 pixel gap
    at 30 (nadir): along lat 0.5, a point gets its nearest pixel, and nothing in the gap or off the swath."""
    import server
    import xarray as xr

    lats, lons = np.linspace(-1, 1, 201), np.linspace(29.9, 30.1, 21)
    lat, lon = np.meshgrid(lats, lons, indexing="ij")
    values = lon.copy()
    values[:, 9:12] = np.nan  # 29.99 to 30.01
    dims = ("time", "along", "cross")
    sub = xr.Dataset({"ssha_unfiltered": (dims, values[None])},
                     coords={"latitude": (dims, lat[None]), "longitude": (dims, lon[None]), "lat": ("along", lats)})
    data = server.Loaded(sub, ["ssha_unfiltered"], 29, 31)
    lon, lat, _, _ = server.along_line(np.array([29.8, 30.2]), np.array([0.5, 0.5]), 0.25)
    v = data.along("none", lon, lat)[0][0]
    on = (np.abs(lon - 30) > 0.03) & (np.abs(lon - 30) < 0.095)
    assert np.allclose(v[on], lon[on], atol=0.0051)  # its own pixel, on the swath
    assert np.isnan(v[np.abs(lon - 30) < 0.005]).all()  # the gap: 2.2 km from the nearest pixel
    assert np.isnan(v[np.abs(lon - 30) > 0.115]).all()  # off the swath, beyond 1.5 km


def test_transect_names_its_passages(client):
    """Along lon 30 from lat 4 to 6, pass 7 of cycle 3: its values come from lines 139 to 159 (data =
    3000 + line), and its time is that of the line at the middle, 149 (05:48:03, a line a second), not the
    passage's mean (05:47:13)."""
    t = client.get("/api/transect", params=PASS_7 | dict(points="30,4;30,6", mode="none")).json()
    assert [(q["pass_id"], q["cycle"]) for q in t["passages"]] == [(7, 3)]
    assert abs(pd.Timestamp(t["passages"][0]["time"]) - pd.Timestamp("2024-01-03 05:48:03")) <= pd.Timedelta("2s")
    v = np.array([np.nan if x is None else x for x in t["passages"][0]["value"]])
    assert np.isfinite(v).any() and np.nanmin(v) >= 3139 and np.nanmax(v) <= 3159
    assert t["distance"][-1] == pytest.approx(222.39, abs=0.01)
    # each value with the time of its own line: line k at 05:45:34.07 + k seconds
    at = np.array([np.nan if x is None else x for x in t["passages"][0]["line_time"]])
    line = pd.to_datetime(at[np.isfinite(v)], unit="ms") - pd.Timestamp("2024-01-03 05:45:34.074870274")
    assert np.allclose(line.total_seconds(), v[np.isfinite(v)] - 3000, atol=0.01)


def test_hovmoller_has_every_passage_with_its_own_values_and_time(fine_store_path, monkeypatch):
    """Pass 7 on a ~2 km grid in cycles 3 (3 Jan) and 5 (6 Jan), data = cycle*1000 + line, one line a second. Along
    lon 29.8 from lat 4 to 6, then to lon 28: each passage has its own values (lines 700-800), the time of its own
    line at the middle, and none off the swath."""
    monkeypatch.setenv("SWOT_STORE", fine_store_path)
    sys.modules.pop("server", None)
    import server

    h = TestClient(server.app).get("/api/hovmoller", params=dict(points="29.8,4;29.8,6;28,6", variable="ssha_unfiltered")).json()
    assert [(q["pass_id"], q["cycle"]) for q in h["passages"]] == [(7, 3), (7, 5)]
    distance = np.array(h["distance"])
    for q, cycle, day in zip(h["passages"], (3, 5), ("2024-01-03", "2024-01-06")):
        v = np.full(len(distance), np.nan)
        v[q["first"]:q["first"] + len(q["value"])] = [np.nan if x is None else x for x in q["value"]]
        north = distance <= 222.4  # the first segment, on the swath
        assert np.isfinite(v[north]).mean() > 0.9
        assert np.nanmin(v[north]) >= cycle * 1000 + 699 and np.nanmax(v[north]) <= cycle * 1000 + 801
        assert np.isnan(v[distance > 222.4 + 40]).all()  # toward lon 28, off the swath
        middle = np.nanmedian(v) - cycle * 1000  # the line at the middle of the points hit
        t = pd.to_datetime(q["time"], unit="ms") - pd.Timestamp(f"{day} 05:45:34.074870274")
        assert abs(t.total_seconds() - middle) <= 3


def test_chart_netcdf_has_the_chart_numbers(fine_store_path, monkeypatch):
    """The NetCDF of a Hovmöller has the same passages and values as the chart's JSON, each value at its point (the
    line starts off the swath, so the passages' values start further along)."""
    import xarray as xr

    monkeypatch.setenv("SWOT_STORE", fine_store_path)
    sys.modules.pop("server", None)
    import server

    c = TestClient(server.app)
    params = dict(points="28,6;29.8,6;29.8,4", variable="ssha_unfiltered")
    h = c.get("/api/hovmoller", params=params).json()
    assert all(q["first"] > 0 for q in h["passages"])
    nc = xr.open_dataset(io.BytesIO(c.get("/api/hovmoller.nc", params=params).content), engine="h5netcdf")
    assert nc.cycle.values.tolist() == [q["cycle"] for q in h["passages"]] == [3, 5]
    for k, q in enumerate(h["passages"]):
        row = nc.value.values[k, q["first"]:q["first"] + len(q["value"])]
        np.testing.assert_allclose(row, [np.nan if v is None else v for v in q["value"]])
        assert np.isnan(nc.value.values[k, :q["first"]]).all()


def test_swath_drawn_where_the_nadir_gap_has_no_position(client):
    """L2 Unsmoothed has no latitude/longitude in its nadir gap: the meshes interpolate them across each line, so
    the pixels with values on both sides are drawn (a line needs all its corners), across 180 too."""
    import server
    import xarray as xr

    lat = np.repeat(np.linspace(-1, 1, 21)[:, None], 7, axis=1)[None]  # one passage, 21 lines, 7 pixels
    lon = np.broadcast_to([179.7, 179.8, 179.9, 180.0, -179.9, -179.8, -179.7], lat.shape).copy()
    values = np.ones(lat.shape)
    values[0, :, 2:5] = np.nan  # the gap: no value...
    lat[0, :, 2:5] = lon[0, :, 2:5] = np.nan  # ...and no position
    dims = ("time", "along", "cross")
    sub = xr.Dataset({"ssha_unfiltered": (dims, values)}, coords={"latitude": (dims, lat), "longitude": (dims, lon)})
    data = server.Loaded(sub, ["ssha_unfiltered"], 179, -179)
    (x0, x1), (y0, y1) = server.mercator(np.array([179.65, 180.35]), np.array([-0.5, 0.5]))
    m = np.isfinite(data.mean_on("none", (x0, x1), (y0, y1), 14, 4))
    assert m[:, :3].all() and m[:, -3:].all()  # both sides of the gap
    assert not m[:, 6:8].any()  # not the gap itself


def test_detail_tiles_take_the_base_colors(store_path, monkeypatch):
    """A detail product (here the same store) is drawn from its zoom on, minus what the base view subtracts: where
    both have the same data, a detail tile has the base tile's colors; off the swath it is transparent."""
    monkeypatch.setenv("SWOT_STORE", store_path[0])
    monkeypatch.setenv("SWOT_DETAIL_STORE", store_path[0])
    monkeypatch.setenv("SWOT_DETAIL_PRODUCT", "l3")
    monkeypatch.setenv("SWOT_DETAIL_ZOOM", "6")
    sys.modules.pop("server", None)
    import server

    c = TestClient(server.app)
    view = PASS_7 | dict(mode="box", vmin=-0.05, vmax=0.05)  # values vary along the track: colors change
    c.get("/api/load", params=PASS_7)
    lon, lat = 30.0, 5.0
    for z in (6, 9):  # at 9 a line (2 km) is ~7 pixels: one missing at a tile's edge shows
        fx = (lon + 180) / 360 * 2**z
        fy = (1 - np.log(np.tan(np.pi / 4 + np.deg2rad(lat) / 2)) / np.pi) / 2 * 2**z
        get = lambda kind: np.asarray(Image.open(io.BytesIO(c.get(f"/api/{kind}/{z}/{int(fx)}/{int(fy)}.png", params=view).content)).convert("RGBA"))  # noqa: E731
        base, fine = get("tile"), get("detail")
        on = base[..., 3] == 255
        assert on.sum() > 100 and (fine[..., 3] == 255).sum() > 100
        both = on & (fine[..., 3] == 255)
        assert np.abs(base[both].astype(int) - fine[both].astype(int)).max() <= 2
        assert (fine[~on][..., 3] == 0).all()  # nothing where the base has nothing
        assert (on & (fine[..., 3] == 0)).sum() <= 0.002 * on.sum()  # and every line up to the tile's edges
    z = 4
    fx, fy = (lon + 180) / 360 * 2**z, (1 - np.log(np.tan(np.pi / 4 + np.deg2rad(lat) / 2)) / np.pi) / 2 * 2**z
    far = c.get(f"/api/detail/{z}/{int(fx)}/{int(fy)}.png", params=view).content
    assert (np.asarray(Image.open(io.BytesIO(far)).convert("RGBA"))[..., 3] == 0).all()  # not below its zoom - 1


def test_detail_tile_with_only_the_edges_of_lines(tmp_path, make_file, monkeypatch):
    """The track leans, so do its lines: north of the last nadir (lat 10), a zoom 11 tile holds only the west edges
    of the last lines. The detail tile draws them like the base does (the passes are found from lines near it)."""
    import icechunk
    from swot_cloud.store import append

    path = str(tmp_path / "tilted")
    session = icechunk.Repository.create(icechunk.local_filesystem_storage(path)).writable_session("main")
    append(session.store, make_file(3, 7, day=3, tilt=0.5, n_lines=1001, n_pixels=51), along_chunk=100, time_chunk=1)
    session.commit("tilted")
    monkeypatch.setenv("SWOT_STORE", path)
    monkeypatch.setenv("SWOT_DETAIL_STORE", path)
    monkeypatch.setenv("SWOT_DETAIL_PRODUCT", "l3")
    monkeypatch.setenv("SWOT_DETAIL_ZOOM", "6")
    sys.modules.pop("server", None)
    import server

    c = TestClient(server.app)
    view = dict(west=33, east=37, south=8, north=12, start="2024-01-03", days=1, variable="ssha_unfiltered",
                mode="none", vmin=3500, vmax=4100)
    z, lon, lat = 11, 34.65, 10.17  # the west edge of the last lines: lon ~34.55-35, lat up to 10.22
    fx = (lon + 180) / 360 * 2**z
    fy = (1 - np.log(np.tan(np.pi / 4 + np.deg2rad(lat) / 2)) / np.pi) / 2 * 2**z
    south = server.tile_ranges(z, int(fx), int(fy))[1][0]
    assert np.rad2deg(2 * np.arctan(np.exp(south / server.R)) - np.pi / 2) > 10  # all north of the last nadir
    get = lambda kind: np.asarray(Image.open(io.BytesIO(c.get(f"/api/{kind}/{z}/{int(fx)}/{int(fy)}.png", params=view).content)).convert("RGBA"))  # noqa: E731
    on = get("tile")[..., 3] == 255
    assert on.sum() > 100 and (on & (get("detail")[..., 3] == 0)).sum() <= 0.002 * on.sum()


def test_coarse_levels_draw_what_the_full_data_draws(fine_store_path, monkeypatch):
    """A view of the world (one tile pixel ~40 km over 2 km cells) is drawn from 16 x 16 block means as points: each
    block at its centre, so the tile's means are those of the cells (data = cycle*1000 + line: a block put at its
    corner would be ~8 lines off); a click reads the cells themselves."""
    monkeypatch.setenv("SWOT_STORE", fine_store_path)
    sys.modules.pop("server", None)
    import server

    c = TestClient(server.app)
    view = dict(west=29, east=31, south=-10, north=10, start="2024-01-03", days=1, variable="ssha_unfiltered")
    c.get("/api/load", params=view)
    data = server.load(**view)[0]
    assert 16 in data.points
    xr_, yr_ = server.tile_ranges(0, 0, 0)  # the world: pixels of ~150 km at the equator, the 16 x 16 blocks
    # the data grow linearly along the track (one per line, lines every 0.02 deg from -10): a block's mean is the
    # value at its centre, so each pixel should be, on average, the value at its own latitude
    y = yr_[1] - (np.arange(256) + 0.5) * (yr_[1] - yr_[0]) / 256  # pixel rows from north to south
    lat = np.rad2deg(2 * np.arctan(np.exp(y / server.R)) - np.pi / 2)
    expected = (3000 + (lat + 10) / 0.02)[::-1, None]  # rows south to north, as mean_on gives them
    m = data.mean_on("none", xr_, yr_, 256, 256)
    ok = np.isfinite(m)
    assert ok.sum() >= 10 and abs(np.mean((m - expected)[ok])) < 3
    # a click reads the cells themselves: the value of its latitude's line (603.5 at 2.07 N), not a block's mean
    assert abs(data.passages_at("none", *server.mercator(30.1, 2.07))[0][0] - (3000 + 12.07 / 0.02)) <= 1.5


def test_l2_values_only_where_the_quality_flag_is_good(client):
    """L2 values over land and ice reach kilometres: a product with a quality flag keeps only its pixels flagged 0,
    for drawing, colors and clicks."""
    import server
    import xarray as xr

    lat = np.repeat(np.linspace(-1, 1, 21)[:, None], 5, axis=1)[None]
    lon = np.broadcast_to([29.8, 29.9, 30.0, 30.1, 30.2], lat.shape).copy()
    values, flag = np.full(lat.shape, 0.1), np.zeros(lat.shape)
    values[0, :, 3:], flag[0, :, 3:] = 2000.0, 132  # the east side: land, flagged
    dims = ("time", "along", "cross")
    sub = xr.Dataset({"v": (dims, values), "q": (dims, flag)}, coords={"latitude": (dims, lat), "longitude": (dims, lon)})
    data = server.Loaded(sub, ["v"], 29, 31, mask="q")
    assert data.quantiles["none"][-1] < 1  # the flagged values are not in the colors
    (x0, x1), (y0, y1) = server.mercator(np.array([29.75, 30.25]), np.array([-0.5, 0.5]))
    m = data.mean_on("none", (x0, x1), (y0, y1), 10, 4)
    assert np.nanmax(m) < 1 and np.isfinite(m[:, :4]).all() and np.isnan(m[:, -2:]).all()


def test_one_absurd_pixel_does_not_move_the_colors(client):
    """What the modes subtract is a median: a pixel kilometres off (some L2 pixels pass their quality flag) leaves
    the others near zero once the box's or the passage's middle is taken away."""
    import server
    import xarray as xr

    lat = np.repeat(np.linspace(-1, 1, 21)[:, None], 5, axis=1)[None]
    lon = np.broadcast_to([29.8, 29.9, 30.0, 30.1, 30.2], lat.shape).copy()
    values = np.full(lat.shape, 0.1)
    values[0, 7, 0] = 3000.0  # pixel 35 of the passage: in its sample (every 7th)
    dims = ("time", "along", "cross")
    sub = xr.Dataset({"v": (dims, values)}, coords={"latitude": (dims, lat), "longitude": (dims, lon)})
    data = server.Loaded(sub, ["v"], 29, 31)
    for mode in ("box", "passage"):
        assert np.nanmedian(np.abs(data.value(mode, 0))) < 0.01


def test_level_follows_the_pixel_on_the_ground(fine_store_path, monkeypatch):
    """The level a view is drawn from compares a screen pixel with the cells on the ground at the view's latitude:
    the world tile takes the 16 x 16 blocks (35 km) as points; a zoom 7 tile (1.2 km pixels over 2.2 km cells), the
    cells as meshes."""
    monkeypatch.setenv("SWOT_STORE", fine_store_path)
    sys.modules.pop("server", None)
    import server

    view = dict(west=29, east=31, south=-10, north=10, start="2024-01-03", days=1, variable="ssha_unfiltered")
    data = server.load(**view)[0]
    assert 1800 < data.spacing < 2600  # 51 pixels over ~112 km
    drawn = []
    monkeypatch.setattr(server.dsh.Canvas, "points", lambda self, frame, *a, **k: drawn.append(len(frame)) or server.xr.DataArray(np.full((self.plot_height, self.plot_width), np.nan)))
    data.mean_on("none", *server.tile_ranges(0, 0, 0), 256, 256)  # the world: ~156 km pixels at its middle (0 N)
    data.mean_on("none", *server.tile_ranges(7, 74, 62), 256, 256)  # lon 30, lat 5
    assert len(drawn) == 1 and 0 < drawn[0] <= len(data.points[16][0])  # the world tile only, from the 16 x 16 blocks



def test_cells_as_points_where_smaller_than_a_pixel(fine_store_path, monkeypatch):
    """At zoom 6 (2.4 km pixels over 2.2 km cells) the cells in view are drawn as points in one go: the same
    picture as their meshes, within a cell's value."""
    monkeypatch.setenv("SWOT_STORE", fine_store_path)
    sys.modules.pop("server", None)
    import server

    view = dict(west=29, east=31, south=-10, north=10, start="2024-01-03", days=1, variable="ssha_unfiltered")
    data = server.load(**view)[0]
    ranges = server.tile_ranges(6, 37, 31)  # lon 28-34, lat 0-6
    points = data.mean_on("none", *ranges, 256, 256)
    total, count = np.zeros((256, 256)), np.zeros((256, 256))
    for *_, m in data.rasters("none", *ranges, 256, 256):
        total, count = total + np.nan_to_num(m), count + np.isfinite(m)
    with np.errstate(invalid="ignore"):
        meshes = total / count
    both = np.isfinite(points) & np.isfinite(meshes)
    assert both.sum() > 200 and np.nanmedian(np.abs(points - meshes)[both]) <= 1.5
