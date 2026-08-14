"""In-process shaded routing: CSR adjacency plus A* over sun fractions.

The route engine is plain numpy over the frozen graph artifact -- no
networkx, no database. The undirected artifact edges become a directed
**CSR adjacency** at load time (see :meth:`RouteGraph.build`): for node
``n``, ``adj_node[indptr[n]:indptr[n + 1]]`` lists its neighbors, and the
parallel ``adj_edge``/``adj_forward`` arrays say which artifact edge each
hop rides and in which direction (both directions share one edge id, so
fractions and geometry are stored once).

**A*** (see docs/learning/a-star.md) is Dijkstra with a compass: it always
expands the node minimizing ``g(n) + h(n)`` -- cost walked so far plus a
*heuristic* estimate of the cost still ahead. The route is optimal exactly
when ``h`` never overestimates (an *admissible* heuristic). Ours is the
straight-line euclidean distance to the destination, in projected meters
(degrees do not measure length), and it stays admissible under the solar
weight for every ``alpha >= 0``::

    cost(edge) = length * (1 + alpha * sun_fraction) >= length >= euclidean

It is also *consistent* (the triangle inequality carries over), so a node
popped from the queue is settled for good and the search can stop the
moment the destination pops. :meth:`RouteGraph.astar` refuses edge costs
below edge lengths: whoever normalizes the weight formula someday must
scale the heuristic too, or A* silently stops being optimal.

**Virtual endpoints.** People drop pins mid-street, not on junctions, so
:meth:`RouteGraph.snap_point` projects them onto the closest *edge*
(see docs/learning/point-segment-projection.md) and
:meth:`RouteGraph.astar_points` routes between those interior points. That
is the classic super-source construction: the search is seeded with both
ends of the origin edge, each charged the partial cost of the stretch
back to the pin, and a virtual destination node -- ``_TARGET`` in the
queue, heuristic 0 -- collects both ends of the destination edge plus, when
the two pins share an edge, the direct walk between them. Partial costs are
exact because our weight has constant cost per meter within an edge (the
sun fraction is stored per edge, not per meter), and consistency survives
because a partial cost ``c * s / L`` is never below the arc ``s`` it
covers, hence never below the euclidean estimate it replaces.
"""

import heapq
import math
from bisect import bisect_right
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime

import numpy as np
import numpy.typing as npt

from shade_core.routegraph import GraphRung, RouteGraphArtifact

_TARGET = -1
"""Pseudo-node standing for the virtual destination inside the queue.

It sorts before every real node index, so on an f-value tie the goal pops
first -- which is what we want, the search is over.
"""

_SAME_POINT_M = 1e-6
"""Below this the two pins snapped to the same spot: no route to compute."""

_MIN_SPAN_M = 1e-9
"""Partial spans shorter than this are dropped (the pin sat on a node)."""


@dataclass(frozen=True)
class RouteSegment:
    """One walked edge stretch, carrying that edge's own shade mix.

    The edge is the router's decision unit -- the artifact stores one sun
    and one canopy fraction per edge per ladder instant -- so this is the
    finest honest slice of a route (docs/learning/edge-granularity.md). A
    partial span inherits its edge's fractions pro rata, not scaled. The
    polyline keeps both joint vertices, so consecutive segments touch and
    dropping each one's first point rebuilds the leg exactly.
    """

    xs: npt.NDArray[np.float64]
    ys: npt.NDArray[np.float64]
    length_m: float
    sun_fraction: float
    veg_shade_fraction: float


@dataclass(frozen=True)
class RouteLeg:
    """One assembled route: concatenated polyline plus its sun accounting.

    ``sun_fraction`` here is length-weighted over the whole leg, while a
    :class:`RouteSegment`'s is its edge's own constant.
    """

    xs: npt.NDArray[np.float64]
    ys: npt.NDArray[np.float64]
    length_m: float
    sun_length_m: float
    veg_shade_length_m: float
    segments: tuple[RouteSegment, ...] = ()

    @property
    def sun_fraction(self) -> float:
        return self.sun_length_m / self.length_m if self.length_m > 0 else 0.0


@dataclass(frozen=True)
class EdgePoint:
    """A point *on* the graph: which edge, and how far along it.

    ``s_m`` is arc length in meters from the edge's ``edge_u`` end, always
    within ``[0, edge_len]``. ``x``/``y`` are the projected coordinates of
    the point itself (not of the query), and ``distance_m`` is how far the
    caller's point sat from the network.
    """

    edge: int
    s_m: float
    x: float
    y: float
    distance_m: float


@dataclass(frozen=True)
class EdgeSpan:
    """A walked interval of one edge, in arc length from the ``edge_u`` end.

    A full forward traversal is ``(e, 0, L)`` and a full backward one is
    ``(e, L, 0)``; route endpoints produce partial spans. The direction is
    the sign of ``s_to - s_from`` and ``abs(s_to - s_from)`` is the walked
    length, so spans carry everything assembly needs.
    """

    edge: int
    s_from: float
    s_to: float


@dataclass(frozen=True)
class RouteGraph:
    """A city's routable graph: the artifact plus its directed CSR adjacency."""

    artifact: RouteGraphArtifact
    indptr: npt.NDArray[np.int64]
    adj_node: npt.NDArray[np.int32]
    adj_edge: npt.NDArray[np.int32]
    adj_forward: npt.NDArray[np.bool_]
    seg_x: npt.NDArray[np.float64]
    seg_y: npt.NDArray[np.float64]
    seg_dx: npt.NDArray[np.float64]
    seg_dy: npt.NDArray[np.float64]
    seg_len: npt.NDArray[np.float64]
    seg_arc0: npt.NDArray[np.float64]
    seg_edge: npt.NDArray[np.int32]

    @classmethod
    def build(cls, artifact: RouteGraphArtifact) -> RouteGraph:
        """Directed CSR from the undirected edges, plus a flat segment table.

        Two derived structures, both built once at load:

        - the adjacency: each undirected edge emits u->v and v->u sharing
          one edge id, so fractions and geometry are stored once;
        - the segment table: every polyline segment of every edge flattened
          into parallel arrays, which is what lets :meth:`snap_point`
          project a pin onto the network with a single vectorized pass.
          ``seg_arc0`` is the arc length from the start of *its own* edge to
          the segment's first vertex, so a hit at parameter ``t`` maps back
          to a position along the edge without any per-edge bookkeeping.
        """
        n_nodes = len(artifact.node_x)
        n_edges = len(artifact.edge_len)
        heads = np.concatenate([artifact.edge_u, artifact.edge_v])
        tails = np.concatenate([artifact.edge_v, artifact.edge_u])
        edges = np.concatenate([np.arange(n_edges), np.arange(n_edges)]).astype(np.int32)
        forward = np.concatenate(
            [np.ones(n_edges, dtype=np.bool_), np.zeros(n_edges, dtype=np.bool_)]
        )
        order = np.argsort(heads, kind="stable")
        indptr = np.zeros(n_nodes + 1, dtype=np.int64)
        np.cumsum(np.bincount(heads, minlength=n_nodes), out=indptr[1:])

        # Ragged flattening: diff() over the concatenated vertices produces
        # one bogus segment per edge boundary (last vertex of edge e to the
        # first of e + 1); the mask drops exactly those.
        offsets = artifact.geom_offsets
        counts = np.diff(offsets)
        dx = np.diff(artifact.geom_x)
        dy = np.diff(artifact.geom_y)
        keep = np.ones(len(dx), dtype=np.bool_)
        keep[offsets[1:-1] - 1] = False
        seg_dx, seg_dy = dx[keep], dy[keep]
        seg_len = np.hypot(seg_dx, seg_dy)
        seg_edge = np.repeat(np.arange(n_edges, dtype=np.int32), counts - 1)
        # Arc from the start of the owning edge = global running arc minus
        # the running arc at that edge's first segment.
        arc = np.concatenate([[0.0], np.cumsum(seg_len)])[:-1]
        first = offsets[:-1] - np.arange(n_edges)
        return cls(
            artifact=artifact,
            indptr=indptr,
            adj_node=tails[order].astype(np.int32),
            adj_edge=edges[order],
            adj_forward=forward[order],
            seg_x=artifact.geom_x[:-1][keep],
            seg_y=artifact.geom_y[:-1][keep],
            seg_dx=seg_dx,
            seg_dy=seg_dy,
            seg_len=seg_len,
            seg_arc0=arc - np.repeat(arc[first], counts - 1),
            seg_edge=seg_edge,
        )

    def snap_point(self, x: float, y: float) -> EdgePoint:
        """Project a point onto the closest edge of the network.

        Per segment the projection is the clamped scalar parameter
        ``t = clip(dot(p - a, d) / |d|^2, 0, 1)`` (see
        docs/learning/point-segment-projection.md); the winner is the
        smallest squared distance, compared squared to skip ~200k square
        roots. Brute force over every segment: one vectorized pass is a
        couple of milliseconds on Cordoba and a spatial index would only
        add a dependency. Trap acknowledged: closest in a straight line may
        sit across a river or a wall -- the route from it is still correct,
        just possibly starting on the far bank.
        """
        length2 = self.seg_dx**2 + self.seg_dy**2
        # Zero-length segments (repeated OSM vertices) collapse to t = 0.
        divisor = np.where(length2 > 0.0, length2, 1.0)
        along = (x - self.seg_x) * self.seg_dx + (y - self.seg_y) * self.seg_dy
        t = np.clip(along / divisor, 0.0, 1.0)
        px = self.seg_x + t * self.seg_dx
        py = self.seg_y + t * self.seg_dy
        d2 = (px - x) ** 2 + (py - y) ** 2
        index = int(np.argmin(d2))
        edge = int(self.seg_edge[index])
        # Clamp: the float64 segment sums drift from the stored float32
        # edge length by ~1e-7 m, and callers rely on s_m <= edge_len.
        arc = self.seg_arc0[index] + t[index] * self.seg_len[index]
        return EdgePoint(
            edge=edge,
            s_m=float(np.clip(arc, 0.0, self.artifact.edge_len[edge])),
            x=float(px[index]),
            y=float(py[index]),
            distance_m=float(np.sqrt(d2[index])),
        )

    def _ladder_year(self) -> int:
        return int(self.artifact.meta.ladder[0].date[:4])

    def _rung_for(self, day: date) -> GraphRung:
        """The declination rung covering a calendar date (any year).

        The ladder's ``covers`` ranges live in the year it was built for;
        any other year maps by month/day (Feb 29 borrows Feb 28: one day of
        declination is ~0.4 deg, far below the rung step).
        """
        try:
            mapped = day.replace(year=self._ladder_year())
        except ValueError:  # Feb 29 into a non-leap ladder year
            mapped = day.replace(year=self._ladder_year(), day=28)
        iso = mapped.isoformat()
        for rung in self.artifact.meta.ladder:
            if any(start <= iso <= end for start, end in rung.covers):
                return rung
        raise ValueError(f"no ladder rung covers {iso}")

    def fractions_at(self, when: datetime) -> npt.NDArray[np.float32]:
        """Per-edge sun fraction (0..1) at a local instant."""
        return self._interpolate_columns(self.artifact.fractions, when)

    def veg_fractions_at(self, when: datetime) -> npt.NDArray[np.float32]:
        """Per-edge fraction under tree canopy (0..1) at a local instant.

        Same resolution as :meth:`fractions_at` over the other stored
        matrix. Sun and canopy are disjoint, so what is left,
        ``1 - sun - vegetation``, is shade cast by buildings or terrain.
        """
        return self._interpolate_columns(self.artifact.veg_fractions, when)

    def _interpolate_columns(
        self, matrix: npt.NDArray[np.uint8], when: datetime
    ) -> npt.NDArray[np.float32]:
        """Resolve one stored fraction matrix to a local instant.

        Date resolves to a rung through ``covers``; the time interpolates
        linearly between the two neighboring hourly columns (nearest would
        step the whole ranking every half hour), clamping outside the
        rung's daylight range -- the sun is low there and the caller cuts
        the true night case via ``SunPosition.is_up`` anyway.
        """
        rung = self._rung_for(when.date())
        minutes = when.hour * 60.0 + when.minute + when.second / 60.0
        times = [int(column.time[:2]) * 60.0 + int(column.time[3:5]) for column in rung.columns]
        fractions = matrix
        scale = np.float32(1.0 / 255.0)
        if minutes <= times[0]:
            return fractions[:, rung.columns[0].col].astype(np.float32) * scale
        if minutes >= times[-1]:
            return fractions[:, rung.columns[-1].col].astype(np.float32) * scale
        upper = bisect_right(times, minutes)
        lower = upper - 1
        weight = np.float32((minutes - times[lower]) / (times[upper] - times[lower]))
        low = fractions[:, rung.columns[lower].col].astype(np.float32) * scale
        high = fractions[:, rung.columns[upper].col].astype(np.float32) * scale
        return (np.float32(1.0) - weight) * low + weight * high

    def _astar_virtual(
        self,
        seeds: Sequence[tuple[int, float]],
        targets: Mapping[int, float],
        target_x: float,
        target_y: float,
        edge_cost: npt.NDArray[np.float64],
        direct_cost: float = math.inf,
    ) -> tuple[int, npt.NDArray[np.int64], npt.NDArray[np.int64]] | None:
        """A* from a set of seeded nodes to a virtual destination.

        ``seeds`` are ``(node, cost already spent to reach it)`` and
        ``targets`` are ``(node, cost from it to the destination)``:
        together they express the super-source/super-sink construction the
        module docstring describes. ``direct_cost`` is the price of getting
        there without touching any node at all (both pins on one edge).

        Returns ``(entry node, pred_adj, pred_node)`` -- ``_TARGET`` as the
        entry node meaning the direct walk won -- or None if unreachable.
        Predecessors store the adjacency index, not the node, so a cheaper
        parallel edge between the same two nodes survives reconstruction;
        seeds keep ``pred_adj == -1`` as the stop sentinel.
        """
        lengths = self.artifact.edge_len.astype(np.float64)
        if np.any(edge_cost < lengths - 1e-6):
            raise ValueError("edge costs below edge lengths would break the A* heuristic")
        node_x, node_y = self.artifact.node_x, self.artifact.node_y
        h = np.hypot(node_x - target_x, node_y - target_y)
        n_nodes = len(node_x)
        dist = np.full(n_nodes, np.inf)
        pred_adj = np.full(n_nodes, -1, dtype=np.int64)
        pred_node = np.full(n_nodes, -1, dtype=np.int64)
        settled = np.zeros(n_nodes, dtype=np.bool_)
        frontier: list[tuple[float, int]] = []
        for node, spent in seeds:
            if spent < float(dist[node]):
                dist[node] = spent
                heapq.heappush(frontier, (spent + float(h[node]), node))
        best = direct_cost
        entry = _TARGET
        if math.isfinite(direct_cost):
            heapq.heappush(frontier, (direct_cost, _TARGET))
        while frontier:
            _, node = heapq.heappop(frontier)
            if node == _TARGET:
                # h(destination) = 0, so the first pop carries the cheapest
                # arrival: every entry still queued has f >= this one.
                return entry, pred_adj, pred_node
            if settled[node]:
                continue
            settled[node] = True
            tail = targets.get(node)
            if tail is not None:
                arrival = float(dist[node]) + tail
                if arrival < best:
                    best = arrival
                    entry = node
                    heapq.heappush(frontier, (arrival, _TARGET))
            for k in range(int(self.indptr[node]), int(self.indptr[node + 1])):
                neighbor = int(self.adj_node[k])
                if settled[neighbor]:
                    continue
                candidate = float(dist[node]) + float(edge_cost[self.adj_edge[k]])
                if candidate < float(dist[neighbor]):
                    dist[neighbor] = candidate
                    pred_adj[neighbor] = k
                    pred_node[neighbor] = node
                    heapq.heappush(frontier, (candidate + float(h[neighbor]), neighbor))
        return None

    def _hops_from(
        self,
        node: int,
        pred_adj: npt.NDArray[np.int64],
        pred_node: npt.NDArray[np.int64],
    ) -> tuple[list[int], int]:
        """Walk predecessors back to a seed: (adjacency indices, seed node)."""
        hops: list[int] = []
        while pred_adj[node] != -1:
            hops.append(int(pred_adj[node]))
            node = int(pred_node[node])
        hops.reverse()
        return hops, node

    def astar(self, src: int, dst: int, edge_cost: npt.NDArray[np.float64]) -> list[int] | None:
        """Cheapest node-to-node path as adjacency indices; None if unreachable.

        ``edge_cost`` is per *undirected* edge (walking costs the same both
        ways). Node endpoints are the degenerate case of
        :meth:`astar_points`: one seed and one target, both free.
        """
        result = self._astar_virtual(
            [(src, 0.0)],
            {dst: 0.0},
            float(self.artifact.node_x[dst]),
            float(self.artifact.node_y[dst]),
            edge_cost,
        )
        if result is None:
            return None
        entry, pred_adj, pred_node = result
        hops, _ = self._hops_from(entry, pred_adj, pred_node)
        return hops

    def astar_points(
        self,
        src: EdgePoint,
        dst: EdgePoint,
        edge_cost: npt.NDArray[np.float64],
    ) -> list[EdgeSpan] | None:
        """Cheapest walk between two points *on* edges, as spans.

        Both pins usually sit mid-edge, so the route starts and ends with a
        partial span. The stretch from a pin to an edge end costs its share
        of the edge, ``cost * s / length``, which is exact under our weight
        (uniform cost per meter within an edge). When both pins share an
        edge, the walk straight along it competes in the queue with leaving
        through one end and coming back through the other -- a real choice
        when the parallel arc is shaded and the direct stretch is not.

        Returns an empty list when both pins land on the same spot, and
        None when no walk connects them.
        """
        artifact = self.artifact
        src_len = float(artifact.edge_len[src.edge])
        dst_len = float(artifact.edge_len[dst.edge])
        src_cost = float(edge_cost[src.edge])
        dst_cost = float(edge_cost[dst.edge])
        direct = math.inf
        if src.edge == dst.edge:
            if abs(src.s_m - dst.s_m) <= _SAME_POINT_M:
                return []
            direct = src_cost * abs(dst.s_m - src.s_m) / src_len
        src_u, src_v = int(artifact.edge_u[src.edge]), int(artifact.edge_v[src.edge])
        dst_u, dst_v = int(artifact.edge_u[dst.edge]), int(artifact.edge_v[dst.edge])
        result = self._astar_virtual(
            [(src_u, src_cost * src.s_m / src_len), (src_v, src_cost * (1.0 - src.s_m / src_len))],
            {dst_u: dst_cost * dst.s_m / dst_len, dst_v: dst_cost * (1.0 - dst.s_m / dst_len)},
            dst.x,
            dst.y,
            edge_cost,
            direct,
        )
        if result is None:
            return None
        entry, pred_adj, pred_node = result
        if entry == _TARGET:  # never left the shared edge
            return [EdgeSpan(edge=src.edge, s_from=src.s_m, s_to=dst.s_m)]
        hops, exit_node = self._hops_from(entry, pred_adj, pred_node)
        spans: list[EdgeSpan] = []
        exit_s = 0.0 if exit_node == src_u else src_len
        if abs(src.s_m - exit_s) > _MIN_SPAN_M:
            spans.append(EdgeSpan(edge=src.edge, s_from=src.s_m, s_to=exit_s))
        for k in hops:
            edge = int(self.adj_edge[k])
            length = float(artifact.edge_len[edge])
            spans.append(
                EdgeSpan(edge=edge, s_from=0.0, s_to=length)
                if self.adj_forward[k]
                else EdgeSpan(edge=edge, s_from=length, s_to=0.0)
            )
        entry_s = 0.0 if entry == dst_u else dst_len
        if abs(entry_s - dst.s_m) > _MIN_SPAN_M:
            spans.append(EdgeSpan(edge=dst.edge, s_from=entry_s, s_to=dst.s_m))
        return spans

    def _span_polyline(self, span: EdgeSpan) -> tuple[npt.NDArray[np.float64], ...]:
        """The stored polyline clipped to a span, oriented as walked."""
        artifact = self.artifact
        start, stop = artifact.geom_offsets[span.edge], artifact.geom_offsets[span.edge + 1]
        xs = artifact.geom_x[start:stop]
        ys = artifact.geom_y[start:stop]
        low, high = min(span.s_from, span.s_to), max(span.s_from, span.s_to)
        arc = np.concatenate([[0.0], np.cumsum(np.hypot(np.diff(xs), np.diff(ys)))])
        # Spans measure in the stored (float32) edge length while this sum is
        # float64: rescale so both agree at the ends, or a whole traversal
        # would stop a hair short of its last vertex.
        arc *= float(artifact.edge_len[span.edge]) / arc[-1]
        # Cut ends then land exactly on stored vertices when the span is
        # whole, so a full traversal reproduces the polyline.
        inner = (arc > low) & (arc < high)
        cut_x = np.concatenate([[np.interp(low, arc, xs)], xs[inner], [np.interp(high, arc, xs)]])
        cut_y = np.concatenate([[np.interp(low, arc, ys)], ys[inner], [np.interp(high, arc, ys)]])
        if span.s_from > span.s_to:
            return cut_x[::-1], cut_y[::-1]
        return cut_x, cut_y

    def assemble_spans(
        self,
        spans: list[EdgeSpan],
        fractions: npt.NDArray[np.float32],
        veg_fractions: npt.NDArray[np.float32],
    ) -> RouteLeg:
        """Stitch walked spans into one origin -> destination leg.

        Each span contributes its clipped polyline, oriented as walked;
        shared joint vertices are dropped. ``sun_length_m`` and
        ``veg_shade_length_m`` weight the walked meters by the edge's sun
        and canopy fractions at the queried instant -- the same accounting
        the router optimized; the remainder is shade cast by buildings or
        terrain. Approximation worth naming: the artifact stores one
        fraction per *edge*, so a partial span is charged that fraction pro
        rata rather than resampling the stretch actually walked.

        ``segments`` keeps the very decomposition this accounting sums
        over, so a caller can colour the route by where its shade came from
        without redoing the walk.
        """
        xs_parts: list[npt.NDArray[np.float64]] = []
        ys_parts: list[npt.NDArray[np.float64]] = []
        segments: list[RouteSegment] = []
        length = 0.0
        sun_length = 0.0
        veg_length = 0.0
        for position, span in enumerate(spans):
            xs, ys = self._span_polyline(span)
            walked = abs(span.s_to - span.s_from)
            segment = RouteSegment(
                xs=xs,
                ys=ys,
                length_m=walked,
                sun_fraction=float(fractions[span.edge]),
                veg_shade_fraction=float(veg_fractions[span.edge]),
            )
            segments.append(segment)
            if position > 0:  # the joint vertex is the previous span's last one
                xs, ys = xs[1:], ys[1:]  # a view; the segment kept the whole array
            xs_parts.append(xs)
            ys_parts.append(ys)
            length += walked
            sun_length += walked * segment.sun_fraction
            veg_length += walked * segment.veg_shade_fraction
        return RouteLeg(
            xs=np.concatenate(xs_parts),
            ys=np.concatenate(ys_parts),
            length_m=length,
            sun_length_m=sun_length,
            veg_shade_length_m=veg_length,
            segments=tuple(segments),
        )

    def assemble(
        self,
        adj_path: list[int],
        fractions: npt.NDArray[np.float32],
        veg_fractions: npt.NDArray[np.float32] | None = None,
    ) -> RouteLeg:
        """Assemble a node-to-node path: every hop is a whole-edge span."""
        spans = []
        for k in adj_path:
            edge = int(self.adj_edge[k])
            length = float(self.artifact.edge_len[edge])
            spans.append(
                EdgeSpan(edge=edge, s_from=0.0, s_to=length)
                if self.adj_forward[k]
                else EdgeSpan(edge=edge, s_from=length, s_to=0.0)
            )
        if veg_fractions is None:
            veg_fractions = np.zeros(len(fractions), dtype=np.float32)
        return self.assemble_spans(spans, fractions, veg_fractions)

    def point_leg(self, point: EdgePoint) -> RouteLeg:
        """Origin and destination snapped to the same spot: a zero-length leg."""
        return RouteLeg(
            xs=np.array([point.x, point.x]),
            ys=np.array([point.y, point.y]),
            length_m=0.0,
            sun_length_m=0.0,
            veg_shade_length_m=0.0,
        )
