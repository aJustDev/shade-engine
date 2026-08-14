"""``GET /v1/routes/shaded`` against the routed cube fixture."""

import itertools
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


def _without_segments(leg: dict[str, Any]) -> dict[str, Any]:
    """The two legs are the same route; only the active one is decomposed."""
    return {key: value for key, value in leg.items() if key != "segments"}


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
    # The snapped point is reported back so clients can draw pin -> network.
    origin_lon, origin_lat = graph_fixture.lonlat(graph_fixture.NORTH_A)
    assert body["origin"]["snapped_lat"] == pytest.approx(origin_lat, abs=1e-5)
    assert body["origin"]["snapped_lon"] == pytest.approx(origin_lon, abs=1e-5)
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
    assert _without_segments(body["shaded"]) == _without_segments(body["shortest"])
    assert body["shaded"]["sun_fraction"] == 0.0


def test_alpha_zero_returns_shortest_twice(routes_client: TestClient) -> None:
    response = _route(routes_client, alpha=0.0)
    assert response.status_code == 200
    body = response.json()
    assert _without_segments(body["shaded"]) == _without_segments(body["shortest"])


def test_same_snap_point_is_zero_length(routes_client: TestClient) -> None:
    response = _route(routes_client, to=_point(graph_fixture.NORTH_A))
    assert response.status_code == 200
    body = response.json()
    assert body["shaded"]["length_m"] == 0.0
    assert len(body["shaded"]["geometry"]["coordinates"]) == 2


def test_route_snaps_to_edge_interior(routes_client: TestClient) -> None:
    """A pin 3 m off the middle of the pocket edge starts the route there,
    not at the junction 6.7 m away that node snapping would have picked."""
    response = _route(
        routes_client,
        **{"from": _point((60.0, 63.0)), "to": _point(graph_fixture.POCKET_B)},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["origin"]["snap_distance_m"] == pytest.approx(3.0, abs=0.1)
    expected_lon, expected_lat = graph_fixture.lonlat((60.0, 60.0))
    assert body["origin"]["snapped_lat"] == pytest.approx(expected_lat, abs=1e-5)
    assert body["origin"]["snapped_lon"] == pytest.approx(expected_lon, abs=1e-5)
    first = body["shaded"]["geometry"]["coordinates"][0]
    assert first == pytest.approx([expected_lon, expected_lat], abs=1e-5)
    assert body["shaded"]["length_m"] == pytest.approx(6.0, abs=0.1)


def test_route_same_edge_partial_leg(routes_client: TestClient) -> None:
    """Both pins on one edge: the leg is the stretch between them."""
    response = _route(
        routes_client,
        **{"from": _point((57.0, 61.0)), "to": _point((63.0, 61.0))},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["shaded"]["length_m"] == pytest.approx(6.0, abs=0.1)
    assert body["shaded"]["geometry"]["coordinates"] == body["shortest"]["geometry"]["coordinates"]


def test_shaded_leg_carries_its_segments(routes_client: TestClient) -> None:
    """The active leg ships its per-edge decomposition, ready to colour."""
    body = _route(routes_client).json()
    segments = body["shaded"]["segments"]
    assert len(segments) >= 2  # the default query crosses several edges
    for segment in segments:
        assert segment["geometry"]["type"] == "LineString"
        assert len(segment["geometry"]["coordinates"]) >= 2
        assert segment["length_m"] > 0
        assert 0.0 <= segment["sun_fraction"] <= 1.0
        assert segment["sun_fraction"] + segment["veg_shade_fraction"] <= 1.0 + 1e-6


def test_segment_lengths_sum_to_the_leg(routes_client: TestClient) -> None:
    body = _route(routes_client).json()
    leg = body["shaded"]
    total = sum(segment["length_m"] for segment in leg["segments"])
    # Each segment rounds to 0.1 m on its own, so the drift grows with the
    # count: a fixed abs=0.1 would be flaky on a real city's 40 segments.
    tolerance = 0.05 * len(leg["segments"]) + 0.05
    assert total == pytest.approx(leg["length_m"], abs=tolerance)


def test_segments_chain_into_the_leg_geometry(routes_client: TestClient) -> None:
    """Neighbours share their joint vertex: this is what lets a client
    stitch the pieces back into the leg (and merge runs by class)."""
    leg = _route(routes_client).json()["shaded"]
    segments = leg["segments"]
    assert segments[0]["geometry"]["coordinates"][0] == leg["geometry"]["coordinates"][0]
    assert segments[-1]["geometry"]["coordinates"][-1] == leg["geometry"]["coordinates"][-1]
    for earlier, later in itertools.pairwise(segments):
        assert earlier["geometry"]["coordinates"][-1] == later["geometry"]["coordinates"][0]


def test_reference_legs_have_no_segments(routes_client: TestClient) -> None:
    """Only the coloured route is decomposed; the rest are reference lines."""
    body = _route(routes_client, alternatives="true").json()
    assert body["shortest"]["segments"] is None
    assert all(alternative["segments"] is None for alternative in body["alternatives"])


def test_night_segments_report_no_sun(routes_client: TestClient) -> None:
    """At night the segments still travel, all zero: there is nothing to
    classify, and a naive argmax would paint the city as building shade."""
    body = _route(routes_client, at="2026-12-21T03:00").json()
    segments = body["shaded"]["segments"]
    assert segments
    assert all(segment["sun_fraction"] == 0.0 for segment in segments)
    assert all(segment["veg_shade_fraction"] == 0.0 for segment in segments)


def test_zero_length_route_has_no_segments(routes_client: TestClient) -> None:
    body = _route(routes_client, to=_point(graph_fixture.NORTH_A)).json()
    assert body["shaded"]["segments"] == []


def test_beta_above_alpha_is_400(routes_client: TestClient) -> None:
    """Sun must stay at least as unwelcome as building shade."""
    response = _route(routes_client, alpha=1.0, beta=2.0)
    assert response.status_code == 400
    assert "must not exceed alpha" in response.json()["detail"]


def test_beta_out_of_bounds_is_422(routes_client: TestClient) -> None:
    assert _route(routes_client, beta=-1.0).status_code == 422
    assert _route(routes_client, alpha=10.0, beta=11.0).status_code == 422


def test_response_carries_beta_and_veg_breakdown(routes_client: TestClient) -> None:
    """The cube has no canopy, so the breakdown is all sun and built shade."""
    response = _route(routes_client, alpha=1.0, beta=0.5)
    assert response.status_code == 200
    body = response.json()
    assert body["beta"] == 0.5
    for leg in (body["shaded"], body["shortest"]):
        assert leg["veg_shade_length_m"] == 0.0
        assert leg["sun_length_m"] + leg["veg_shade_length_m"] <= leg["length_m"] + 1e-6


def test_pareto_front_drops_dominated_routes() -> None:
    """Longer AND sunnier than a sibling means nobody would ever pick it."""
    import numpy as np

    from shade_api.routing import RouteLeg
    from shade_api.shaded_routes import _pareto_front

    def leg(length: float, sun: float) -> RouteLeg:
        return RouteLeg(
            xs=np.zeros(2),
            ys=np.zeros(2),
            length_m=length,
            sun_length_m=sun,
            veg_shade_length_m=0.0,
        )

    front = _pareto_front(
        [
            (0.0, leg(100.0, 80.0)),  # shortest, sunniest: survives
            (1.0, leg(120.0, 90.0)),  # longer AND sunnier: dominated
            (2.0, leg(140.0, 20.0)),  # buys real shade: survives
            (4.0, leg(150.0, 20.0)),  # same sun for more length: dominated
        ]
    )
    assert [(round(item[1].length_m), round(item[1].sun_length_m)) for item in front] == [
        (100, 80),
        (140, 20),
    ]


def test_pareto_front_collapses_indistinguishable_offers() -> None:
    """Neighboring alphas often differ by a couple of meters of sun over a
    kilometer: technically non-dominated, but the same offer on screen."""
    import numpy as np

    from shade_api.routing import RouteLeg
    from shade_api.shaded_routes import _pareto_front

    def leg(length: float, sun: float) -> RouteLeg:
        return RouteLeg(
            xs=np.zeros(2),
            ys=np.zeros(2),
            length_m=length,
            sun_length_m=sun,
            veg_shade_length_m=0.0,
        )

    front = _pareto_front(
        [
            (0.5, leg(1471.8, 299.2)),
            (1.0, leg(1474.2, 294.7)),  # 2 m longer, 4 m less sun: same offer
            (2.0, leg(1572.4, 214.3)),  # a real step down in sun: kept
        ]
    )
    assert [round(item[1].sun_length_m) for item in front] == [299, 214]


def test_alternatives_absent_by_default(routes_client: TestClient) -> None:
    assert _route(routes_client).json()["alternatives"] is None


def test_alternatives_are_sorted_and_nondominated(routes_client: TestClient) -> None:
    """Every returned route must be the best at something: sorted by length,
    each one strictly less sunny than the shorter ones before it."""
    response = _route(routes_client, alternatives="true")
    assert response.status_code == 200
    body = response.json()
    alternatives = body["alternatives"]
    assert alternatives
    lengths = [alt["length_m"] for alt in alternatives]
    suns = [alt["sun_length_m"] for alt in alternatives]
    assert lengths == sorted(lengths)
    assert all(later < earlier for earlier, later in itertools.pairwise(suns))
    # No duplicate offers, and each carries the alpha that produced it.
    assert len({(a["length_m"], a["sun_length_m"]) for a in alternatives}) == len(alternatives)
    for alt in alternatives:
        assert 0.0 <= alt["alpha"] <= 10.0
        assert "veg_shade_length_m" in alt
    # The cheapest offer is the shortest route.
    assert alternatives[0]["length_m"] == pytest.approx(body["shortest"]["length_m"])


def test_alternatives_at_night_is_a_single_route(routes_client: TestClient) -> None:
    body = _route(routes_client, at="2026-12-21T03:00", alternatives="true").json()
    assert body["status"] == "night"
    assert len(body["alternatives"]) == 1
    assert body["alternatives"][0]["length_m"] == body["shortest"]["length_m"]


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
