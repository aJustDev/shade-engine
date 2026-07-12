"""/v1/parking/nearby against a synthetic layer in the cube fixture city.

The real Cordoba zones fall outside the cube fixture's raster, so the
layer here is synthetic: one zone inside the cube's winter-noon shadow
(around the golden NEAR point of test_api_shade), one in open ground, and
one off the raster entirely. DB-backed tests skip without a PostGIS server
(they always run in CI); the settings tests run everywhere.
"""

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from pyproj import Transformer
from sqlalchemy import Engine

import synthetic
from shade_api.app import create_app
from shade_api.settings import ApiSettings
from shade_core.config import load_city
from shade_pipeline.layers import import_parking_layer

_TO_WGS84 = Transformer.from_crs("EPSG:25830", "EPSG:4326", always_xy=True)

# The golden NEAR point of test_api_shade: 10 m north of the cube wall.
_NEAR_X = synthetic.UTM_ORIGIN[0] + synthetic.QUERY_X
_NEAR_Y = synthetic.UTM_ORIGIN[1] + synthetic.CUBE_NORTH_WALL_Y + 10.0
NEAR_LON, NEAR_LAT = _TO_WGS84.transform(_NEAR_X, _NEAR_Y)

WINTER_NOON = "2026-12-21T13:20:00+01:00"

# Three zones, in expected distance order from the NEAR query point:
# 2 m of street straight through the golden point (building shade at winter
# noon), 6 m of open ground well west of the shadow band (~39 m away), and
# a zone beyond the raster's east edge (~87 m away, shade must be null).
SHADED_LINE = [(_NEAR_X - 1.0, _NEAR_Y), (_NEAR_X + 1.0, _NEAR_Y)]
SUNNY_LINE = [
    (synthetic.UTM_ORIGIN[0] + 30.0, _NEAR_Y + 25.0),
    (synthetic.UTM_ORIGIN[0] + 36.0, _NEAR_Y + 25.0),
]
OFFGRID_LINE = [
    (synthetic.UTM_ORIGIN[0] + 150.0, _NEAR_Y),
    (synthetic.UTM_ORIGIN[0] + 156.0, _NEAR_Y),
]


def _feature(name: str, line_utm: list[tuple[float, float]]) -> dict[str, Any]:
    return {
        "type": "Feature",
        "properties": {
            "name": name,
            "zone_type": "blue",
            "orientation": "cordon",
            "capacity": 8,
            "schedule": [{"days": "mo-fr", "from": "09:00", "to": "14:00"}],
            "max_minutes": 120,
            "tariff_eur_hour": 0.9,
            "notes": None,
            "source": "synthetic",
            "last_verified": "2026-07-12",
        },
        "geometry": {
            "type": "MultiLineString",
            "coordinates": [[list(_TO_WGS84.transform(x, y)) for x, y in line_utm]],
        },
    }


@pytest.fixture(scope="module")
def client_with_db(
    api_settings: ApiSettings,
    parking_db: Engine,
    tmp_path_factory: pytest.TempPathFactory,
) -> Any:
    """Client whose app talks to the scratch DB, with the layer imported."""
    layer = tmp_path_factory.mktemp("parking_layer") / "parking.geojson"
    layer.write_text(
        json.dumps(
            {
                "type": "FeatureCollection",
                "features": [
                    _feature("SHADED STREET", SHADED_LINE),
                    _feature("SUNNY STREET", SUNNY_LINE),
                    _feature("OFFGRID STREET", OFFGRID_LINE),
                ],
            }
        )
    )
    config = load_city(Path(api_settings.cities_dir) / "cube.yaml")
    import_parking_layer(config, layer, parking_db)
    url = parking_db.url.render_as_string(hide_password=False)
    settings = api_settings.model_copy(update={"database_url": url})
    with TestClient(create_app(settings)) as instance:
        yield instance


def _nearby(client: TestClient, **params: Any) -> httpx.Response:
    query: dict[str, Any] = {"city": "cube", "lat": NEAR_LAT, "lon": NEAR_LON, **params}
    response: httpx.Response = client.get("/v1/parking/nearby", params=query)
    return response


def test_nearby_matches_the_shade_endpoint(client_with_db: TestClient) -> None:
    """Exit criterion of the phase: zone verdicts agree with /v1/shade."""
    response = _nearby(client_with_db, at=WINTER_NOON)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "public, max-age=60"
    body = response.json()
    assert [zone["name"] for zone in body["zones"]] == [
        "SHADED STREET",
        "SUNNY STREET",
        "OFFGRID STREET",
    ]
    shaded, sunny, offgrid = body["zones"]

    point = client_with_db.get(
        "/v1/shade",
        params={"city": "cube", "lat": NEAR_LAT, "lon": NEAR_LON, "at": WINTER_NOON},
    ).json()
    assert point["state"] == "shade"
    assert shaded["shade"]["state"] == "shade"
    assert shaded["shade"]["in_shade"] is True
    assert shaded["shade"]["shade_fraction"] == 1.0
    assert sunny["shade"]["state"] == "sun"
    assert sunny["shade"]["shade_fraction"] == 0.0
    assert sunny["shade"]["shaded_until"] is None
    assert offgrid["shade"] is None

    assert shaded["distance_m"] < sunny["distance_m"] < offgrid["distance_m"]
    assert shaded["schedule"] == [{"days": "mo-fr", "from": "09:00", "to": "14:00"}]
    assert shaded["geometry"]["type"] == "MultiLineString"
    assert body["attribution"] == ["Synthetic LiDAR (test fixture)"]


def test_shaded_until_flips_the_verdict(client_with_db: TestClient) -> None:
    """Re-querying at shaded_until must show the shade run has ended."""
    first = _nearby(client_with_db, at=WINTER_NOON).json()
    until = first["zones"][0]["shade"]["shaded_until"]
    assert until is not None
    later = _nearby(client_with_db, at=until).json()
    flipped = {zone["name"]: zone for zone in later["zones"]}["SHADED STREET"]
    assert flipped["shade"]["state"] != "shade"


def test_night_zones_skip_sampling(client_with_db: TestClient) -> None:
    body = _nearby(client_with_db, at="2026-12-21T03:00:00").json()
    for zone in body["zones"]:
        if zone["shade"] is None:  # offgrid stays null even at night
            continue
        assert zone["shade"]["state"] == "night"
        assert zone["shade"]["shade_fraction"] is None
        assert zone["shade"]["shaded_until"] is None


def test_radius_excludes_far_zones(client_with_db: TestClient) -> None:
    body = _nearby(client_with_db, at=WINTER_NOON, radius=15).json()
    assert [zone["name"] for zone in body["zones"]] == ["SHADED STREET"]
    assert body["radius_m"] == 15.0


def test_naive_at_means_city_timezone(client_with_db: TestClient) -> None:
    naive = _nearby(client_with_db, at="2026-12-21T13:20:00").json()
    aware = _nearby(client_with_db, at=WINTER_NOON).json()
    assert naive == aware


def test_implicit_now_is_not_cacheable(client_with_db: TestClient) -> None:
    response = _nearby(client_with_db)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"


def test_unknown_city_is_404(client_with_db: TestClient) -> None:
    response = client_with_db.get(
        "/v1/parking/nearby", params={"city": "atlantis", "lat": NEAR_LAT, "lon": NEAR_LON}
    )
    assert response.status_code == 404


def test_radius_above_cap_is_422(client_with_db: TestClient) -> None:
    assert _nearby(client_with_db, radius=5000).status_code == 422


def test_without_database_is_503(client: TestClient) -> None:
    """The shared DB-less client: everything works except parking."""
    response = client.get(
        "/v1/parking/nearby", params={"city": "cube", "lat": NEAR_LAT, "lon": NEAR_LON}
    )
    assert response.status_code == 503
    assert client.get("/healthz").status_code == 200


def test_database_url_reads_the_unprefixed_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHADE_DATABASE_URL", "postgresql+psycopg://env/db")
    assert ApiSettings().database_url == "postgresql+psycopg://env/db"


def test_database_url_alias_beats_the_prefixed_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """populate_by_name lets SHADE_API_DATABASE_URL work too; the alias wins."""
    monkeypatch.setenv("SHADE_API_DATABASE_URL", "postgresql+psycopg://prefixed/db")
    assert ApiSettings().database_url == "postgresql+psycopg://prefixed/db"
    monkeypatch.setenv("SHADE_DATABASE_URL", "postgresql+psycopg://alias/db")
    assert ApiSettings().database_url == "postgresql+psycopg://alias/db"


def test_database_url_accepts_keyword_construction() -> None:
    assert ApiSettings(database_url="postgresql+psycopg://kw/db").database_url == (
        "postgresql+psycopg://kw/db"
    )
