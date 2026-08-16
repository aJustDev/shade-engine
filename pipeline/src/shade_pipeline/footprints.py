"""OSM building footprints as a corrective on the LiDAR landcover.

PNOA's automatic classification is generous with vegetation over rooftops.
Measured on 240 x 240 m of the Cordoba historic centre: of 32.607 cells above
8 m, 6.832 came out VEGETATION, and half of those held no building point at
all -- so no per-cell tie-break (see ``BUILDING_MARGIN_M`` in
``shade_pipeline.rasterize``) can rescue them. What those cells *do* have is an
address: they sit inside a building the OSM community has drawn.

The rule is per footprint, not global, because Cordoba's blocks are full of
patios and a blanket "inside a footprint means building" would fell every
courtyard tree in the city::

    roof_p = median CHM of the BUILDING cells inside footprint p
    flip   = footprint p & VEGETATION & (chm >= roof_p - ROOF_TOLERANCE_M)

A crown below the eaves survives; a "tree" resting on the tile plane does not.
Footprints with no building cell of their own are skipped: with no roof
reference there is nothing to compare against.

Only the *label* moves. This module changes who gets blamed for a shadow,
never whether the shadow exists. That used to be true of the whole build; it
is now true only of this module, since :mod:`shade_pipeline.declutter` edits
the surface itself (ADR-022).
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Protocol

import numpy as np
import numpy.typing as npt
import rasterio.features
from affine import Affine
from shapely.geometry.base import BaseGeometry

from shade_core.config import Bbox
from shade_core.shade import Landcover

DEFAULT_OSM_CACHE: Final = Path("data/cache/osm")

ROOF_TOLERANCE_M: Final = 2.0
"""How far below its roof's median a vegetation cell may sit and still flip.

Wide enough to swallow the spread of a real roofscape (eaves, ridges, the
odd terrace) and narrow enough to leave a patio tree alone: a courtyard
orange tree in the historic centre stands well below the third-floor eaves.
"""


class FootprintSource(Protocol):
    def fetch(self, bbox_wgs84: Bbox, crs: str) -> list[BaseGeometry]:
        """Building polygons covering the bbox, in the given projected CRS."""
        ...


@dataclass(frozen=True)
class OsmnxFootprintSource:
    """Downloads building footprints from Overpass through osmnx, cached on disk.

    Shares the cache directory and the lazy import of
    :class:`shade_pipeline.graph.OsmnxWalkSource`: same server, same etiquette,
    and a build that already fetched the walk network hits a warm cache.
    """

    cache_dir: Path = DEFAULT_OSM_CACHE

    def fetch(self, bbox_wgs84: Bbox, crs: str) -> list[BaseGeometry]:
        import osmnx as ox  # lazy: pulls geopandas; only this path pays the import
        from osmnx._errors import InsufficientResponseError

        ox.settings.cache_folder = str(self.cache_dir)
        west, south, east, north = bbox_wgs84
        try:
            features = ox.features_from_bbox((west, south, east, north), tags={"building": True})
        except InsufficientResponseError:
            # "Nothing mapped here" is an answer, not a failure: a village with
            # no footprints drawn yet builds fine, just without the correction.
            # A real Overpass outage raises something else and still aborts.
            return []
        if features.empty:
            return []
        # Relations arrive as points and lines too (entrances, building parts
        # mapped as nodes); only areas can hold a roof.
        areas = features[features.geometry.geom_type.isin(("Polygon", "MultiPolygon"))]
        if areas.empty:
            return []
        return [geometry for geometry in areas.to_crs(crs).geometry if not geometry.is_empty]


def footprint_ids(
    geometries: Sequence[BaseGeometry] | Iterable[BaseGeometry],
    transform: Affine,
    shape: tuple[int, int],
) -> npt.NDArray[np.int32]:
    """Burn one id per polygon (1-based; 0 = no footprint) onto the grid.

    Overlapping polygons resolve to whichever burns last, which is fine: the
    rule only needs *a* roof reference for the cell, and two overlapping
    footprints are two halves of the same building.
    """
    shapes = [(geometry, index) for index, geometry in enumerate(geometries, start=1)]
    if not shapes:
        return np.zeros(shape, dtype=np.int32)
    burned: npt.NDArray[np.int32] = rasterio.features.rasterize(
        shapes,
        out_shape=shape,
        transform=transform,
        fill=0,
        dtype=np.int32,
    )
    return burned


def apply_footprint_override(
    landcover: npt.NDArray[np.uint8],
    chm: npt.NDArray[np.floating],
    ids: npt.NDArray[np.int32],
    *,
    roof_tolerance_m: float = ROOF_TOLERANCE_M,
) -> int:
    """Relabel roof-height vegetation inside footprints as building, in place.

    Returns the number of cells flipped. Works on flat indices of the
    candidate cells only: a per-pixel threshold array would be another
    float-sized copy of the padded city grid.
    """
    count = int(ids.max()) if ids.size else 0
    if count == 0:
        return 0

    # Median CHM per footprint over its building cells. A grouped median has no
    # vectorized form, so group by sorting once and walk the runs; at city
    # scale that is tens of thousands of tiny medians, well under a second.
    roof_ids = np.where(landcover == Landcover.BUILDING, ids, 0).ravel()
    selected = roof_ids > 0
    roof_ids = roof_ids[selected]
    roof_chm = np.asarray(chm).ravel()[selected]
    order = np.argsort(roof_ids, kind="stable")
    roof_ids = roof_ids[order]
    roof_chm = roof_chm[order]
    labels = np.arange(1, count + 1, dtype=np.int32)
    starts = np.searchsorted(roof_ids, labels, side="left")
    ends = np.searchsorted(roof_ids, labels, side="right")
    # inf for footprints with no roof reference: nothing ever clears the bar.
    threshold = np.full(count + 1, np.inf, dtype=np.float64)
    for label, start, end in zip(labels, starts, ends, strict=True):
        if end > start:
            threshold[label] = float(np.median(roof_chm[start:end])) - roof_tolerance_m

    candidates = np.flatnonzero(((ids > 0) & (landcover == Landcover.VEGETATION)).ravel())
    if not candidates.size:
        return 0
    tall = np.asarray(chm).ravel()[candidates] >= threshold[ids.ravel()[candidates]]
    flipped = candidates[tall]
    landcover.flat[flipped] = Landcover.BUILDING
    return int(flipped.size)
