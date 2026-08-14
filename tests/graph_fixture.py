"""Synthetic walk graphs over the cube fixture city (no osmnx, no network).

Geometry designed against the cube's shadow (cube 20 m tall, footprint
x in [50, 70), y in [30, 50) in the scene's local frame; shade boundary at
``y = 50 + 18.4 / tan(elevation)`` for an eye at 1.6 m):

- The *pocket* edge (y = 60, x 54-66) sits 10 m north of the wall: deep in
  shade at the winter-solstice noon instant (shadow reaches ~34 m) and in
  the sun at the summer-solstice noon one (shadow reaches ~7.5 m). Its
  fraction must flip between those two ladder columns.
- The *north* edges (y = 88) sit beyond the winter shadow tip (~84 m):
  always sunny. The straight one is added as a reciprocal directed pair
  (extraction must collapse it) and the arc through (60, 94) as a true
  parallel edge (extraction must keep it).

Node ids are ints and coordinates are lon/lat (osmnx convention: node
attrs ``x``/``y``), so the fixture walks the exact code path a real OSM
download does.
"""

import networkx as nx
from pyproj import Transformer
from shapely import LineString

import synthetic
from shade_core.config import Bbox

_TO_WGS84 = Transformer.from_crs("EPSG:25830", "EPSG:4326", always_xy=True)

# Local scene coordinates (see synthetic.py's world frame).
POCKET_A = (54.0, 60.0)
POCKET_B = (66.0, 60.0)
NORTH_A = (54.0, 88.0)
NORTH_B = (66.0, 88.0)
ARC_MID = (60.0, 94.0)


def lonlat(local: tuple[float, float]) -> tuple[float, float]:
    lon, lat = _TO_WGS84.transform(
        synthetic.UTM_ORIGIN[0] + local[0], synthetic.UTM_ORIGIN[1] + local[1]
    )
    return float(lon), float(lat)


def cube_walk_graph() -> nx.MultiDiGraph:
    """4 nodes / 5 undirected edges after extraction; see module docstring."""
    graph = nx.MultiDiGraph()
    graph.graph["crs"] = "EPSG:4326"
    for node_id, local in ((1, POCKET_A), (2, POCKET_B), (3, NORTH_A), (4, NORTH_B)):
        lon, lat = lonlat(local)
        graph.add_node(node_id, x=lon, y=lat)
    graph.add_edge(1, 2)  # pocket edge, inside the winter-noon shadow
    graph.add_edge(1, 3)  # connectors pocket <-> north row
    graph.add_edge(2, 4)
    graph.add_edge(3, 4)  # north straight edge, as a reciprocal pair...
    graph.add_edge(4, 3)  # ...that extraction must collapse into one
    arc = LineString([lonlat(NORTH_A), lonlat(ARC_MID), lonlat(NORTH_B)])
    graph.add_edge(3, 4, geometry=arc)  # true parallel edge (distinct polyline)
    graph.add_edge(4, 3, geometry=LineString(list(arc.coords)[::-1]))
    return graph


class SyntheticWalkSource:
    """GraphSource that serves the fixture graph instead of touching Overpass."""

    def fetch(self, bbox_wgs84: Bbox) -> nx.MultiDiGraph:
        return cube_walk_graph()
