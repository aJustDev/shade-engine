"""/v1/shade: golden verdicts, timezone semantics and cache headers."""

from pathlib import Path
from typing import Any

import httpx
import yaml
from fastapi.testclient import TestClient
from pyproj import Transformer

import synthetic
from conftest import CUBE_CITY
from shade_api.app import create_app
from shade_api.settings import ApiSettings

# The golden NEAR point (10 m north of the cube wall) in WGS84, computed the
# same way the API will invert it. Roundtrip error is nanometers: same pixel.
_NEAR_X = synthetic.UTM_ORIGIN[0] + synthetic.QUERY_X
_NEAR_Y = synthetic.UTM_ORIGIN[1] + synthetic.CUBE_NORTH_WALL_Y + 10.0
_TO_WGS84 = Transformer.from_crs("EPSG:25830", "EPSG:4326", always_xy=True)
NEAR_LON, NEAR_LAT = _TO_WGS84.transform(_NEAR_X, _NEAR_Y)


def _shade(client: TestClient, **params: Any) -> httpx.Response:
    query: dict[str, Any] = {"city": "cube", "lat": NEAR_LAT, "lon": NEAR_LON, **params}
    response: httpx.Response = client.get("/v1/shade", params=query)
    return response


def test_winter_noon_is_building_shade(client: TestClient) -> None:
    response = _shade(client, at="2026-12-21T13:20:00+01:00")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "public, max-age=86400"
    body = response.json()
    assert body["state"] == "shade"
    assert body["in_shade"] is True
    assert body["shade_type"] == "building"
    assert 28.0 < body["sun"]["elevation_deg"] < 29.5
    assert body["attribution"] == ["Synthetic LiDAR (test fixture)"]


def test_a_building_shade_carries_no_caveat(client: TestClient) -> None:
    """The field is empty unless the answer actually depends on the assumption.

    The cube fixture has no vegetation, so this is the half of the contract the
    API can prove end to end; that the caveat DOES ride on vegetal shade is
    pinned at the domain level, where a crown can be built in three lines
    (test_shade_classes.py).
    """
    assert _shade(client, at="2026-12-21T13:20:00+01:00").json()["caveats"] == []
    assert _shade(client, at="2026-06-21T14:20:00+02:00").json()["caveats"] == []
    assert _shade(client, at="2026-12-21T03:00:00").json()["caveats"] == []


def test_summer_noon_is_sun(client: TestClient) -> None:
    response = _shade(client, at="2026-06-21T14:20:00+02:00")
    body = response.json()
    assert body["state"] == "sun"
    assert body["in_shade"] is False
    assert body["shade_type"] is None
    assert body["sun"]["elevation_deg"] > 70.0


def test_night_is_its_own_state(client: TestClient) -> None:
    body = _shade(client, at="2026-12-21T03:00:00").json()
    assert body["state"] == "night"
    assert body["in_shade"] is False
    assert body["shade_type"] is None
    assert body["sun"]["elevation_deg"] < 0.0


def test_naive_at_means_city_timezone(client: TestClient) -> None:
    naive = _shade(client, at="2026-12-21T13:20:00").json()
    aware = _shade(client, at="2026-12-21T13:20:00+01:00").json()
    assert naive == aware
    assert naive["at"].endswith("+01:00")


def test_urlencoded_offset_in_raw_query(client: TestClient) -> None:
    """A '+' in a query string is a space; %2B is the offset sign."""
    url = f"/v1/shade?city=cube&lat={NEAR_LAT}&lon={NEAR_LON}&at=2026-12-21T13:20:00%2B01:00"
    response = client.get(url)
    assert response.status_code == 200
    assert response.json()["state"] == "shade"


def test_implicit_now_is_not_cacheable(client: TestClient) -> None:
    response = _shade(client)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["state"] in ("sun", "shade", "night")


def test_point_outside_coverage_is_400(client: TestClient) -> None:
    response = _shade(client, lat=NEAR_LAT + 0.01)  # ~1.1 km north of the bbox
    assert response.status_code == 400
    assert "coverage" in response.json()["detail"]


def test_point_inside_the_bbox_but_outside_the_area_is_400(
    masked_city: Path, tmp_path: Path
) -> None:
    """The check this whole feature exists for: no data is not sunshine.

    Same bbox, same rasters; the eastern half was simply never computed, and
    its horizon cube is zeros, which the engine reads as an open sky. The API
    has to refuse it exactly as it refuses a point in the next province.

    The city YAML here carries no ``area`` on purpose: the artifact is what
    knows, so a server that has not seen the config still answers correctly.
    """
    cities_dir = tmp_path / "cities"
    cities_dir.mkdir()
    (cities_dir / "cube.yaml").write_text(yaml.safe_dump(CUBE_CITY.model_dump(mode="json")))
    settings = ApiSettings(
        cities_dir=cities_dir,
        artifacts_root=masked_city.parent.parent,
        rate_limit_enabled=False,
    )
    east = _TO_WGS84.transform(synthetic.UTM_ORIGIN[0] + 90.0, synthetic.UTM_ORIGIN[1] + 60.0)
    west = _TO_WGS84.transform(synthetic.UTM_ORIGIN[0] + 30.0, synthetic.UTM_ORIGIN[1] + 60.0)
    with TestClient(create_app(settings)) as instance:
        query = {"city": "cube", "at": "2026-06-21T14:20:00+02:00"}
        outside = instance.get("/v1/shade", params={**query, "lon": east[0], "lat": east[1]})
        assert outside.status_code == 400
        assert outside.json()["detail"] == "point outside city coverage"

        inside = instance.get("/v1/shade", params={**query, "lon": west[0], "lat": west[1]})
        assert inside.status_code == 200
        assert inside.json()["state"] in ("sun", "shade")


def test_unknown_city_is_404(client: TestClient) -> None:
    response = client.get(
        "/v1/shade", params={"city": "atlantis", "lat": NEAR_LAT, "lon": NEAR_LON}
    )
    assert response.status_code == 404


def test_validation_errors_are_422(client: TestClient) -> None:
    assert _shade(client, lat=95.0).status_code == 422
    assert _shade(client, at="not-a-date").status_code == 422
