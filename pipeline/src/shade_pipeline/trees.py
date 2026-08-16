"""The municipal tree inventory as ground truth for the canopy mask.

PNOA's LiDAR arrives pre-classified by an automatic classifier (NPC01), and
that classifier has to put every return somewhere: what it cannot call ground
or building it calls vegetation, ASPRS classes 3/4/5. Awnings, cables, market
stalls, vehicles and street furniture come out labelled as plants. See
``shade-docs: learning/clasificacion-lidar-pnoa.md``.

Nothing inside the LiDAR can settle that argument, because the LiDAR is the
one making the claim. A city that publishes its tree inventory can: every
street tree has a row in it, with a position good to a couple of metres. So
the inventory is not used to *paint* canopy -- it never adds a pixel -- but to
**audit** it. The build burns two zones onto the city grid:

- ``ZONE_DENSE`` within :data:`DENSE_RADIUS_M` of some specimen: streets and
  squares the surveyors walked, where the absence of a record means something.
- ``ZONE_NEAR`` within :data:`NEAR_RADIUS_M` of one: the reach of an alignment
  tree's crown plus the inventory's own positional error.

Outside both, ``ZONE_NONE``: hillside, riverbank and private courtyards, where
no record proves nothing at all. Measured on Cordoba, 57% of the canopy area
falls there, which is why the corroboration check in
:mod:`shade_pipeline.verify` only ever looks inside the dense zone.

The inventory never edits an artifact. It measures one, and a build whose
canopy stops matching the city's own trees fails instead of shipping.

A note on axis order, the classic WFS trap: WFS 2.0 honours the *authority*
axis order, so ``EPSG:4326`` comes back as (lat, lon) and not the (lon, lat)
GeoJSON promises. This module only ever asks for the city's projected CRS,
where EPSG:25830 is (easting, northing) and the ambiguity does not arise. See
``shade-docs: learning/crs.md``.
"""

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Protocol

import httpx
import numpy as np
import numpy.typing as npt
from affine import Affine
from scipy import ndimage

from shade_core.config import Bbox

DEFAULT_TREE_CACHE: Final = Path("data/cache/trees")

ZONE_NONE: Final = 0
"""Beyond the inventory's reach: absence of a record proves nothing here."""
ZONE_DENSE: Final = 1
"""Surveyed ground: within ``DENSE_RADIUS_M`` of a specimen, but not under one."""
ZONE_NEAR: Final = 2
"""Within ``NEAR_RADIUS_M`` of a specimen: canopy here is corroborated."""

DENSE_RADIUS_M: Final = 60.0
"""How far the inventory's authority reaches from a recorded specimen.

A street with trees in it is walked end to end by the survey, so a gap of a
few tens of metres between records is a gap between trees, not a gap in the
record. Sixty metres is about a city block: wide enough to cover the paving
between two alignment trees, tight enough that a wooded slope nobody surveyed
never enters the audit.
"""

NEAR_RADIUS_M: Final = 4.0
"""Crown reach of an alignment tree, plus the inventory's positional error.

An orange tree in a Cordoba street spans some 5-6 m of crown, so 4 m from the
trunk is inside it. A larger radius would corroborate the awning next to the
tree as readily as the tree.
"""

ZONE_BAND_ROWS: Final = 2048
"""Rows per band of the distance transform, which is what bounds its memory.

``distance_transform_edt`` allocates float64 distances plus an int32 nearest-
feature array per axis: about 16 bytes a pixel, or 1.6 GiB over Cordoba's
grid, at the end of a build that has already spent hours. Bands with a halo
of ``DENSE_RADIUS_M`` give the identical answer -- every distance the result
keeps is below the halo -- at a bounded cost.
"""

HTTP_TIMEOUT_S: Final = 180.0
"""Whole-layer WFS responses run to tens of MB over a municipal server."""


class TreeInventorySource(Protocol):
    def fetch(self, bbox: Bbox, crs: str) -> npt.NDArray[np.float64]:
        """Specimen positions inside ``bbox``, as (n, 2) x/y in ``crs``."""
        ...


@dataclass(frozen=True)
class WfsTreeSource:
    """Point layers from an OGC WFS 2.0 endpoint, cached on disk.

    Unlike :class:`shade_pipeline.footprints.OsmnxFootprintSource`, which has
    to speak lon/lat because Overpass only knows lon/lat, a WFS serves any CRS
    it advertises. Asking for the city's own projected CRS costs one round trip
    less and skips a reprojection that would move every trunk by a few
    centimetres for no gain.

    The bbox filter is not an optimisation: Cordoba's arboreal layer is 81 MB
    whole, and a build only ever needs the trees over its own grid.
    """

    url: str
    layers: tuple[str, ...]
    cache_dir: Path = DEFAULT_TREE_CACHE

    def fetch(self, bbox: Bbox, crs: str) -> npt.NDArray[np.float64]:
        chunks = [self._layer(layer, bbox, crs) for layer in self.layers]
        if not chunks:
            return np.zeros((0, 2), dtype=np.float64)
        return np.concatenate(chunks)

    def _layer(self, layer: str, bbox: Bbox, crs: str) -> npt.NDArray[np.float64]:
        path = self.cache_dir / _cache_name(layer, bbox, crs)
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(self._download(layer, bbox, crs))
        return _positions(json.loads(path.read_text(encoding="utf-8")), path)

    def _download(self, layer: str, bbox: Bbox, crs: str) -> bytes:
        min_x, min_y, max_x, max_y = bbox
        params = {
            "service": "WFS",
            "version": "2.0.0",
            "request": "GetFeature",
            "typeNames": layer,
            "outputFormat": "application/json",
            "srsName": crs,
            "bbox": f"{min_x},{min_y},{max_x},{max_y},{crs}",
        }
        response = httpx.get(self.url, params=params, timeout=HTTP_TIMEOUT_S, follow_redirects=True)
        response.raise_for_status()
        # GeoServer reports a bad request as a 200 carrying an XML
        # ExceptionReport, so the status code alone is not proof of anything.
        head = response.content.lstrip()[:1]
        if head != b"{":
            raise ValueError(
                f"{self.url}: {layer} did not answer with JSON "
                f"(first bytes: {response.content[:120]!r})"
            )
        return response.content


def _cache_name(layer: str, bbox: Bbox, crs: str) -> str:
    """A cache filename that says what is in it: layer, CRS and rounded bbox."""
    safe = re.sub(r"[^A-Za-z0-9]+", "-", f"{layer}-{crs}").strip("-")
    return f"{safe}-" + "-".join(str(round(value)) for value in bbox) + ".json"


def _positions(document: Any, source: Path) -> npt.NDArray[np.float64]:
    """The (n, 2) coordinates of every Point feature in a GeoJSON document."""
    if not isinstance(document, dict) or not isinstance(document.get("features"), list):
        raise ValueError(f"{source}: not a GeoJSON FeatureCollection")
    coordinates = [
        geometry["coordinates"][:2]
        for feature in document["features"]
        if isinstance(feature, dict)
        for geometry in [feature.get("geometry")]
        if isinstance(geometry, dict) and geometry.get("type") == "Point"
    ]
    if not coordinates:
        return np.zeros((0, 2), dtype=np.float64)
    return np.asarray(coordinates, dtype=np.float64)


def inventory_zones(
    positions: npt.NDArray[np.float64],
    transform: Affine,
    shape: tuple[int, int],
    *,
    dense_radius_m: float = DENSE_RADIUS_M,
    near_radius_m: float = NEAR_RADIUS_M,
    band_rows: int = ZONE_BAND_ROWS,
) -> npt.NDArray[np.uint8]:
    """Burn the inventory onto the grid as ``ZONE_NONE``/``DENSE``/``NEAR``.

    Distances are Euclidean and in metres, measured from the nearest specimen
    to the *centre* of each pixel. The transform is north-up with square
    pixels, as every artifact in this engine is, so one scale converts both
    axes.
    """
    rows, cols = shape
    resolution_m = float(transform.a)
    seed = np.zeros(shape, dtype=bool)
    if positions.size:
        col = np.floor((positions[:, 0] - transform.c) / resolution_m).astype(np.int64)
        row = np.floor((transform.f - positions[:, 1]) / resolution_m).astype(np.int64)
        inside = (row >= 0) & (row < rows) & (col >= 0) & (col < cols)
        seed[row[inside], col[inside]] = True

    zones = np.zeros(shape, dtype=np.uint8)
    if not seed.any():
        return zones

    halo = int(np.ceil(dense_radius_m / resolution_m))
    for start in range(0, rows, band_rows):
        stop = min(start + band_rows, rows)
        # The halo makes the band's answer identical to the whole grid's for
        # every distance under dense_radius_m, and those are the only ones
        # that survive the thresholds below.
        lo, hi = max(0, start - halo), min(rows, stop + halo)
        distance = ndimage.distance_transform_edt(~seed[lo:hi], sampling=resolution_m)
        assert isinstance(distance, np.ndarray)
        band = distance[start - lo : stop - lo]
        zones[start:stop] = np.where(
            band <= near_radius_m,
            ZONE_NEAR,
            np.where(band <= dense_radius_m, ZONE_DENSE, ZONE_NONE),
        )
    return zones


def corroborated_area(
    canopy: npt.NDArray[np.uint8] | npt.NDArray[np.bool_],
    zones: npt.NDArray[np.uint8],
) -> tuple[int, int]:
    """(area of corroborated crowns, area of judgeable crowns), in pixels.

    The question is asked **per crown, not per pixel**, and that is the whole
    difference between a useful number and a misleading one. A catalogued tree
    is one point; its crown is eight metres across. Asking "is this pixel
    within 4 m of a trunk" of a mature plane tree in a park answers no for
    most of the crown, and the measure collapses -- 0.45 on ``cordoba-test``
    against 0.74 for the same artifacts judged by region.

    So: connected regions of canopy, 8-connectivity, matching the mask's own
    sieve. A region is **judgeable** when most of it sits on surveyed ground,
    and a judgeable region is **corroborated** when any part of it reaches a
    catalogued specimen. Regions out in ``ZONE_NONE`` -- hillside, riverbank,
    private courtyards -- are not counted either way: there a crown with no
    record is a tree nobody catalogued, not a false positive.

    Reads the whole raster rather than streaming it, unlike everything else
    verification does: connected components have no windowed form, and a
    region split at a window edge is two regions. At Cordoba's size that is
    about 500 MB, spent once at the end of a build.
    """
    mask = np.asarray(canopy).astype(bool)
    labels, count = ndimage.label(mask, structure=np.ones((3, 3), dtype=np.uint8))
    if count == 0:
        return 0, 0
    index = np.arange(1, count + 1)
    areas = np.bincount(labels.ravel(), minlength=count + 1)[1:]
    surveyed = np.asarray(
        ndimage.mean((zones != ZONE_NONE).astype(np.float64), labels=labels, index=index)
    )
    judgeable = surveyed > 0.5
    reaches = np.bincount(labels[zones == ZONE_NEAR], minlength=count + 1)[1:] > 0
    return int(areas[judgeable & reaches].sum()), int(areas[judgeable].sum())
