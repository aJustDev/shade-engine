"""Route engine unit tests: CSR build, snap, fractions, A*, assembly."""

from datetime import datetime

import networkx as nx
import numpy as np
import pytest

from shade_api.routing import EdgePoint, EdgeSpan, RouteGraph
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
    veg: list[list[int]] | None = None,
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
    veg_array = (
        np.asarray(veg, dtype=np.uint8)
        if veg is not None
        else np.zeros_like(fractions_array, dtype=np.uint8)
    )
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
        veg_fractions=veg_array,
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


def test_astar_same_node_is_empty() -> None:
    nodes: list[Node] = [(3.0, 4.0), (10.0, 4.0)]
    artifact = _artifact(nodes, [(0, 1, None)], [[0] * 3])
    graph = RouteGraph.build(artifact)
    assert graph.astar(0, 0, artifact.edge_len.astype(np.float64)) == []


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


def test_assemble_spans_reports_one_segment_per_span() -> None:
    """A -> B -> C: two segments, each with its own edge's fractions."""
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

    assert len(leg.segments) == 2
    lengths = artifact.edge_len.astype(np.float64)
    assert leg.segments[0].length_m == pytest.approx(float(lengths[0]))
    assert leg.segments[0].sun_fraction == pytest.approx(1.0)
    assert leg.segments[1].sun_fraction == pytest.approx(0.0)
    assert sum(s.length_m for s in leg.segments) == pytest.approx(leg.length_m)


def test_segments_rebuild_the_leg_polyline() -> None:
    """Dropping each follower's joint vertex reproduces the leg exactly."""
    nodes: list[Node] = [(0.0, 0.0), (10.0, 0.0), (20.0, 0.0)]
    edges: list[Edge] = [
        (0, 1, [(0.0, 0.0), (5.0, 2.0), (10.0, 0.0)]),
        (2, 1, [(20.0, 0.0), (15.0, 2.0), (10.0, 0.0)]),
    ]
    artifact = _artifact(nodes, edges, [[0] * 3] * 2)
    graph = RouteGraph.build(artifact)
    fractions = graph.fractions_at(datetime.fromisoformat("2026-07-01T11:00"))
    path = graph.astar(0, 2, artifact.edge_len.astype(np.float64))
    assert path is not None
    leg = graph.assemble(path, fractions)

    xs = np.concatenate([leg.segments[0].xs, *[s.xs[1:] for s in leg.segments[1:]]])
    ys = np.concatenate([leg.segments[0].ys, *[s.ys[1:] for s in leg.segments[1:]]])
    assert xs.tolist() == leg.xs.tolist()
    assert ys.tolist() == leg.ys.tolist()
    for earlier, later in zip(leg.segments, leg.segments[1:], strict=False):
        assert earlier.xs[-1] == later.xs[0]
        assert earlier.ys[-1] == later.ys[0]


def test_segment_keeps_its_edge_fractions_on_a_partial_span() -> None:
    """Half an edge is charged pro rata, but reports the edge's fractions
    unscaled: a client colours by how sunny the street is, not by how much
    of it was walked."""
    nodes: list[Node] = [(0.0, 0.0), (100.0, 0.0)]
    artifact = _artifact(nodes, [(0, 1, None)], [[0] * 3])
    graph = RouteGraph.build(artifact)
    leg = graph.assemble_spans(
        [EdgeSpan(edge=0, s_from=0.0, s_to=50.0)],
        np.array([0.2], dtype=np.float32),
        np.array([0.5], dtype=np.float32),
    )
    assert len(leg.segments) == 1
    assert leg.segments[0].length_m == pytest.approx(50.0)
    assert leg.segments[0].sun_fraction == pytest.approx(0.2)
    assert leg.segments[0].veg_shade_fraction == pytest.approx(0.5)


# --- snapping ------------------------------------------------------------------


def test_snap_point_projects_onto_edge_interior() -> None:
    """A pin beside a street lands on the street, not on the far junction."""
    nodes: list[Node] = [(0.0, 0.0), (100.0, 0.0), (0.0, 50.0)]
    artifact = _artifact(nodes, [(0, 1, None), (0, 2, None)], [[0] * 3] * 2)
    graph = RouteGraph.build(artifact)
    point = graph.snap_point(60.0, 8.0)
    assert point.edge == 0
    assert point.s_m == pytest.approx(60.0)
    assert (point.x, point.y) == pytest.approx((60.0, 0.0))
    assert point.distance_m == pytest.approx(8.0)


def test_snap_point_prefers_closer_parallel_geometry() -> None:
    """Straight and arc between the same nodes: the arc wins when nearer."""
    nodes: list[Node] = [(0.0, 0.0), (100.0, 0.0)]
    edges: list[Edge] = [(0, 1, None), (0, 1, [(0.0, 0.0), (50.0, 40.0), (100.0, 0.0)])]
    artifact = _artifact(nodes, edges, [[0] * 3] * 2)
    graph = RouteGraph.build(artifact)
    assert graph.snap_point(50.0, 38.0).edge == 1
    assert graph.snap_point(50.0, 2.0).edge == 0


def test_snap_point_clamps_beyond_segment_ends() -> None:
    """Past the end the projection clamps to the vertex; zero-length
    segments (repeated OSM vertices) must not divide by zero."""
    nodes: list[Node] = [(0.0, 0.0), (10.0, 0.0)]
    edges: list[Edge] = [(0, 1, [(0.0, 0.0), (5.0, 0.0), (5.0, 0.0), (10.0, 0.0)])]
    artifact = _artifact(nodes, edges, [[0] * 3])
    graph = RouteGraph.build(artifact)
    point = graph.snap_point(40.0, 3.0)
    assert point.s_m == pytest.approx(10.0)
    assert (point.x, point.y) == pytest.approx((10.0, 0.0))
    assert point.distance_m == pytest.approx(float(np.hypot(30.0, 3.0)))


# --- routing between points on edges -------------------------------------------


def _edge_point(artifact: RouteGraphArtifact, edge: int, s_m: float) -> EdgePoint:
    """An EdgePoint at arc length s_m, with the matching coordinates."""
    start, stop = int(artifact.geom_offsets[edge]), int(artifact.geom_offsets[edge + 1])
    xs, ys = artifact.geom_x[start:stop], artifact.geom_y[start:stop]
    arc = np.concatenate([[0.0], np.cumsum(np.hypot(np.diff(xs), np.diff(ys)))])
    return EdgePoint(
        edge=edge,
        s_m=s_m,
        x=float(np.interp(s_m, arc, xs)),
        y=float(np.interp(s_m, arc, ys)),
        distance_m=0.0,
    )


def _spans_cost(artifact: RouteGraphArtifact, spans: list[EdgeSpan], cost: np.ndarray) -> float:
    """What the walked spans cost, charging each edge pro rata."""
    return float(
        sum(
            abs(span.s_to - span.s_from) * cost[span.edge] / float(artifact.edge_len[span.edge])
            for span in spans
        )
    )


def _virtual_oracle(
    artifact: RouteGraphArtifact, cost: np.ndarray, src: EdgePoint, dst: EdgePoint
) -> float:
    """Brute-force cost with the virtual endpoints spelled out as nodes."""
    oracle = nx.MultiGraph()
    for index in range(len(artifact.edge_len)):
        oracle.add_edge(
            int(artifact.edge_u[index]), int(artifact.edge_v[index]), weight=float(cost[index])
        )
    for label, point in (("O", src), ("D", dst)):
        length = float(artifact.edge_len[point.edge])
        share = float(cost[point.edge]) / length
        tail = length - point.s_m
        oracle.add_edge(label, int(artifact.edge_u[point.edge]), weight=share * point.s_m)
        oracle.add_edge(label, int(artifact.edge_v[point.edge]), weight=share * tail)
    if src.edge == dst.edge:
        share = float(cost[src.edge]) / float(artifact.edge_len[src.edge])
        oracle.add_edge("O", "D", weight=share * abs(dst.s_m - src.s_m))
    return float(nx.dijkstra_path_length(oracle, "O", "D"))


def test_astar_points_matches_virtual_dijkstra_oracle() -> None:
    """Random pins on a seeded grid: same cost as the explicit oracle."""
    rng = np.random.default_rng(7)
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
    cost = artifact.edge_len.astype(np.float64) * (1.0 + rng.uniform(0.0, 3.0, len(edges)))

    queries = rng.uniform(-5.0, 45.0, size=(30, 4))
    for from_x, from_y, to_x, to_y in queries:
        src = graph.snap_point(float(from_x), float(from_y))
        dst = graph.snap_point(float(to_x), float(to_y))
        spans = graph.astar_points(src, dst, cost)
        assert spans is not None
        expected = _virtual_oracle(artifact, cost, src, dst)
        assert _spans_cost(artifact, spans, cost) == pytest.approx(expected)


def test_astar_points_same_edge_direct_wins() -> None:
    """Two pins on one street: walk it, do not detour through a junction."""
    nodes: list[Node] = [(0.0, 0.0), (100.0, 0.0)]
    artifact = _artifact(nodes, [(0, 1, None)], [[0] * 3])
    graph = RouteGraph.build(artifact)
    spans = graph.astar_points(
        _edge_point(artifact, 0, 20.0),
        _edge_point(artifact, 0, 75.0),
        artifact.edge_len.astype(np.float64),
    )
    assert spans is not None
    assert spans == [EdgeSpan(edge=0, s_from=20.0, s_to=75.0)]
    zeros = np.zeros(1, dtype=np.float32)
    leg = graph.assemble_spans(spans, zeros, zeros)
    assert leg.length_m == pytest.approx(55.0)
    assert leg.xs.tolist() == [20.0, 75.0]


def test_astar_points_same_edge_detour_via_parallel_wins() -> None:
    """The stretch between the pins is sunny and a shaded arc parallels it:
    leaving the edge and coming back beats walking straight through."""
    nodes: list[Node] = [(0.0, 0.0), (100.0, 0.0)]
    apex = (50.0, float(np.sqrt(60.0**2 - 50.0**2)))  # each half exactly 60 m
    edges: list[Edge] = [(0, 1, None), (0, 1, [(0.0, 0.0), apex, (100.0, 0.0)])]
    artifact = _artifact(nodes, edges, [[255] * 3, [0] * 3])
    graph = RouteGraph.build(artifact)
    fractions = graph.fractions_at(datetime.fromisoformat("2026-07-01T11:00"))
    cost = artifact.edge_len.astype(np.float64) * (1.0 + 3.0 * fractions)
    src, dst = _edge_point(artifact, 0, 5.0), _edge_point(artifact, 0, 95.0)

    spans = graph.astar_points(src, dst, cost)
    assert spans is not None
    assert [span.edge for span in spans] == [0, 1, 0]  # out, around, back in
    assert _spans_cost(artifact, spans, cost) == pytest.approx(160.0)  # vs 360 straight
    leg = graph.assemble_spans(spans, fractions, np.zeros_like(fractions))
    assert leg.length_m == pytest.approx(130.0)  # 5 + 120 + 5
    assert leg.sun_length_m == pytest.approx(10.0)  # only the two stubs

    # Without the sun penalty the direct walk wins.
    direct = graph.astar_points(src, dst, artifact.edge_len.astype(np.float64))
    assert direct == [EdgeSpan(edge=0, s_from=5.0, s_to=95.0)]


def test_astar_points_between_parallel_edges() -> None:
    """Origin on the straight, destination on the arc: two partial spans."""
    nodes: list[Node] = [(0.0, 0.0), (100.0, 0.0)]
    apex = (50.0, float(np.sqrt(60.0**2 - 50.0**2)))
    edges: list[Edge] = [(0, 1, None), (0, 1, [(0.0, 0.0), apex, (100.0, 0.0)])]
    artifact = _artifact(nodes, edges, [[0] * 3] * 2)
    graph = RouteGraph.build(artifact)
    spans = graph.astar_points(
        _edge_point(artifact, 0, 50.0),
        _edge_point(artifact, 1, 60.0),
        artifact.edge_len.astype(np.float64),
    )
    assert spans is not None
    assert [span.edge for span in spans] == [0, 1]
    assert sum(abs(span.s_to - span.s_from) for span in spans) == pytest.approx(110.0)


def test_astar_points_node_coincident_matches_astar() -> None:
    """Pins exactly on junctions reproduce the node-to-node search."""
    nodes: list[Node] = [(0.0, 0.0), (30.0, 0.0), (30.0, 40.0)]
    edges: list[Edge] = [(0, 1, None), (1, 2, None)]
    artifact = _artifact(nodes, edges, [[0] * 3] * 2)
    graph = RouteGraph.build(artifact)
    cost = artifact.edge_len.astype(np.float64)
    path = graph.astar(0, 2, cost)
    spans = graph.astar_points(_edge_point(artifact, 0, 0.0), _edge_point(artifact, 1, 40.0), cost)
    assert path is not None and spans is not None
    assert _spans_cost(artifact, spans, cost) == pytest.approx(_path_cost(graph, path, cost))
    assert [span.edge for span in spans] == [0, 1]


def test_astar_points_identical_point_returns_empty() -> None:
    artifact = _artifact([(0.0, 0.0), (10.0, 0.0)], [(0, 1, None)], [[0] * 3])
    graph = RouteGraph.build(artifact)
    point = _edge_point(artifact, 0, 4.0)
    assert graph.astar_points(point, point, artifact.edge_len.astype(np.float64)) == []


def test_point_leg_is_zero_length() -> None:
    artifact = _artifact([(3.0, 4.0), (10.0, 4.0)], [(0, 1, None)], [[0] * 3])
    graph = RouteGraph.build(artifact)
    leg = graph.point_leg(_edge_point(artifact, 0, 0.0))
    assert leg.length_m == 0.0
    assert leg.sun_fraction == 0.0
    assert leg.xs.tolist() == [3.0, 3.0]
    assert leg.segments == ()  # nothing was walked, nothing to decompose


def test_astar_points_unreachable_returns_none() -> None:
    nodes: list[Node] = [(0.0, 0.0), (10.0, 0.0), (100.0, 0.0), (110.0, 0.0)]
    edges: list[Edge] = [(0, 1, None), (2, 3, None)]
    artifact = _artifact(nodes, edges, [[0] * 3] * 2)
    graph = RouteGraph.build(artifact)
    spans = graph.astar_points(
        _edge_point(artifact, 0, 5.0),
        _edge_point(artifact, 1, 5.0),
        artifact.edge_len.astype(np.float64),
    )
    assert spans is None


def test_astar_points_rejects_costs_below_lengths() -> None:
    artifact = _artifact([(0.0, 0.0), (10.0, 0.0)], [(0, 1, None)], [[0] * 3])
    graph = RouteGraph.build(artifact)
    with pytest.raises(ValueError, match="heuristic"):
        graph.astar_points(
            _edge_point(artifact, 0, 1.0), _edge_point(artifact, 0, 9.0), np.array([5.0])
        )


def test_assemble_spans_cuts_partial_geometry() -> None:
    """A span clips the polyline at exact arc positions, either direction."""
    nodes: list[Node] = [(0.0, 0.0), (10.0, 10.0)]
    edges: list[Edge] = [(0, 1, [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0)])]
    artifact = _artifact(nodes, edges, [[255] * 3])
    graph = RouteGraph.build(artifact)
    fractions = np.array([0.5], dtype=np.float32)

    leg = graph.assemble_spans(
        [EdgeSpan(edge=0, s_from=5.0, s_to=15.0)], fractions, np.zeros_like(fractions)
    )
    assert leg.xs.tolist() == [5.0, 10.0, 10.0]
    assert leg.ys.tolist() == [0.0, 0.0, 5.0]
    assert leg.length_m == pytest.approx(10.0)
    assert leg.sun_length_m == pytest.approx(5.0)

    backward = graph.assemble_spans(
        [EdgeSpan(edge=0, s_from=15.0, s_to=5.0)], fractions, np.zeros_like(fractions)
    )
    assert backward.xs.tolist() == [10.0, 10.0, 5.0]
    assert backward.ys.tolist() == [5.0, 0.0, 0.0]


# --- vegetation weighting ------------------------------------------------------


def test_astar_beta_prefers_the_tree_shaded_parallel() -> None:
    """Two shaded parallels, neither in the sun: beta breaks the tie toward
    the one under canopy, and only once it outweighs the extra length.

    Straight 100 m under building shade vs 120 m arc under trees. With
    cost = len * (1 + beta * non_veg_shade): straight = 100 * (1 + beta),
    arc = 120. The flip sits at beta = 0.2.
    """
    nodes: list[Node] = [(0.0, 0.0), (100.0, 0.0)]
    apex = (50.0, float(np.sqrt(60.0**2 - 50.0**2)))
    edges: list[Edge] = [(0, 1, None), (0, 1, [(0.0, 0.0), apex, (100.0, 0.0)])]
    # Neither edge sees the sun; only the arc is under canopy.
    artifact = _artifact(nodes, edges, [[0] * 3, [0] * 3], veg=[[0] * 3, [255] * 3])
    graph = RouteGraph.build(artifact)
    when = datetime.fromisoformat("2026-07-01T11:00")
    sun = graph.fractions_at(when)
    veg = graph.veg_fractions_at(when)
    lengths = artifact.edge_len.astype(np.float64)
    other_shade = np.clip(1.0 - sun - veg, 0.0, 1.0)

    for beta, expected_edge in [(0.0, 0), (0.1, 0), (0.5, 1)]:
        path = graph.astar(0, 1, lengths * (1.0 + beta * other_shade))
        assert path is not None
        assert int(graph.adj_edge[path[0]]) == expected_edge, f"beta {beta}"


def test_veg_fractions_interpolate_between_columns() -> None:
    """The canopy matrix resolves exactly like the sun one."""
    artifact = _artifact(
        [(0.0, 0.0), (10.0, 0.0)], [(0, 1, None)], [[0, 0, 0]], veg=[[0, 255, 255]]
    )
    graph = RouteGraph.build(artifact)
    assert graph.veg_fractions_at(datetime.fromisoformat("2026-05-10T10:30"))[0] == pytest.approx(
        0.5, abs=1e-3
    )
    assert graph.veg_fractions_at(datetime.fromisoformat("2026-05-10T10:00"))[0] == 0.0
    assert graph.veg_fractions_at(datetime.fromisoformat("2026-05-10T06:00"))[0] == 0.0


def test_assemble_spans_accounts_vegetal_length() -> None:
    """A leg reports sun, canopy, and (implicitly) built shade separately."""
    nodes: list[Node] = [(0.0, 0.0), (100.0, 0.0)]
    artifact = _artifact(nodes, [(0, 1, None)], [[0] * 3])
    graph = RouteGraph.build(artifact)
    leg = graph.assemble_spans(
        [EdgeSpan(edge=0, s_from=0.0, s_to=50.0)],
        np.array([0.2], dtype=np.float32),
        np.array([0.5], dtype=np.float32),
    )
    assert leg.length_m == pytest.approx(50.0)
    assert leg.sun_length_m == pytest.approx(10.0)
    assert leg.veg_shade_length_m == pytest.approx(25.0)
    # The remainder is shade cast by buildings or terrain.
    assert leg.length_m - leg.sun_length_m - leg.veg_shade_length_m == pytest.approx(15.0)


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
