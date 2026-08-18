"""Building outlines as vectors: the drawing a 1 m raster cannot make.

A building has straight walls, and no raster mask of it does. The reason is
worth stating precisely, because it is easy to blame the LiDAR and wrong to:

- PNOA's point cloud over Montalban carries **5.29 first returns per square
  metre**, and the class-6 returns on a roof draw its edge straight to
  **+-0.26 m**, with 0.28 m between neighbouring points. The data is not the
  problem.
- 1 m is the finest grid that density supports. At 1 m only 1.9% of cells get
  no first return at all; at 0.5 m it is 14.2% and at 0.25 m it is 67.9%. A
  finer raster does not buy straighter walls, it buys holes.
- A cell becomes ``BUILDING`` when it holds **one** roof point, and almost no
  wall in a Spanish town runs parallel to the UTM grid. So every wall gains up
  to a whole cell, irregularly, and the mask comes out **17% larger** than the
  real footprint (median over 22 isolated buildings). No labelling rule fixes
  that: five were measured and they all land between 1.18 and 1.28.

So the staircase is ours, it is inherent to the grid, and the only thing that
draws a straight wall is a straight line. This module makes them: raster mask
-> polygons -> simplified -> **regularized against the building's own
orientation**, which is the step that turns a staircase into walls.

**What this does not fix.** The horizon sweep reads the same 1 m DSM, so the
edge of a shadow stays a 1 m staircase. This is a drawing, exactly like
:mod:`shade_pipeline.relief`; the shade is computed elsewhere and is not
improved by anything here.

See ``shade-docs: learning/vectorizar-mascara.md``, and
``learning/rasterizacion-de-poligonos.md`` for the same trip in the opposite
direction.
"""

import json
import math
from dataclasses import dataclass
from typing import Final, NamedTuple

import numpy as np
import numpy.typing as npt
import rasterio.features
import shapely
from affine import Affine
from pyproj import Transformer
from shapely.geometry import Polygon, mapping

WGS84: Final = "EPSG:4326"

MIN_AREA_M2: Final = 20.0
"""Smallest polygon worth drawing.

Of Montalban's 830 connected building components, 348 clear 20 m2 and they
carry **99.7% of the built area** (355,150 of 356,220 m2). What the threshold
drops is wall stubs, chimney clusters and single stray cells.

**This is a drawing threshold, never a modelling one.** A polygon left out of
this file still casts its shadow: the shade comes from the DSM and the mask,
which this module only reads. Reading a missing polygon as a deleted building
would be exactly backwards.
"""

SIMPLIFY_TOLERANCE_M: Final = 1.0
"""Douglas-Peucker tolerance, in metres -- one cell.

One cell is precisely the discretization error being removed, so it is the
only defensible value: anything smaller preserves the staircase it is meant to
drop, anything larger starts eating real corners. Measured over Montalban:
63,613 vertices become 23,895 and the total area moves +0.4%.

In **metres**, and that is the trap. A tolerance in degrees is not a distance:
it would be about 88 m of longitude here and a different number one province
north. Same rule as everywhere else in this engine -- geometry gets simplified
in the city's projected CRS, then reprojected.
"""

SNAP_DEG: Final = 25.0
"""How far an edge may sit from the dominant axis and still be snapped to it.

Above the deviation a 1 m staircase step can induce on a simplified edge and
below any chamfer somebody would actually build, so a warehouse with a cut
corner keeps it while a jagged wall gets straightened.
"""

FREE_EDGE_MIN_M: Final = 3.0
"""How long an edge must be before it is allowed to disagree with the axis.

Keeping an off-axis edge is how a real chamfer survives, and it is also how
staircase residue survives. Length is what separates the two: on a square
turned 30 degrees, every edge the tolerance refused to snap measured **2.0 to
2.24 m** -- three metres is more wall than that and less than any cut corner
worth drawing. Without this the same square came out with 15 vertices instead
of 4.
"""

EDGE_BIAS_CELLS: Final = 0.5
"""How far the finished outline is pulled in, in cells.

The mask does not merely stagger the wall, it **moves it outward**: a cell
becomes ``BUILDING`` when it holds one roof point, so a wall crossing anywhere
inside a cell claims the whole of it and the building region comes out dilated
by about half a cell on every side. Eroding by the same half cell puts it back.

Measured against the point cloud itself rather than against OSM, whose
footprints carry their own offset: over **339 walls longer than 8 m on 70
isolated buildings**, the distance from the drawn wall to the outermost class-6
return is **+0.59 m** without this and **+0.09 m** with it. Since neighbouring
returns sit 0.28 m apart, the outermost point already lies about 0.14 m inside
the true edge -- so +0.09 m is the wall landing on the roof edge, and there is
nothing left to correct.

It costs 14.4% of the mask's area, which is the over-claim itself: the same
mask measures 17% larger than the real footprint. **The shade does not move**:
this file is a drawing and the sweep never reads it.
"""

MAX_AREA_DRIFT: Final = 0.40
"""Area change above which a regularized polygon is thrown away.

The regularization solves for corners by intersecting lines, and on a shape
with no dominant orientation those intersections can run away. A polygon that
grew or shrank by more than this is not a tidier version of the input, so the
merely simplified one is kept instead.
"""

_MIN_EDGE_M: Final = 1e-9
"""Below this an edge has no direction, only rounding."""


@dataclass(frozen=True)
class OutlineSet:
    """The finished polygons, and how they were arrived at.

    ``regularized`` and ``fell_back`` are recorded because they are the honest
    quality signal for this layer: a city where most polygons fell back is one
    whose walls are still staircases, and nothing in the output says so.
    """

    polygons: tuple[Polygon, ...]
    regularized: int
    fell_back: int

    @property
    def area_m2(self) -> float:
        return float(sum(polygon.area for polygon in self.polygons))


def polygonize(
    mask: npt.NDArray[np.bool_],
    transform: Affine,
    *,
    min_area_m2: float = MIN_AREA_M2,
) -> list[Polygon]:
    """Connected runs of ``True`` as polygons in the raster's own CRS.

    Wraps ``rasterio.features.shapes``, which is the exact inverse of the
    ``rasterize`` this pipeline already uses on OSM footprints: there a polygon
    became cells, here cells become a polygon whose edges trace **cell
    boundaries**. That is why the result is a staircase and why it needs the
    two steps that follow.

    Holes come back as interior rings, which is what a patio is.
    """
    polygons = [
        shapely.geometry.shape(geometry)
        for geometry, value in rasterio.features.shapes(
            mask.astype(np.uint8), mask=mask, transform=transform
        )
        if value == 1
    ]
    return [polygon for polygon in polygons if polygon.area >= min_area_m2]


def dominant_angle(polygon: Polygon) -> float:
    """The orientation of the building, in degrees, in ``[0, 90)``.

    Modulo 90 because a rectangle has no single direction: its four walls are
    two pairs at right angles, and folding the circle into a quadrant makes
    them one number. 0 means walls parallel to the grid axes.

    Taken from the **minimum rotated rectangle** of the traced staircase -- the
    tightest box that contains it, whatever its angle -- because the staircase
    is a faithful envelope of the building even though not one of its edges
    points the right way.

    The obvious alternative, a length-weighted histogram of edge bearings, was
    tried first and is worse in exactly the way that matters. Douglas-Peucker
    does not chord a staircase along the wall: it lands on step corners, and
    those corners line up on lattice directions. On a square turned 30 degrees
    it returns **26.87** -- which is ``atan(1/2)``, a 2:1 step pattern and not
    a wall anywhere in the input. The box returns 29.98. On Montalban the two
    regularize nearly the same count (342 against 344 of 348) but the box
    leaves 9,750 exterior vertices against 10,654, which is the walls actually
    collapsing into one line instead of staying kinked.
    """
    box = np.asarray(polygon.minimum_rotated_rectangle.exterior.coords, dtype=np.float64)
    deltas = np.diff(box, axis=0)
    lengths = np.hypot(deltas[:, 0], deltas[:, 1])
    if not lengths.size or lengths.max() <= _MIN_EDGE_M:
        return 0.0
    longest = deltas[int(np.argmax(lengths))]
    return math.degrees(math.atan2(longest[1], longest[0])) % 90.0


def _wrap180(degrees: float) -> float:
    return (degrees + 180.0) % 360.0 - 180.0


def _intersect(
    point_a: npt.NDArray[np.float64],
    direction_a: npt.NDArray[np.float64],
    point_b: npt.NDArray[np.float64],
    direction_b: npt.NDArray[np.float64],
) -> npt.NDArray[np.float64] | None:
    """Where two infinite lines cross, or ``None`` when they do not."""
    matrix = np.array([[direction_a[0], -direction_b[0]], [direction_a[1], -direction_b[1]]])
    if abs(float(np.linalg.det(matrix))) < 1e-9:
        return None
    steps = np.linalg.solve(matrix, point_b - point_a)
    crossing: npt.NDArray[np.float64] = point_a + steps[0] * direction_a
    return crossing


def _snap_ring(
    ring: npt.NDArray[np.float64], angle_deg: float, snap_deg: float
) -> list[tuple[float, float]] | None:
    """One ring with its walls straightened, or ``None`` if it cannot be.

    Each edge close enough to the dominant axis (or its perpendicular) is
    replaced by an infinite line with that exact bearing through the edge's
    midpoint; edges too far off keep their own direction, so a real chamfer
    survives. The new corners are where consecutive lines cross, which is what
    makes a run of steps collapse into one continuous wall instead of being
    averaged into a diagonal.
    """
    coords = np.asarray(ring, dtype=np.float64)[:-1]  # a ring repeats its first point
    if len(coords) < 3:
        return None

    axes: list[_Axis] = []
    for index in range(len(coords)):
        start = coords[index]
        end = coords[(index + 1) % len(coords)]
        delta = end - start
        length = float(np.hypot(*delta))
        if length <= _MIN_EDGE_M:
            continue
        bearing = math.degrees(math.atan2(delta[1], delta[0]))
        target = angle_deg + 90.0 * round((bearing - angle_deg) / 90.0)
        if abs(_wrap180(bearing - target)) <= snap_deg or length < FREE_EDGE_MIN_M:
            radians = math.radians(target)
            direction = np.array([math.cos(radians), math.sin(radians)])
        else:
            direction = delta / length
        axes.append(_Axis((start + end) / 2.0, direction, length, end))

    merged = _merge_collinear(axes)
    if len(merged) < 3:
        return None

    corners: list[tuple[float, float]] = []
    for index, axis in enumerate(merged):
        following = merged[(index + 1) % len(merged)]
        corner = _intersect(axis.point, axis.direction, following.point, following.direction)
        # Two walls that ended up parallel have no crossing to offer. That is a
        # local failure, so it is answered locally: the vertex the two runs
        # already shared stays put and every other corner is still straightened.
        # Failing the whole ring here cost 36 of Montalban's 348 polygons.
        if corner is None:
            corners.append((float(axis.end[0]), float(axis.end[1])))
        else:
            corners.append((float(corner[0]), float(corner[1])))
    return corners


class _Axis(NamedTuple):
    """One wall in the making: where it is, which way it runs, and how long.

    ``end`` is the ring vertex this edge finished on, kept only so a corner
    that cannot be solved by intersection has somewhere honest to fall back to.
    """

    point: npt.NDArray[np.float64]
    direction: npt.NDArray[np.float64]
    length: float
    end: npt.NDArray[np.float64]


def _merge_collinear(axes: list[_Axis]) -> list[_Axis]:
    """Collapse consecutive edges that ended up on the same bearing.

    Snapping a staircase sends every other step to the same direction, and two
    parallel lines have no intersection to offer as a corner. Merging them
    first is what lets the run become a single wall; its position is the
    length-weighted mean of the steps' midpoints, which is the least-squares
    answer for where the wall actually was.

    Antiparallel edges are deliberately **not** merged: those are the two faces
    of something thin, not one wall seen twice.
    """
    if not axes:
        return []
    groups: list[list[_Axis]] = []
    for axis in axes:
        if groups and float(np.dot(groups[-1][0].direction, axis.direction)) > 1.0 - 1e-9:
            groups[-1].append(axis)
        else:
            groups.append([axis])
    # The ring is cyclic: the last group may continue into the first.
    if (
        len(groups) > 2
        and float(np.dot(groups[-1][0].direction, groups[0][0].direction)) > 1.0 - 1e-9
    ):
        groups[0] = groups.pop() + groups[0]

    collapsed = []
    for group in groups:
        weights = np.array([axis.length for axis in group])
        points = np.array([axis.point for axis in group])
        centre = (points * weights[:, None]).sum(axis=0) / weights.sum()
        collapsed.append(_Axis(centre, group[0].direction, float(weights.sum()), group[-1].end))
    return collapsed


def regularize(polygon: Polygon, angle_deg: float, *, snap_deg: float = SNAP_DEG) -> Polygon | None:
    """``polygon`` with its walls snapped to ``angle_deg``, or ``None``.

    Interior rings are straightened too, and for a reason worth naming: an
    interior ring is a patio, and a patio dropped or deformed is a roof
    invented over a courtyard the sun actually reaches.
    """
    shell = _snap_ring(np.asarray(polygon.exterior.coords), angle_deg, snap_deg)
    if shell is None:
        return None
    holes = []
    for interior in polygon.interiors:
        ring = _snap_ring(np.asarray(interior.coords), angle_deg, snap_deg)
        if ring is not None:
            holes.append(ring)

    result = Polygon(shell, holes)
    if not result.is_valid:
        # Intersecting lines can cross in the wrong order on a spiky outline.
        # buffer(0) is the standard repair; if it hands back anything other
        # than a single polygon the shape was too far gone to trust.
        result = result.buffer(0)
    if result.is_empty or result.geom_type != "Polygon":
        return None
    return result


def building_outlines(
    mask: npt.NDArray[np.bool_],
    transform: Affine,
    *,
    min_area_m2: float = MIN_AREA_M2,
    simplify_tolerance_m: float = SIMPLIFY_TOLERANCE_M,
    snap_deg: float = SNAP_DEG,
    edge_bias_cells: float = EDGE_BIAS_CELLS,
) -> OutlineSet:
    """The whole trip: mask -> polygons -> simplified -> regularized -> pulled in.

    Every polygon that cannot be regularized -- or whose area moved more than
    :data:`MAX_AREA_DRIFT` -- keeps its merely simplified form rather than
    being dropped. A staircase is a worse drawing than a wall and a better one
    than a hole.
    """
    bias_m = edge_bias_cells * abs(transform.a)
    regularized = 0
    fell_back = 0
    finished: list[Polygon] = []
    for polygon in polygonize(mask, transform, min_area_m2=min_area_m2):
        simplified = shapely.simplify(polygon, simplify_tolerance_m, preserve_topology=True)
        if simplified.is_empty or simplified.geom_type != "Polygon":
            fell_back += 1
            drawn = polygon
        else:
            angle = dominant_angle(polygon)
            candidate = regularize(simplified, angle, snap_deg=snap_deg)
            drift = abs(candidate.area - polygon.area) / polygon.area if candidate else math.inf
            if candidate is None or drift > MAX_AREA_DRIFT:
                fell_back += 1
                drawn = simplified
            else:
                regularized += 1
                drawn = candidate
        pulled = _pull_in(drawn, bias_m)
        if pulled is not None:
            finished.append(pulled)
    return OutlineSet(tuple(finished), regularized=regularized, fell_back=fell_back)


def _pull_in(polygon: Polygon, bias_m: float) -> Polygon | None:
    """Erode by ``bias_m``, keeping corners square and the largest piece.

    ``mitre`` because the corners have just been squared and a round join would
    undo that. Eroding can split something thin in two or erase it; the largest
    surviving piece is kept, and nothing at all means the shape was narrower
    than the correction and had no wall to speak of.
    """
    if bias_m <= 0.0:
        return polygon
    eroded = polygon.buffer(-bias_m, join_style="mitre")
    if eroded.is_empty:
        return None
    if eroded.geom_type == "MultiPolygon":
        eroded = max(eroded.geoms, key=lambda part: part.area)
    return eroded if eroded.geom_type == "Polygon" and eroded.area > 0.0 else None


def outlines_geojson(polygons: tuple[Polygon, ...] | list[Polygon], crs: str) -> str:
    """The polygons as a GeoJSON FeatureCollection in EPSG:4326.

    GeoJSON is lon/lat by RFC 7946 -- the opposite order from what a WFS hands
    back in the same CRS -- so the transformer is built ``always_xy`` and the
    output needs no swapping. Coordinates are rounded to six decimals, about
    0.1 m of longitude here: far finer than a 1 m raster can justify, and it
    keeps the file the browser downloads small.

    See ``shade-docs: learning/geojson.md``.
    """
    transformer = Transformer.from_crs(crs, WGS84, always_xy=True)

    def to_wgs84(coords: npt.NDArray[np.float64]) -> npt.NDArray[np.float64]:
        return np.stack(transformer.transform(coords[:, 0], coords[:, 1]), axis=1)

    features = [
        {
            "type": "Feature",
            "properties": {},
            "geometry": _rounded(mapping(shapely.transform(polygon, to_wgs84))),
        }
        for polygon in polygons
    ]
    return json.dumps({"type": "FeatureCollection", "features": features}, separators=(",", ":"))


def _rounded(value: object) -> object:
    """Round every coordinate to 6 decimals: ~0.1 m of longitude, plenty."""
    if isinstance(value, float):
        return round(value, 6)
    if isinstance(value, (list, tuple)):
        return [_rounded(item) for item in value]
    if isinstance(value, dict):
        return {key: _rounded(item) for key, item in value.items()}
    return value
