"""Route engine unit tests: CSR build, snap, fractions, A*, assembly."""

from datetime import datetime

import networkx as nx
import numpy as np
import pytest

from shade_api.routing import RouteGraph
from shade_core.routegraph import (
    OSM_ATTRIBUTION,
    ROUTE_GRAPH_SCHEMA_VERSION,
    GraphRung,
    RouteGraphArtifact,
    RouteGraphMeta,
    RungColumn,
)

Node = tuple[float, float]
Edge = tuple[int, int, list[Node] | None]


def _ladder(times: list[str]) -> list[GraphRung]:
    """One rung covering every date, columns at the given times."""
    return [
        GraphRung(
            date="2026-03-21",
            declination_deg=0.0,
            covers=[("2026-01-01", "2026-12-31")],
            columns=[RungColumn(time=time, col=col) for col, time in enumerate(times)],
        )
    ]


def _artifact(
    nodes: list[Node],
    edges: list[Edge],
    fractions: list[list[int]],
    times: list[str] | None = None,
) -> RouteGraphArtifact:
    """Hand-built artifact; edge geometry defaults to the straight segment."""
    times = times if times is not None else ["10:00", "11:00", "12:00"]
    geoms = []
    for u, v, geometry in edges:
        points = geometry if geometry is not None else [nodes[u], nodes[v]]
        geoms.append(np.asarray(points, dtype=np.float64))
    lengths = [float(np.sum(np.hypot(np.diff(g[:, 0]), np.diff(g[:, 1])))) for g in geoms]
    offsets = np.zeros(len(edges) + 1, dtype=np.int64)
    np.cumsum([len(g) for g in geoms], out=offsets[1:])
    fractions_array = np.asarray(fractions, dtype=np.uint8)
    ladder = _ladder(times)
    return RouteGraphArtifact(
        node_x=np.asarray([x for x, _ in nodes], dtype=np.float64),
        node_y=np.asarray([y for _, y in nodes], dtype=np.float64),
        edge_u=np.asarray([u for u, _, _ in edges], dtype=np.int32),
        edge_v=np.asarray([v for _, v, _ in edges], dtype=np.int32),
        edge_len=np.asarray(lengths, dtype=np.float32),
        geom_x=np.concatenate([g[:, 0] for g in geoms]),
        geom_y=np.concatenate([g[:, 1] for g in geoms]),
        geom_offsets=offsets,
        fractions=fractions_array,
        meta=RouteGraphMeta(
            schema_version=ROUTE_GRAPH_SCHEMA_VERSION,
            city_id="test",
            crs="EPSG:25830",
            network_type="walk",
            spacing_m=5.0,
            nodes=len(nodes),
            edges=len(edges),
            samples=0,
            built_at=datetime.fromisoformat("2026-08-14T00:00:00+00:00"),
            ladder=ladder,
            attribution=[OSM_ATTRIBUTION],
        ),
    )


def _path_cost(graph: RouteGraph, path: list[int], cost: np.ndarray) -> float:
    return float(sum(cost[graph.adj_edge[k]] for k in path))


# --- A* ------------------------------------------------------------------------


def test_astar_matches_dijkstra_oracle() -> None:
    """Seeded grid with solar-style weights: A* cost equals networkx dijkstra."""
    rng = np.random.default_rng(42)
    side = 5
    nodes: list[Node] = [(float(i * 10), float(j * 10)) for i in range(side) for j in range(side)]
    edges: list[Edge] = []
    for i in range(side):
        for j in range(side):
            if i + 1 < side:
                edges.append((i * side + j, (i + 1) * side + j, None))
            if j + 1 < side:
                edges.append((i * side + j, i * side + j + 1, None))
    artifact = _artifact(nodes, edges, [[0, 0, 0]] * len(edges))
    graph = RouteGraph.build(artifact)
    # cost = length * (1 + w), w >= 0: the exact shape of the solar weight.
    cost = artifact.edge_len.astype(np.float64) * (1.0 + rng.uniform(0.0, 3.0, len(edges)))

    oracle = nx.Graph()
    for index, (u, v, _) in enumerate(edges):
        oracle.add_edge(u, v, weight=cost[index])
    for src, dst in [(0, side * side - 1), (3, 17), (side - 1, side * (side - 1))]:
        path = graph.astar(src, dst, cost)
        assert path is not None
        expected = nx.dijkstra_path_length(oracle, src, dst)
        assert _path_cost(graph, path, cost) == pytest.approx(expected)


def test_astar_alpha_flips_to_the_shaded_parallel_route() -> None:
    """Sunny straight (60 m, fraction 1) vs shaded arc (100 m, fraction 0):
    the flip sits at alpha = 2/3, so alpha 0 goes straight and alpha 1 arcs."""
    nodes: list[Node] = [(0.0, 0.0), (60.0, 0.0)]
    edges: list[Edge] = [
        (0, 1, None),
        (0, 1, [(0.0, 0.0), (30.0, 40.0), (60.0, 0.0)]),
    ]
    artifact = _artifact(nodes, edges, [[255, 255, 255], [0, 0, 0]])
    graph = RouteGraph.build(artifact)
    fractions = graph.fractions_at(datetime.fromisoformat("2026-07-01T11:00"))
    lengths = artifact.edge_len.astype(np.float64)

    direct = graph.astar(0, 1, lengths * (1.0 + 0.0 * fractions))
    shaded = graph.astar(0, 1, lengths * (1.0 + 1.0 * fractions))
    assert direct is not None and shaded is not None
    assert int(graph.adj_edge[direct[0]]) == 0  # alpha 0: the 60 m straight
    assert int(graph.adj_edge[shaded[0]]) == 1  # alpha 1: the 100 m shaded arc

    leg = graph.assemble(shaded, fractions)
    assert leg.length_m == pytest.approx(100.0)
    assert leg.sun_fraction == pytest.approx(0.0)


def test_astar_prefers_cheaper_parallel_edge() -> None:
    nodes: list[Node] = [(0.0, 0.0), (10.0, 0.0)]
    edges: list[Edge] = [(0, 1, None), (0, 1, [(0.0, 0.0), (5.0, 5.0), (10.0, 0.0)])]
    artifact = _artifact(nodes, edges, [[0] * 3, [0] * 3])
    graph = RouteGraph.build(artifact)
    cost = np.array([30.0, 15.0])  # the longer arc is the cheaper hop
    path = graph.astar(0, 1, cost)
    assert path is not None and len(path) == 1
    assert int(graph.adj_edge[path[0]]) == 1


def test_astar_unreachable_returns_none() -> None:
    nodes: list[Node] = [(0.0, 0.0), (10.0, 0.0), (100.0, 0.0), (110.0, 0.0)]
    edges: list[Edge] = [(0, 1, None), (2, 3, None)]
    artifact = _artifact(nodes, edges, [[0] * 3] * 2)
    graph = RouteGraph.build(artifact)
    assert graph.astar(0, 3, artifact.edge_len.astype(np.float64)) is None


def test_astar_same_node_is_empty_and_trivial_leg() -> None:
    nodes: list[Node] = [(3.0, 4.0), (10.0, 4.0)]
    artifact = _artifact(nodes, [(0, 1, None)], [[0] * 3])
    graph = RouteGraph.build(artifact)
    assert graph.astar(0, 0, artifact.edge_len.astype(np.float64)) == []
    leg = graph.trivial_leg(0)
    assert leg.length_m == 0.0
    assert leg.sun_fraction == 0.0
    assert leg.xs.tolist() == [3.0, 3.0]


def test_astar_rejects_costs_below_lengths() -> None:
    """The admissibility guard: normalized weights must scale the heuristic."""
    nodes: list[Node] = [(0.0, 0.0), (10.0, 0.0)]
    artifact = _artifact(nodes, [(0, 1, None)], [[0] * 3])
    graph = RouteGraph.build(artifact)
    with pytest.raises(ValueError, match="heuristic"):
        graph.astar(0, 1, np.array([5.0]))


# --- assembly ------------------------------------------------------------------


def test_assemble_orients_and_joins_geometry() -> None:
    """A -> B -> C with B-C stored backwards: one polyline, joints deduped."""
    nodes: list[Node] = [(0.0, 0.0), (10.0, 0.0), (20.0, 0.0)]
    edges: list[Edge] = [
        (0, 1, [(0.0, 0.0), (5.0, 2.0), (10.0, 0.0)]),
        (2, 1, [(20.0, 0.0), (15.0, 2.0), (10.0, 0.0)]),  # stored C -> B
    ]
    artifact = _artifact(nodes, edges, [[255] * 3, [0] * 3])
    graph = RouteGraph.build(artifact)
    fractions = graph.fractions_at(datetime.fromisoformat("2026-07-01T11:00"))
    path = graph.astar(0, 2, artifact.edge_len.astype(np.float64))
    assert path is not None
    leg = graph.assemble(path, fractions)
    assert leg.xs.tolist() == [0.0, 5.0, 10.0, 15.0, 20.0]
    assert leg.ys.tolist() == [0.0, 2.0, 0.0, 2.0, 0.0]
    # Sun accounting: only the first edge is sunny.
    lengths = artifact.edge_len.astype(np.float64)
    assert leg.length_m == pytest.approx(float(lengths.sum()))
    assert leg.sun_length_m == pytest.approx(float(lengths[0]))


# --- snapping ------------------------------------------------------------------


def test_nearest_node() -> None:
    nodes: list[Node] = [(0.0, 0.0), (100.0, 0.0), (0.0, 50.0)]
    artifact = _artifact(nodes, [(0, 1, None), (0, 2, None)], [[0] * 3] * 2)
    graph = RouteGraph.build(artifact)
    node, distance = graph.nearest_node(90.0, 10.0)
    assert node == 1
    assert distance == pytest.approx(np.hypot(10.0, 10.0))


# --- fraction resolution -------------------------------------------------------


def test_fractions_interpolate_between_columns() -> None:
    artifact = _artifact([(0.0, 0.0), (10.0, 0.0)], [(0, 1, None)], [[0, 255, 255]])
    graph = RouteGraph.build(artifact)
    assert graph.fractions_at(datetime.fromisoformat("2026-05-10T10:30"))[0] == pytest.approx(
        0.5, abs=1e-3
    )
    assert graph.fractions_at(datetime.fromisoformat("2026-05-10T10:00"))[0] == 0.0
    assert graph.fractions_at(datetime.fromisoformat("2026-05-10T11:45"))[0] == 1.0


def test_fractions_clamp_outside_daylight_columns() -> None:
    artifact = _artifact([(0.0, 0.0), (10.0, 0.0)], [(0, 1, None)], [[51, 102, 204]])
    graph = RouteGraph.build(artifact)
    assert graph.fractions_at(datetime.fromisoformat("2026-05-10T06:00"))[0] == pytest.approx(
        51 / 255
    )
    assert graph.fractions_at(datetime.fromisoformat("2026-05-10T23:00"))[0] == pytest.approx(
        204 / 255
    )


def test_fractions_resolve_other_years_and_leap_day() -> None:
    artifact = _artifact([(0.0, 0.0), (10.0, 0.0)], [(0, 1, None)], [[0, 128, 255]])
    graph = RouteGraph.build(artifact)
    # Any year maps into the ladder year through month/day; Feb 29 borrows
    # Feb 28 (the ladder year 2026 is not a leap year).
    assert graph.fractions_at(datetime.fromisoformat("2031-11-03T11:00"))[0] == pytest.approx(
        128 / 255
    )
    assert graph.fractions_at(datetime.fromisoformat("2028-02-29T11:00"))[0] == pytest.approx(
        128 / 255
    )
