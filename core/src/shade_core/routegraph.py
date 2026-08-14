"""Pedestrian route-graph artifact: the reading side.

A city's walking network is a graph: nodes are street intersections and
edges are the walkable segments between them, extracted from OSM ways
(footways, residential streets, paths...). The pipeline builds it once,
precomputes each edge's "sun fraction" for the same declination-ladder
instants the shade tiles use, and freezes everything as plain numpy arrays
under ``data/cities/<id>/v1/graph/``:

- ``graph.npz``: node coordinates (projected CRS, meters), edge endpoints,
  edge lengths, and every edge polyline concatenated into one ragged array
  (``geom_offsets[i]:geom_offsets[i + 1]`` slices edge ``i``'s vertices).
- ``fractions.npz``: one uint8 row per edge, one column per ladder instant;
  value = fraction of the edge's sample points standing in the sun, scaled
  to 0-255 (quantization error < 0.4%, far below the 5 m sampling step).
- ``graph.json``: provenance plus the ladder -> column mapping the API
  needs to resolve an arbitrary query instant to fraction columns.

Runtime routing needs only numpy over these arrays: no osmnx, no networkx,
no network access. Like ``canopy.tif`` and ``tiles/``, the graph directory
is an additive artifact -- absent from older builds, backfilled with
``shade-engine graph <city>``, invisible to ``metadata.json``.

Licensing: the graph geometry is derived from OpenStreetMap, so the
artifact and every response built on it must credit OSM under the ODbL
(see docs/learning/routing-graph.md).
"""

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Final

import numpy as np
import numpy.typing as npt
from pydantic import BaseModel

GRAPH_DIRNAME: Final = "graph"
GRAPH_FILENAME: Final = "graph.npz"
FRACTIONS_FILENAME: Final = "fractions.npz"
GRAPH_META_FILENAME: Final = "graph.json"
ROUTE_GRAPH_SCHEMA_VERSION: Final = 1
OSM_ATTRIBUTION: Final = "(c) OpenStreetMap contributors (ODbL)"


class RungColumn(BaseModel):
    """One fraction column: a local civil time ("HH:MM") on its rung's date."""

    time: str
    col: int


class GraphRung(BaseModel):
    """One declination-ladder rung plus the fraction columns rendered for it.

    ``covers`` lists the inclusive [start, end] ISO date ranges whose solar
    declination sits closest to this rung's; the API resolves any query date
    to a rung through them, exactly like the tiles manifest ``ladder``.
    """

    date: str
    declination_deg: float
    covers: list[tuple[str, str]]
    columns: list[RungColumn]


class RouteGraphMeta(BaseModel):
    """Contents of ``graph.json``: provenance plus the instant -> column map."""

    schema_version: int
    city_id: str
    crs: str
    network_type: str
    spacing_m: float
    nodes: int
    edges: int
    samples: int
    built_at: datetime
    ladder: list[GraphRung]
    attribution: list[str]


@dataclass(frozen=True)
class RouteGraphArtifact:
    """The graph artifact in memory, exactly as stored (undirected edges)."""

    node_x: npt.NDArray[np.float64]
    node_y: npt.NDArray[np.float64]
    edge_u: npt.NDArray[np.int32]
    edge_v: npt.NDArray[np.int32]
    edge_len: npt.NDArray[np.float32]
    geom_x: npt.NDArray[np.float64]
    geom_y: npt.NDArray[np.float64]
    geom_offsets: npt.NDArray[np.int64]
    fractions: npt.NDArray[np.uint8]
    meta: RouteGraphMeta


def load_route_graph(artifact_dir: str | Path) -> RouteGraphArtifact:
    """Load and cross-check a city's pedestrian graph artifact.

    Raises ``FileNotFoundError`` (actionable) when the graph was never
    built, ``ValueError`` when the three files disagree with each other or
    with their own metadata -- a truncated rsync must fail here at load
    time, not at query time.
    """
    directory = Path(artifact_dir) / GRAPH_DIRNAME
    meta_path = directory / GRAPH_META_FILENAME
    if not meta_path.exists():
        raise FileNotFoundError(
            f"{meta_path} missing; no pedestrian graph for this city -- "
            "run `shade-engine graph <city>` and redeploy the artifact"
        )
    meta = RouteGraphMeta.model_validate(json.loads(meta_path.read_text(encoding="utf-8")))
    if meta.schema_version != ROUTE_GRAPH_SCHEMA_VERSION:
        raise ValueError(
            f"unsupported graph schema_version {meta.schema_version} "
            f"(this build reads {ROUTE_GRAPH_SCHEMA_VERSION})"
        )
    with np.load(directory / GRAPH_FILENAME) as data:
        artifact = RouteGraphArtifact(
            node_x=data["node_x"],
            node_y=data["node_y"],
            edge_u=data["edge_u"],
            edge_v=data["edge_v"],
            edge_len=data["edge_len"],
            geom_x=data["geom_x"],
            geom_y=data["geom_y"],
            geom_offsets=data["geom_offsets"],
            fractions=_load_fractions(directory),
            meta=meta,
        )
    _check_coherence(artifact)
    return artifact


def _load_fractions(directory: Path) -> npt.NDArray[np.uint8]:
    with np.load(directory / FRACTIONS_FILENAME) as data:
        fractions: npt.NDArray[np.uint8] = data["sun_fraction"]
    return fractions


def _check_coherence(artifact: RouteGraphArtifact) -> None:
    """Every claim the files make about each other, verified up front."""
    meta = artifact.meta
    expected_dtypes = {
        "node_x": np.float64,
        "node_y": np.float64,
        "edge_u": np.int32,
        "edge_v": np.int32,
        "edge_len": np.float32,
        "geom_x": np.float64,
        "geom_y": np.float64,
        "geom_offsets": np.int64,
        "fractions": np.uint8,
    }
    for name, dtype in expected_dtypes.items():
        actual = getattr(artifact, name).dtype
        if actual != np.dtype(dtype):
            raise ValueError(f"graph artifact {name}: dtype {actual}, expected {np.dtype(dtype)}")

    nodes, edges = len(artifact.node_x), len(artifact.edge_u)
    if nodes == 0 or edges == 0:
        raise ValueError("graph artifact is empty")
    if len(artifact.node_y) != nodes:
        raise ValueError("graph artifact: node_x and node_y lengths differ")
    for name in ("edge_v", "edge_len"):
        if len(getattr(artifact, name)) != edges:
            raise ValueError(f"graph artifact: edge_u and {name} lengths differ")
    if meta.nodes != nodes or meta.edges != edges:
        raise ValueError(
            f"graph.json declares {meta.nodes} nodes / {meta.edges} edges, "
            f"arrays hold {nodes} / {edges}"
        )

    offsets = artifact.geom_offsets
    if len(offsets) != edges + 1 or offsets[0] != 0:
        raise ValueError("graph artifact: geom_offsets must be (edges + 1,) starting at 0")
    if np.any(np.diff(offsets) < 2):
        raise ValueError("graph artifact: every edge geometry needs at least 2 vertices")
    if offsets[-1] != len(artifact.geom_x) or len(artifact.geom_x) != len(artifact.geom_y):
        raise ValueError("graph artifact: geom_offsets do not match the vertex arrays")

    for name in ("edge_u", "edge_v"):
        indices = getattr(artifact, name)
        if indices.min() < 0 or indices.max() >= nodes:
            raise ValueError(f"graph artifact: {name} holds out-of-range node indices")
    if not np.all(np.isfinite(artifact.edge_len)) or artifact.edge_len.min() <= 0:
        raise ValueError("graph artifact: edge lengths must be positive and finite")

    columns = [column.col for rung in meta.ladder for column in rung.columns]
    if sorted(columns) != list(range(len(columns))):
        raise ValueError("graph.json ladder columns must cover 0..k-1 exactly once")
    if artifact.fractions.shape != (edges, len(columns)):
        raise ValueError(
            f"fractions shape {artifact.fractions.shape}, expected ({edges}, {len(columns)})"
        )
