"""``shade-engine graph``: pedestrian graph with per-edge sun fractions.

OSM models streets as *ways* (ordered vertex lists with tags); a routable
graph needs *edges between intersections*. osmnx does that conversion for
``network_type="walk"`` (footway, pedestrian, path, steps, living_street,
residential...) and hands back a ``MultiDiGraph`` where nearly every
segment appears twice, once per direction, with mirrored geometry. Walking
has no one-way streets, so :func:`extract_graph_arrays` collapses those
reciprocal twins into undirected edges -- while *keeping* true parallel
edges between the same pair of nodes (a plaza diagonal in the sun and its
arcade in the shade are different candidates the router must see).

The expensive question "how sunny is this street at that time?" is
precomputed here, not answered per request: every edge is sampled every
``spacing_m`` meters (arc length, projected CRS -- degrees do not measure
length) and those points are indexed against the whole-city shade state
raster of each declination-ladder instant -- the same 83 instants the
tiles preset renders, so a route and the visible overlay always agree.
Per instant that is one vectorized :func:`compute_state_raster` call plus
numpy indexing; nothing touches the point engine.

Network access is confined to :class:`OsmnxWalkSource` behind the
:class:`GraphSource` protocol (the LidarSource/CnigSource pattern): tests
inject a synthetic graph and exercise everything below the download.
"""

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol
from zoneinfo import ZoneInfo

import networkx as nx
import numpy as np
import numpy.typing as npt
import rasterio.transform
from affine import Affine
from pyproj import Transformer

from shade_core.artifacts import load_coverage, load_metadata
from shade_core.config import Bbox, CityConfig
from shade_core.routegraph import (
    FRACTIONS_FILENAME,
    GRAPH_DIRNAME,
    GRAPH_FILENAME,
    GRAPH_META_FILENAME,
    OSM_ATTRIBUTION,
    ROUTE_GRAPH_SCHEMA_VERSION,
    GraphRung,
    RouteGraphMeta,
    RungColumn,
    load_route_graph,
)
from shade_core.solar import sun_position
from shade_pipeline.budget import estimate_tiles_worker_bytes, warn_if_serial_is_tight
from shade_pipeline.grid import grid_shape, transform_from_bbox
from shade_pipeline.progress import format_duration
from shade_pipeline.shade_raster import (
    STATE_OUTSIDE,
    STATE_SHADE_BOTH,
    STATE_SHADE_VEGETATION,
    STATE_SUN,
    compute_state_raster,
)
from shade_pipeline.tiles import (
    LADDER_PRESET_2026,
    bounds_wgs84,
    declination_ladder,
    season_preset_instants,
)

DEFAULT_SPACING_M = 5.0
DEFAULT_OSM_CACHE = Path("data/cache/osm")

# Below this an "edge" is a projection artifact, not a walkable segment.
_MIN_EDGE_LENGTH_M = 0.01


class GraphSource(Protocol):
    def fetch(self, bbox_wgs84: Bbox) -> nx.MultiDiGraph:
        """Walk network covering the WGS84 (west, south, east, north) bbox."""
        ...


@dataclass(frozen=True)
class OsmnxWalkSource:
    """Downloads the walk network from Overpass through osmnx, cached on disk.

    The single place in the codebase that talks to OSM; everything else
    consumes the returned graph, so tests swap this for a synthetic source.
    osmnx's default ``retain_all=False`` keeps only the largest connected
    component: unreachable islands would otherwise let the API snap a query
    to a node no route can leave.
    """

    cache_dir: Path = DEFAULT_OSM_CACHE

    def fetch(self, bbox_wgs84: Bbox) -> nx.MultiDiGraph:
        import osmnx as ox  # lazy: pulls geopandas; only this path pays the import

        ox.settings.cache_folder = str(self.cache_dir)
        west, south, east, north = bbox_wgs84
        graph: nx.MultiDiGraph = ox.graph_from_bbox((west, south, east, north), network_type="walk")
        return graph


@dataclass(frozen=True)
class GraphArrays:
    """Undirected edge arrays in the city CRS (the geometric half of the artifact)."""

    node_x: npt.NDArray[np.float64]
    node_y: npt.NDArray[np.float64]
    edge_u: npt.NDArray[np.int32]
    edge_v: npt.NDArray[np.int32]
    edge_len: npt.NDArray[np.float32]
    geom_x: npt.NDArray[np.float64]
    geom_y: npt.NDArray[np.float64]
    geom_offsets: npt.NDArray[np.int64]


def _edge_coords(
    graph: nx.MultiDiGraph, u: int, v: int, data: dict[str, object]
) -> npt.NDArray[np.float64]:
    """(k, 2) lon/lat vertices of one directed edge, oriented u -> v.

    osmnx only attaches a ``geometry`` when the way has intermediate
    vertices; a plain edge is the straight segment between its nodes.
    """
    geometry = data.get("geometry")
    if geometry is not None:
        return np.asarray(geometry.coords, dtype=np.float64)  # type: ignore[attr-defined]
    return np.array(
        [
            [graph.nodes[u]["x"], graph.nodes[u]["y"]],
            [graph.nodes[v]["x"], graph.nodes[v]["y"]],
        ],
        dtype=np.float64,
    )


def extract_graph_arrays(graph: nx.MultiDiGraph, crs: str) -> GraphArrays:
    """Collapse a WGS84 MultiDiGraph into undirected projected arrays.

    Dedup rule: orient every directed edge's polyline from its lower to its
    higher node id; two edges of the same node pair with (near-)identical
    oriented polylines are one walkable segment seen from both directions,
    so only the first survives. Distinct polylines between the same pair
    are true parallel edges and all survive. Self-loops are dropped (never
    part of a simple shortest path), and edge lengths are *recomputed* from
    the projected polyline so they equal, by construction, the arc the
    sun-fraction sampler walks.
    """
    to_projected = Transformer.from_crs("EPSG:4326", crs, always_xy=True)

    kept: dict[tuple[int, int], list[npt.NDArray[np.float64]]] = {}
    for u, v, data in graph.edges(data=True):
        if u == v:
            continue
        coords = _edge_coords(graph, u, v, data)
        if u > v:
            coords = coords[::-1]
        key = (min(u, v), max(u, v))
        bucket = kept.setdefault(key, [])
        if any(
            candidate.shape == coords.shape and np.allclose(candidate, coords, atol=1e-9)
            for candidate in bucket
        ):
            continue
        bucket.append(coords)

    edges: list[tuple[int, int, float, npt.NDArray[np.float64], npt.NDArray[np.float64]]] = []
    for u, v in sorted(kept):
        for coords in kept[(u, v)]:
            xs, ys = to_projected.transform(coords[:, 0], coords[:, 1])
            xs, ys = np.asarray(xs, dtype=np.float64), np.asarray(ys, dtype=np.float64)
            length = float(np.sum(np.hypot(np.diff(xs), np.diff(ys))))
            if length < _MIN_EDGE_LENGTH_M:
                continue
            edges.append((u, v, length, xs, ys))
    if not edges:
        raise ValueError("no walkable edges inside the city bbox")

    # Node set from the surviving edges only: an isolated node would be a
    # snap target no route can leave.
    node_ids = sorted({n for u, v, _, _, _ in edges for n in (u, v)})
    index_of = {node_id: index for index, node_id in enumerate(node_ids)}
    node_lon = np.array([graph.nodes[n]["x"] for n in node_ids], dtype=np.float64)
    node_lat = np.array([graph.nodes[n]["y"] for n in node_ids], dtype=np.float64)
    node_x, node_y = to_projected.transform(node_lon, node_lat)

    vertex_counts = [len(xs) for _, _, _, xs, _ in edges]
    offsets = np.zeros(len(edges) + 1, dtype=np.int64)
    np.cumsum(vertex_counts, out=offsets[1:])
    return GraphArrays(
        node_x=np.asarray(node_x, dtype=np.float64),
        node_y=np.asarray(node_y, dtype=np.float64),
        edge_u=np.array([index_of[u] for u, _, _, _, _ in edges], dtype=np.int32),
        edge_v=np.array([index_of[v] for _, v, _, _, _ in edges], dtype=np.int32),
        edge_len=np.array([length for _, _, length, _, _ in edges], dtype=np.float32),
        geom_x=np.concatenate([xs for _, _, _, xs, _ in edges]),
        geom_y=np.concatenate([ys for _, _, _, _, ys in edges]),
        geom_offsets=offsets,
    )


def sample_edges(
    arrays: GraphArrays, spacing_m: float
) -> tuple[npt.NDArray[np.float64], npt.NDArray[np.float64], npt.NDArray[np.int64]]:
    """(x, y, edge_id) of points every ``spacing_m`` along every edge.

    Same arc-length resampling as the parking endpoint's ``sample_polyline``:
    accumulate projected segment lengths and ``np.interp`` evenly spaced
    positions back to coordinates, endpoints always included. At 5 m over a
    1 m/px raster a single street tree's shadow still gets hit.
    """
    xs_out: list[npt.NDArray[np.float64]] = []
    ys_out: list[npt.NDArray[np.float64]] = []
    ids_out: list[npt.NDArray[np.int64]] = []
    offsets = arrays.geom_offsets
    for edge in range(len(arrays.edge_len)):
        xs = arrays.geom_x[offsets[edge] : offsets[edge + 1]]
        ys = arrays.geom_y[offsets[edge] : offsets[edge + 1]]
        cum = np.concatenate(([0.0], np.cumsum(np.hypot(np.diff(xs), np.diff(ys)))))
        total = float(cum[-1])
        positions = np.linspace(0.0, total, max(int(total // spacing_m) + 2, 2))
        xs_out.append(np.interp(positions, cum, xs))
        ys_out.append(np.interp(positions, cum, ys))
        ids_out.append(np.full(len(positions), edge, dtype=np.int64))
    return np.concatenate(xs_out), np.concatenate(ys_out), np.concatenate(ids_out)


def edges_fully_covered(
    sample_x: npt.NDArray[np.float64],
    sample_y: npt.NDArray[np.float64],
    sample_edge: npt.NDArray[np.int64],
    n_edges: int,
    coverage: npt.NDArray[np.bool_],
    transform: Affine,
) -> npt.NDArray[np.bool_]:
    """Which edges have every sample inside the computation area.

    All or nothing per edge, on purpose. Scoring a street on the half of it
    that happens to have data would publish a sun fraction that describes a
    different street, and the router would then compare it against fully
    sampled ones as if they meant the same thing.
    """
    rows, cols = rasterio.transform.rowcol(transform, sample_x, sample_y)
    rows, cols = np.asarray(rows), np.asarray(cols)
    height, width = coverage.shape
    inside = (rows >= 0) & (rows < height) & (cols >= 0) & (cols < width)
    covered = np.zeros(len(sample_x), dtype=bool)
    covered[inside] = coverage[rows[inside], cols[inside]]
    uncovered = np.bincount(sample_edge[~covered], minlength=n_edges)
    fully: npt.NDArray[np.bool_] = uncovered == 0
    return fully


def keep_edges(arrays: GraphArrays, keep: npt.NDArray[np.bool_]) -> GraphArrays:
    """``arrays`` with only the kept edges, their vertices, and reindexed nodes.

    Nodes left with no edge go too: a snap target no route can leave is worse
    than no snap target, because the router finds it and then fails.
    """
    offsets = arrays.geom_offsets
    kept = np.flatnonzero(keep)
    vertices = (
        np.concatenate([np.arange(offsets[edge], offsets[edge + 1]) for edge in kept])
        if len(kept)
        else np.empty(0, dtype=np.int64)
    )
    counts = (offsets[1:] - offsets[:-1])[keep]
    new_offsets = np.zeros(len(counts) + 1, dtype=np.int64)
    np.cumsum(counts, out=new_offsets[1:])

    edge_u, edge_v = arrays.edge_u[keep], arrays.edge_v[keep]
    used = np.unique(np.concatenate([edge_u, edge_v])) if len(kept) else np.empty(0, dtype=np.int32)
    remap = np.full(len(arrays.node_x), -1, dtype=np.int32)
    remap[used] = np.arange(len(used), dtype=np.int32)
    return GraphArrays(
        node_x=arrays.node_x[used],
        node_y=arrays.node_y[used],
        edge_u=remap[edge_u],
        edge_v=remap[edge_v],
        edge_len=arrays.edge_len[keep],
        geom_x=arrays.geom_x[vertices],
        geom_y=arrays.geom_y[vertices],
        geom_offsets=new_offsets,
    )


def edge_state_fractions(
    sample_x: npt.NDArray[np.float64],
    sample_y: npt.NDArray[np.float64],
    sample_edge: npt.NDArray[np.int64],
    n_edges: int,
    state: npt.NDArray[np.uint8],
    transform: Affine,
) -> tuple[npt.NDArray[np.float64], npt.NDArray[np.float64]]:
    """Per-edge fractions under one state raster: (in the sun, under canopy).

    Two numbers per edge because the shade states are not equivalent for a
    walker: a plane tree cools far better than a wall does (see shade-docs:
    learning/vegetation-cooling.md), so vegetation shade is kept apart from
    building and terrain shade, which stay implicit as
    ``1 - sun - vegetation``.

    The question here is comfort -- is there a crown over or behind me -- not
    the counterfactual the map layers answer, so ``STATE_SHADE_BOTH`` (crown
    *and* skyline) counts as vegetation. Splitting it the other way would tell
    the router that a shaded, tree-lined street is a wall.

    Out-of-grid samples and ``STATE_OUTSIDE`` pixels count as sun (and never
    as canopy): claiming shade where there is no data would fabricate exactly
    what a shade seeker is looking for. That is a floor, not the answer to
    missing data -- a city with a computation area drops those edges outright
    (:func:`edges_fully_covered`), because "sunlit" is a claim too.
    """
    rows, cols = rasterio.transform.rowcol(transform, sample_x, sample_y)
    rows, cols = np.asarray(rows), np.asarray(cols)
    height, width = state.shape
    inside = (rows >= 0) & (rows < height) & (cols >= 0) & (cols < width)
    sun = np.ones(len(sample_x), dtype=np.float64)
    vegetation = np.zeros(len(sample_x), dtype=np.float64)
    values = state[rows[inside], cols[inside]]
    sun[inside] = ((values == STATE_SUN) | (values == STATE_OUTSIDE)).astype(np.float64)
    vegetation[inside] = np.isin(values, (STATE_SHADE_VEGETATION, STATE_SHADE_BOTH)).astype(
        np.float64
    )
    counts = np.bincount(sample_edge, minlength=n_edges)
    sunny = np.bincount(sample_edge, weights=sun, minlength=n_edges)
    shaded_by_trees = np.bincount(sample_edge, weights=vegetation, minlength=n_edges)
    return sunny / counts, shaded_by_trees / counts


def graph_ladder() -> list[GraphRung]:
    """The tiles ladder annotated with each rung's fraction column indices.

    Columns follow ``season_preset_instants`` order exactly: consecutive
    hours within each rung, rungs in ``LADDER_PRESET_2026`` order.
    """
    rungs: list[GraphRung] = []
    col = 0
    for entry, (_day, first, last) in zip(declination_ladder(), LADDER_PRESET_2026, strict=True):
        columns = [
            RungColumn(time=f"{hour:02d}:00", col=col + offset)
            for offset, hour in enumerate(range(first, last + 1))
        ]
        col += len(columns)
        rungs.append(GraphRung.model_validate({**entry, "columns": columns}))
    return rungs


def write_route_graph(
    artifact_dir: Path,
    arrays: GraphArrays,
    fractions: npt.NDArray[np.uint8],
    veg_fractions: npt.NDArray[np.uint8],
    meta: RouteGraphMeta,
) -> Path:
    """Write the three graph files, then read them back and compare.

    The readback is the same "what was computed is what was stored"
    contract ``write_cog`` enforces since the corrupted-horizon postmortem;
    these files are small, so it costs nothing.
    """
    directory = artifact_dir / GRAPH_DIRNAME
    directory.mkdir(parents=True, exist_ok=True)
    np.savez(
        directory / GRAPH_FILENAME,
        node_x=arrays.node_x,
        node_y=arrays.node_y,
        edge_u=arrays.edge_u,
        edge_v=arrays.edge_v,
        edge_len=arrays.edge_len,
        geom_x=arrays.geom_x,
        geom_y=arrays.geom_y,
        geom_offsets=arrays.geom_offsets,
    )
    # Key names must match the loader's constants; the readback below is
    # what catches any drift.
    np.savez(
        directory / FRACTIONS_FILENAME,
        sun_fraction=fractions,
        veg_shade_fraction=veg_fractions,
    )
    (directory / GRAPH_META_FILENAME).write_text(
        meta.model_dump_json(indent=2) + "\n", encoding="utf-8"
    )

    stored = load_route_graph(artifact_dir)
    array_fields = (
        "node_x",
        "node_y",
        "edge_u",
        "edge_v",
        "edge_len",
        "geom_x",
        "geom_y",
        "geom_offsets",
    )
    for name in array_fields:
        if not np.array_equal(getattr(stored, name), getattr(arrays, name)):
            raise ValueError(f"{name}: readback mismatch after graph write")
    if not np.array_equal(stored.fractions, fractions):
        raise ValueError("fractions: readback mismatch after graph write")
    if not np.array_equal(stored.veg_fractions, veg_fractions):
        raise ValueError("veg fractions: readback mismatch after graph write")
    if stored.meta != meta:
        raise ValueError("graph.json: readback mismatch after graph write")
    return directory


def build_graph(
    config: CityConfig,
    artifact_dir: Path,
    source: GraphSource,
    *,
    spacing_m: float = DEFAULT_SPACING_M,
    progress: Callable[[str], None] | None = None,
) -> Path:
    """Fetch, extract, sample, precompute fractions and write the artifact."""
    echo = progress if progress is not None else lambda _message: None
    start = time.monotonic()
    metadata = load_metadata(artifact_dir)
    zone = ZoneInfo(config.timezone)
    bounds = bounds_wgs84(metadata.crs, metadata.bbox)

    echo("fetching OSM walk network")
    graph = source.fetch(bounds)
    arrays = extract_graph_arrays(graph, metadata.crs)
    n_nodes, n_edges = len(arrays.node_x), len(arrays.edge_len)
    echo(
        f"graph: {n_nodes:,} nodes, {n_edges:,} edges, "
        f"{float(arrays.edge_len.sum()) / 1000.0:.1f} km"
    )

    sample_x, sample_y, sample_edge = sample_edges(arrays, spacing_m)
    echo(f"sampling: {len(sample_x):,} points every {spacing_m:g} m")

    transform = transform_from_bbox(metadata.bbox, metadata.resolution_m)
    coverage = load_coverage(artifact_dir)
    if coverage is not None:
        # The graph is cut to what the build computed. The alternative is a
        # street with no data scored as sunlit, which is exactly what a shade
        # seeker is trying to avoid, offered to them as a route.
        keep = edges_fully_covered(sample_x, sample_y, sample_edge, n_edges, coverage, transform)
        dropped = int((~keep).sum())
        if dropped:
            lost_km = float(arrays.edge_len[~keep].sum()) / 1000.0
            arrays = keep_edges(arrays, keep)
            n_nodes, n_edges = len(arrays.node_x), len(arrays.edge_len)
            if n_edges == 0:
                raise ValueError(
                    "no walkable edge lies entirely inside the computation area; "
                    "the area and the OSM network do not overlap"
                )
            sample_x, sample_y, sample_edge = sample_edges(arrays, spacing_m)
            echo(
                f"computation area: {dropped:,} edges dropped ({lost_km:.1f} km), "
                f"{n_nodes:,} nodes and {n_edges:,} edges left"
            )

    # Same wall as the tile phase, and for the same reason: this loop calls
    # compute_state_raster once per ladder instant, each one a whole-raster
    # pass. It runs in this process and cannot be turned down, so all it can
    # do is say so before spending the first hour.
    warn_if_serial_is_tight(
        estimate_tiles_worker_bytes(*grid_shape(metadata.bbox, metadata.resolution_m)), progress
    )

    instants = season_preset_instants(zone)
    center_lon = (bounds[0] + bounds[2]) / 2.0
    center_lat = (bounds[1] + bounds[3]) / 2.0
    fractions = np.empty((n_edges, len(instants)), dtype=np.uint8)
    veg_fractions = np.empty((n_edges, len(instants)), dtype=np.uint8)
    for index, when in enumerate(instants):
        sun = sun_position(center_lat, center_lon, when)
        state = compute_state_raster(artifact_dir, sun)
        sunny, vegetation = edge_state_fractions(
            sample_x, sample_y, sample_edge, n_edges, state, transform
        )
        fractions[:, index] = np.rint(sunny * 255.0).astype(np.uint8)
        veg_fractions[:, index] = np.rint(vegetation * 255.0).astype(np.uint8)
        echo(f"sun fractions [{index + 1}/{len(instants)}] {when:%Y-%m-%d %H:%M}")

    meta = RouteGraphMeta(
        schema_version=ROUTE_GRAPH_SCHEMA_VERSION,
        city_id=config.id,
        crs=metadata.crs,
        network_type="walk",
        spacing_m=spacing_m,
        nodes=n_nodes,
        edges=n_edges,
        samples=len(sample_x),
        built_at=datetime.now(UTC),
        ladder=graph_ladder(),
        attribution=[OSM_ATTRIBUTION],
    )
    directory = write_route_graph(artifact_dir, arrays, fractions, veg_fractions, meta)
    echo(f"graph artifact written to {directory} in {format_duration(time.monotonic() - start)}")
    return directory
