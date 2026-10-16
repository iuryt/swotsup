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
