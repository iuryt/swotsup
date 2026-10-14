import io
import sys

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image


@pytest.fixture
def client(store_path, monkeypatch):
    monkeypatch.setenv("SWOT_STORE", store_path[0])
    sys.modules.pop("server", None)  # it opens the store when imported
    import server
    return TestClient(server.app)


def test_tile_is_where_the_swath_is(client):
    """Pass 7 spans lon 29.5-30.5, lat -10-10 on 2024-01-03: its pixels must land there on the map tiles."""
    d = client.get("/api/load", params=dict(west=29, east=31, south=-10, north=10, start="2024-01-03", days=1,
                                            variable="ssha_unfiltered")).json()
    assert d["passages"] == 1
    z = 6

    def alpha(lon, lat):
        fx = (lon + 180) / 360 * 2**z
        fy = (1 - np.log(np.tan(np.pi / 4 + np.deg2rad(lat) / 2)) / np.pi) / 2 * 2**z
        png = client.get(f"/api/tile/{d['id']}/{z}/{int(fx)}/{int(fy)}.png", params=dict(mode="none")).content
        return np.asarray(Image.open(io.BytesIO(png)).convert("RGBA"))[int(fy % 1 * 256), int(fx % 1 * 256), 3]

    assert alpha(30.0, 5.0) == 255  # on the swath
    assert alpha(31.0, 5.0) == 0  # east of it
    assert alpha(30.0, 10.5) == 0  # north of its end
    assert alpha(30.0, -9.5) == 255


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
