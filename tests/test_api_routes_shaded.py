"""``GET /v1/routes/shaded`` against the routed cube fixture."""

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient

import graph_fixture
from conftest import CUBE_CITY
from shade_api.app import create_app
from shade_api.settings import ApiSettings
from shade_core.routegraph import OSM_ATTRIBUTION

WINTER_NOON = "2026-12-21T13:00"


@pytest.fixture(scope="module")
def routes_client(
    routed_city: Path, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[TestClient]:
    cities_dir = tmp_path_factory.mktemp("routes_cities")
    (cities_dir / "cube.yaml").write_text(yaml.safe_dump(CUBE_CITY.model_dump(mode="json")))
    settings = ApiSettings(
        cities_dir=cities_dir,
        artifacts_root=routed_city.parent.parent,
        rate_limit_enabled=False,
    )
    with TestClient(create_app(settings)) as instance:
        yield instance


def _point(local: tuple[float, float]) -> str:
    lon, lat = graph_fixture.lonlat(local)
    return f"{lat},{lon}"


def _route(client: TestClient, **overrides: Any) -> Any:
    params: dict[str, Any] = {
        "city": "cube",
        "from": _point(graph_fixture.NORTH_A),
        "to": _point(graph_fixture.POCKET_B),
        "at": WINTER_NOON,
        "alpha": 1.0,
    }
    params.update(overrides)
    params = {key: value for key, value in params.items() if value is not None}
    return client.get("/v1/routes/shaded", params=params)


def test_route_shape_and_invariants(routes_client: TestClient) -> None:
    response = _route(routes_client)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "public, max-age=86400"
    body = response.json()
    assert body["city"] == "cube"
    assert body["status"] == "ok"
    assert body["alpha"] == 1.0
    assert body["sun"]["elevation_deg"] > 0
    assert body["origin"]["snap_distance_m"] < 1.0
    assert OSM_ATTRIBUTION in body["attribution"]
    assert CUBE_CITY.attribution[0] in body["attribution"]

    shaded, shortest = body["shaded"], body["shortest"]
    for leg in (shaded, shortest):
        assert leg["geometry"]["type"] == "LineString"
        assert len(leg["geometry"]["coordinates"]) >= 2
        assert leg["length_m"] > 0
        assert 0.0 <= leg["sun_fraction"] <= 1.0
    # The defining invariants of the pair: the shaded route may only buy
    # less sun by walking more.
    assert shaded["length_m"] >= shortest["length_m"] - 1e-6
    assert shaded["sun_fraction"] <= shortest["sun_fraction"] + 1e-6
    # Both legs run between the same snapped endpoints.
    assert shaded["geometry"]["coordinates"][0] == shortest["geometry"]["coordinates"][0]
    assert shaded["geometry"]["coordinates"][-1] == shortest["geometry"]["coordinates"][-1]


def test_pocket_route_is_shaded_at_winter_noon(routes_client: TestClient) -> None:
    """The pocket edge sits deep inside the cube's winter shadow: routing
    along it must report almost no sun, and its length is the edge's 12 m."""
    response = _route(
        routes_client,
        **{"from": _point(graph_fixture.POCKET_A), "to": _point(graph_fixture.POCKET_B)},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["shaded"]["length_m"] == pytest.approx(12.0, abs=0.1)
    assert body["shaded"]["sun_fraction"] < 0.2


def test_night_routes_are_identical(routes_client: TestClient) -> None:
    response = _route(routes_client, at="2026-12-21T03:00")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "night"
    assert body["sun"]["elevation_deg"] < 0
    assert body["shaded"] == body["shortest"]
    assert body["shaded"]["sun_fraction"] == 0.0


def test_alpha_zero_returns_shortest_twice(routes_client: TestClient) -> None:
    response = _route(routes_client, alpha=0.0)
    assert response.status_code == 200
    body = response.json()
    assert body["shaded"] == body["shortest"]


def test_same_snap_node_is_zero_length(routes_client: TestClient) -> None:
    response = _route(routes_client, to=_point(graph_fixture.NORTH_A))
    assert response.status_code == 200
    body = response.json()
    assert body["shaded"]["length_m"] == 0.0
    assert len(body["shaded"]["geometry"]["coordinates"]) == 2


def test_snap_beyond_threshold_is_400(
    routes_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The cube bbox is only 80 m wide, so no in-coverage point is 400 m from
    # a node; shrink the threshold to exercise the rejection.
    monkeypatch.setattr("shade_api.shaded_routes.SNAP_MAX_M", 10.0)
    response = _route(routes_client, **{"from": _point((21.0, 21.0))})
    assert response.status_code == 400
    assert "no walkable path within" in response.json()["detail"]
    assert "origin" in response.json()["detail"]


def test_malformed_points_are_400(routes_client: TestClient) -> None:
    for bad in ("37.9", "a,b", "95,0", "0,181"):
        response = _route(routes_client, **{"from": bad})
        assert response.status_code == 400, bad
        assert "from" in response.json()["detail"]


def test_outside_coverage_is_400(routes_client: TestClient) -> None:
    response = _route(routes_client, to=_point((2000.0, 2000.0)))
    assert response.status_code == 400
    assert response.json()["detail"] == "point outside city coverage"


def test_unknown_city_is_404(routes_client: TestClient) -> None:
    assert _route(routes_client, city="atlantis").status_code == 404


def test_alpha_out_of_bounds_is_422(routes_client: TestClient) -> None:
    assert _route(routes_client, alpha=-0.5).status_code == 422
    assert _route(routes_client, alpha=11).status_code == 422


def test_at_omitted_is_no_store(routes_client: TestClient) -> None:
    response = _route(routes_client, at=None)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"


def test_city_without_graph_is_503(client: TestClient) -> None:
    """The session client serves built_city, which has no graph/ directory."""
    response = client.get(
        "/v1/routes/shaded",
        params={
            "city": "cube",
            "from": _point(graph_fixture.NORTH_A),
            "to": _point(graph_fixture.POCKET_B),
        },
    )
    assert response.status_code == 503
    assert "shade-engine graph" in response.json()["detail"]
