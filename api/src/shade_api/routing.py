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
"""

import heapq
from bisect import bisect_right
from dataclasses import dataclass
from datetime import date, datetime

import numpy as np
import numpy.typing as npt

from shade_core.routegraph import GraphRung, RouteGraphArtifact


@dataclass(frozen=True)
class RouteLeg:
    """One assembled route: concatenated polyline plus its sun accounting."""

    xs: npt.NDArray[np.float64]
    ys: npt.NDArray[np.float64]
    length_m: float
    sun_length_m: float

    @property
    def sun_fraction(self) -> float:
        return self.sun_length_m / self.length_m if self.length_m > 0 else 0.0


@dataclass(frozen=True)
class RouteGraph:
    """A city's routable graph: the artifact plus its directed CSR adjacency."""

    artifact: RouteGraphArtifact
    indptr: npt.NDArray[np.int64]
    adj_node: npt.NDArray[np.int32]
    adj_edge: npt.NDArray[np.int32]
    adj_forward: npt.NDArray[np.bool_]

    @classmethod
    def build(cls, artifact: RouteGraphArtifact) -> RouteGraph:
        """Directed CSR from the undirected edges: each emits u->v and v->u."""
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
        return cls(
            artifact=artifact,
            indptr=indptr,
            adj_node=tails[order].astype(np.int32),
            adj_edge=edges[order],
            adj_forward=forward[order],
        )

    def nearest_node(self, x: float, y: float) -> tuple[int, float]:
        """(node index, distance in meters) of the closest graph node.

        Brute force over all nodes: at ~13k nodes per city one vectorized
        argmin costs microseconds; a spatial index would only add a
        dependency. Trap acknowledged: nearest in a straight line may sit
        across a river or a wall -- the route from it is still correct,
        just possibly starting on the far side.
        """
        d2 = (self.artifact.node_x - x) ** 2 + (self.artifact.node_y - y) ** 2
        index = int(np.argmin(d2))
        return index, float(np.sqrt(d2[index]))

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
        """Per-edge sun fraction (0..1) at a local instant.

        Date resolves to a rung through ``covers``; the time interpolates
        linearly between the two neighboring hourly columns (nearest would
        step the whole ranking every half hour), clamping outside the
        rung's daylight range -- the sun is low there and the caller cuts
        the true night case via ``SunPosition.is_up`` anyway.
        """
        rung = self._rung_for(when.date())
        minutes = when.hour * 60.0 + when.minute + when.second / 60.0
        times = [int(column.time[:2]) * 60.0 + int(column.time[3:5]) for column in rung.columns]
        fractions = self.artifact.fractions
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

    def astar(self, src: int, dst: int, edge_cost: npt.NDArray[np.float64]) -> list[int] | None:
        """Cheapest path src -> dst as adjacency indices; None if unreachable.

        ``edge_cost`` is per *undirected* edge (walking costs the same both
        ways). Predecessors store the adjacency index, not the node, so a
        cheaper parallel edge between the same two nodes survives path
        reconstruction. See the module docstring for why the euclidean
        heuristic is admissible and why costs below lengths are rejected.
        """
        lengths = self.artifact.edge_len.astype(np.float64)
        if np.any(edge_cost < lengths - 1e-6):
            raise ValueError("edge costs below edge lengths would break the A* heuristic")
        if src == dst:
            return []
        node_x, node_y = self.artifact.node_x, self.artifact.node_y
        h = np.hypot(node_x - node_x[dst], node_y - node_y[dst])
        n_nodes = len(node_x)
        dist = np.full(n_nodes, np.inf)
        dist[src] = 0.0
        pred_adj = np.full(n_nodes, -1, dtype=np.int64)
        pred_node = np.full(n_nodes, -1, dtype=np.int64)
        settled = np.zeros(n_nodes, dtype=np.bool_)
        frontier: list[tuple[float, int]] = [(float(h[src]), src)]
        while frontier:
            _, node = heapq.heappop(frontier)
            if node == dst:
                break
            if settled[node]:
                continue
            settled[node] = True
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
        if not np.isfinite(dist[dst]):
            return None
        path: list[int] = []
        node = dst
        while node != src:
            path.append(int(pred_adj[node]))
            node = int(pred_node[node])
        path.reverse()
        return path

    def assemble(self, adj_path: list[int], fractions: npt.NDArray[np.float32]) -> RouteLeg:
        """Concatenate a path's edge polylines into one origin -> destination leg.

        Each hop's geometry is reversed when ridden against its stored
        direction; shared joint vertices are dropped. ``sun_length_m``
        weights each edge's length by its sun fraction at the queried
        instant -- the same accounting the router optimized.
        """
        artifact = self.artifact
        xs_parts: list[npt.NDArray[np.float64]] = []
        ys_parts: list[npt.NDArray[np.float64]] = []
        length = 0.0
        sun_length = 0.0
        for position, k in enumerate(adj_path):
            edge = int(self.adj_edge[k])
            start, stop = artifact.geom_offsets[edge], artifact.geom_offsets[edge + 1]
            xs = artifact.geom_x[start:stop]
            ys = artifact.geom_y[start:stop]
            if not self.adj_forward[k]:
                xs, ys = xs[::-1], ys[::-1]
            if position > 0:  # the joint vertex is the previous hop's last one
                xs, ys = xs[1:], ys[1:]
            xs_parts.append(xs)
            ys_parts.append(ys)
            edge_length = float(artifact.edge_len[edge])
            length += edge_length
            sun_length += edge_length * float(fractions[edge])
        return RouteLeg(
            xs=np.concatenate(xs_parts),
            ys=np.concatenate(ys_parts),
            length_m=length,
            sun_length_m=sun_length,
        )

    def trivial_leg(self, node: int) -> RouteLeg:
        """Origin and destination snapped to the same node: a zero-length leg."""
        x = self.artifact.node_x[node]
        y = self.artifact.node_y[node]
        return RouteLeg(xs=np.array([x, x]), ys=np.array([y, y]), length_m=0.0, sun_length_m=0.0)
