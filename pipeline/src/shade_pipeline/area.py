"""Plan a city's computation area before spending hours building it.

A city's ``bbox`` is a rectangle, and a rectangle that covers a city covers a
lot of what is not the city. Declaring an ``area`` polygon keeps the same
raster georeference and marks which pixels are worth sweeping, so the cost
follows the shape of the town instead of the shape of its bounding box.

Drawing the polygon is somebody else's job: geojson.io and QGIS already do it
well and for free. What this module does is the arithmetic nobody wants to do
by hand -- project it, snap the bbox to whole pixels, count the sweep tiles it
saves, price it in minutes and memory, and say which PNOA tiles are still
missing from the cache -- so the choice of area is made against numbers rather
than against a hunch.

Coordinates arrive in EPSG:4326 (RFC 7946 says GeoJSON is lon/lat degrees, and
that is what a drawing tool exports). They are projected into the city CRS
immediately: areas, distances and pixel counts in degrees are meaningless.
See ``shade-docs: learning/geojson.md`` and ``learning/crs.md``.
"""

import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import numpy as np
import numpy.typing as npt
import rasterio.features
import shapely
from pyproj import Transformer
from shapely.geometry import mapping, shape
from shapely.geometry.base import BaseGeometry

from shade_core.config import Bbox, CityConfig
from shade_pipeline.budget import (
    available_bytes,
    estimate_sweep_worker_bytes,
    estimate_tiles_worker_bytes,
    workers_that_fit,
)
from shade_pipeline.cnig import TILE_KM, expected_tiles, parse_tile_name
from shade_pipeline.grid import buffer_pixels, grid_shape, transform_from_bbox
from shade_pipeline.horizon import tile_jobs
from shade_pipeline.progress import format_bytes, format_duration

WGS84: Final = "EPSG:4326"
DEFAULT_TILE_SIZES: Final = (128, 256, 512)

SWEEP_PX_PER_CORE_S: Final = 1340.0
"""Inner pixels one core sweeps per second, at the reference configuration.

Measured on ``montilla-test`` (1489 x 860 px, 64 sectors, 500 m radius,
1 m/px, ``--tile-size 256``): 15m 56s serial, so 1.339 px/s. Cost per pixel is
proportional to sectors times samples per sector, which is what the
``REFERENCE_*`` constants below normalise against.
"""
PARALLEL_PENALTY: Final = 0.114
"""Amdahl-style term fitted to the same city: x2,67 on 3 workers, x4,16 on 7.

Sweep workers share memory bandwidth, and the last cores of an SMT machine are
not whole cores. Ignoring this would promise a linear speedup the machine has
never once delivered.
"""
REFERENCE_SECTORS: Final = 64
REFERENCE_SAMPLES: Final = 500.0
"""``max_distance_m / resolution_m`` of the timed build: samples per sector."""

ARTIFACT_BYTES_PER_SWEPT_PIXEL: Final = 155
"""Compressed size of a finished artifact directory, per swept pixel.

Cordoba's 56 Mpx build writes about 8,6 GB of COGs, nearly all of it the three
horizon cubes. Uncovered pixels are constant and compress to almost nothing,
so the estimate scales with swept pixels rather than with the grid.
"""


class AreaError(ValueError):
    """The drawn geometry cannot serve as a computation area; the message says why."""


@dataclass(frozen=True)
class DrawnArea:
    """One polygon, in the city's CRS and in WGS84, plus how it arrived."""

    projected: BaseGeometry
    wgs84: BaseGeometry
    features: int
    repaired: bool

    @property
    def area_km2(self) -> float:
        return float(self.projected.area) / 1e6


@dataclass(frozen=True)
class TileSaving:
    """What one sweep tile size costs against a given area."""

    tile_size: int
    total: int
    swept: int
    total_px: int
    swept_px: int

    @property
    def skipped(self) -> int:
        return self.total - self.swept

    @property
    def saved(self) -> float:
        """Fraction of the grid's pixels the sweep never visits."""
        return 1.0 - (self.swept_px / self.total_px) if self.total_px else 0.0


@dataclass(frozen=True)
class LidarNeed:
    """PNOA tiles the padded bbox needs, and which are already downloaded."""

    tile_km: int
    needed: int
    cached: int
    missing: tuple[str, ...]


@dataclass(frozen=True)
class AreaPlan:
    """Everything the report prints: no formatting, no I/O."""

    city_id: str
    source: Path
    area: DrawnArea
    bbox: Bbox
    previous_bbox: Bbox
    rows: int
    cols: int
    savings: tuple[TileSaving, ...]
    tile_size: int
    workers: int
    sweep_worker_bytes: int
    sweep_workers_fit: int | None
    tiles_worker_bytes: int
    tiles_workers_fit: int | None
    scratch_bytes: int
    lidar: LidarNeed
    cache_dir: Path
    area_path: Path
    config_path: Path

    @property
    def box_km2(self) -> float:
        min_x, min_y, max_x, max_y = self.bbox
        return (max_x - min_x) * (max_y - min_y) / 1e6

    @property
    def chosen(self) -> TileSaving:
        """The saving at ``tile_size``, which is the one the estimates use."""
        for saving in self.savings:
            if saving.tile_size == self.tile_size:
                return saving
        raise AssertionError("the chosen tile size is always among the savings")


def _iter_geometries(raw: object, source: Path) -> list[dict[str, Any]]:
    """Every geometry in a GeoJSON document, whatever wrapper it came in."""
    if not isinstance(raw, dict):
        raise AreaError(f"{source}: the top level of a GeoJSON document is an object")
    kind = raw.get("type")
    if kind == "FeatureCollection":
        features = raw.get("features")
        if not isinstance(features, list):
            raise AreaError(f"{source}: FeatureCollection without a features array")
        return [
            feature["geometry"]
            for feature in features
            if isinstance(feature, dict) and isinstance(feature.get("geometry"), dict)
        ]
    if kind == "Feature":
        geometry = raw.get("geometry")
        return [geometry] if isinstance(geometry, dict) else []
    if kind is None:
        raise AreaError(f"{source}: no 'type' field; this is not GeoJSON")
    return [raw]


def _reject_projected(geometry: BaseGeometry, source: Path) -> None:
    """Refuse meters wearing the clothes of degrees.

    A polygon exported in UTM has eastings in the hundreds of thousands. Read
    as lon/lat it silently lands off the planet and every later number is
    nonsense, so the check happens at the door.
    """
    min_x, min_y, max_x, max_y = geometry.bounds
    if abs(min_x) > 180 or abs(max_x) > 180 or abs(min_y) > 90 or abs(max_y) > 90:
        raise AreaError(
            f"{source}: coordinates {geometry.bounds} are outside lon/lat range; "
            "GeoJSON is EPSG:4326 by RFC 7946 -- pass --geojson-crs if the file "
            "really is projected"
        )


def _reproject(geometry: BaseGeometry, source_crs: str, target_crs: str) -> BaseGeometry:
    if source_crs == target_crs:
        return geometry
    transformer = Transformer.from_crs(source_crs, target_crs, always_xy=True)
    return shapely.transform(
        geometry,
        lambda coords: np.stack(transformer.transform(coords[:, 0], coords[:, 1]), axis=1),
    )


def read_area(path: Path, city_crs: str, *, source_crs: str = WGS84) -> DrawnArea:
    """Load a drawn area and hand it back in both CRSs.

    Several features are merged: drawing tools happily produce one polygon per
    click, and the union is what the build will mask with.
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise AreaError(f"{path}: cannot be read ({exc})") from exc
    except json.JSONDecodeError as exc:
        raise AreaError(f"{path}: not valid JSON ({exc})") from exc

    parts = [shape(geometry) for geometry in _iter_geometries(raw, path)]
    if not parts:
        raise AreaError(f"{path}: no geometry inside")
    merged = shapely.union_all(parts)
    repaired = not merged.is_valid
    if repaired:
        # Self-intersections are what a hand-drawn ring produces when the last
        # vertex crosses the first; make_valid keeps the intent and the
        # predicates below stay meaningful.
        merged = shapely.make_valid(merged)
    if merged.is_empty or merged.area <= 0:
        raise AreaError(f"{path}: the geometry encloses no area (a point or a line is not an area)")
    if source_crs == WGS84:
        _reject_projected(merged, path)
    return DrawnArea(
        projected=_reproject(merged, source_crs, city_crs),
        wgs84=_reproject(merged, source_crs, WGS84),
        features=len(parts),
        repaired=repaired,
    )


def snap_bbox(bounds: Bbox, resolution_m: float) -> Bbox:
    """The smallest bbox on the pixel lattice that still contains ``bounds``.

    Snapping to absolute multiples of the resolution (not just to a whole
    number of pixels from an arbitrary origin) means two cities, or two
    successive areas of the same city, land on the same lattice and their
    pixels can be compared without resampling.
    """
    min_x, min_y, max_x, max_y = bounds
    return (
        math.floor(min_x / resolution_m) * resolution_m,
        math.floor(min_y / resolution_m) * resolution_m,
        math.ceil(max_x / resolution_m) * resolution_m,
        math.ceil(max_y / resolution_m) * resolution_m,
    )


def tile_saving(
    geometry: BaseGeometry, bbox: Bbox, resolution_m: float, tile_size: int
) -> TileSaving:
    """How many sweep tiles the area lets the build skip at this tile size.

    The build skips a tile with no covered pixel, and coverage is rasterized
    with ``all_touched``, so "has a covered pixel" is exactly "the polygon
    touches the tile" -- the geometric test used here.

    The saving is quantized by the tile: an area that leaves a sliver in a
    tile pays for the whole tile. That is why the report prices several sizes
    instead of assuming one.
    """
    rows, cols = grid_shape(bbox, resolution_m)
    min_x, _, _, max_y = bbox
    jobs = np.asarray(tile_jobs((0, rows, 0, cols), tile_size), dtype=np.int64)
    t0, t1, u0, u1 = jobs.T
    boxes = shapely.box(
        min_x + u0 * resolution_m,
        max_y - t1 * resolution_m,
        min_x + u1 * resolution_m,
        max_y - t0 * resolution_m,
    )
    shapely.prepare(geometry)
    hits = shapely.intersects(geometry, boxes)
    pixels = (t1 - t0) * (u1 - u0)
    return TileSaving(
        tile_size=tile_size,
        total=len(jobs),
        swept=int(hits.sum()),
        total_px=int(pixels.sum()),
        swept_px=int(pixels[hits].sum()),
    )


def coverage_mask(geometry: BaseGeometry, bbox: Bbox, resolution_m: float) -> npt.NDArray[np.bool_]:
    """Burn the area onto the city grid: True where the build has data.

    ``all_touched`` on purpose. Rasterizing a polygon is always an
    approximation -- the drawn edge is a straight line, the burnt one a
    staircase of whole pixels -- so the only choice is which way to err.
    Erring outward keeps a street the user drew inside the area, and costs
    nothing: those pixels get swept anyway as part of their tile. Erring
    inward would silently drop them. See
    ``shade-docs: learning/rasterizacion-de-poligonos.md``.
    """
    rows, cols = grid_shape(bbox, resolution_m)
    burnt: npt.NDArray[np.uint8] = rasterio.features.rasterize(
        [(geometry, 1)],
        out_shape=(rows, cols),
        transform=transform_from_bbox(bbox, resolution_m),
        all_touched=True,
        dtype="uint8",
    )
    return burnt.astype(bool)


def sweep_seconds(
    swept_px: int, sectors: int, max_distance_m: float, resolution_m: float, workers: int
) -> float:
    """Estimated sweep time, normalized off the measured reference build.

    Cost per pixel is proportional to sectors (one pass each) times samples per
    sector (``max_distance / resolution``), so a city with a different radius
    or resolution scales off the same measurement.
    """
    per_pixel = (sectors / REFERENCE_SECTORS) * (
        (max_distance_m / resolution_m) / REFERENCE_SAMPLES
    )
    speedup = workers / (1.0 + PARALLEL_PENALTY * (workers - 1))
    return swept_px * per_pixel / (SWEEP_PX_PER_CORE_S * speedup)


def lidar_needs(bbox: Bbox, buffer_m: float, tile_km: int, cache_dir: Path) -> LidarNeed:
    """PNOA tiles the padded bbox needs, and which are already in the cache.

    Names the missing ones with the same ``PNOA-*-<e>-<n>-*.laz`` pattern the
    downloader's own error uses, so a hand download is a copy and paste.
    """
    expected = expected_tiles(bbox, buffer_m, tile_km)
    cached: set[tuple[int, int]] = set()
    if cache_dir.is_dir():
        tiles = [
            *cache_dir.glob("*.laz", case_sensitive=False),
            *cache_dir.glob("*.las", case_sensitive=False),
        ]
        for path in tiles:
            key = parse_tile_name(path.name)
            if key is not None and key in expected:
                cached.add(key)
    return LidarNeed(
        tile_km=tile_km,
        needed=len(expected),
        cached=len(cached),
        missing=tuple(f"PNOA-*-{east}-{north}-*.laz" for east, north in sorted(expected - cached)),
    )


def plan_area(
    config: CityConfig,
    area: DrawnArea,
    source: Path,
    *,
    tile_size: int,
    workers: int,
    cache_dir: Path,
    area_path: Path,
    config_path: Path,
    tile_sizes: tuple[int, ...] = DEFAULT_TILE_SIZES,
) -> AreaPlan:
    """Every number the report needs, computed once."""
    resolution = config.resolution_m
    bbox = snap_bbox(area.projected.bounds, resolution)
    rows, cols = grid_shape(bbox, resolution)
    sizes = tuple(sorted({*tile_sizes, tile_size}))
    savings = tuple(tile_saving(area.projected, bbox, resolution, size) for size in sizes)
    pad = buffer_pixels(config.horizon_max_distance_m, resolution)
    sweep_bytes = estimate_sweep_worker_bytes(config.horizon_sectors, tile_size, pad)
    tiles_bytes = estimate_tiles_worker_bytes(rows, cols)
    return AreaPlan(
        city_id=config.id,
        source=source,
        area=area,
        bbox=bbox,
        previous_bbox=config.bbox,
        rows=rows,
        cols=cols,
        savings=savings,
        tile_size=tile_size,
        workers=workers,
        sweep_worker_bytes=sweep_bytes,
        sweep_workers_fit=workers_that_fit(sweep_bytes),
        tiles_worker_bytes=tiles_bytes,
        tiles_workers_fit=workers_that_fit(tiles_bytes),
        scratch_bytes=3 * config.horizon_sectors * rows * cols,
        lidar=lidar_needs(
            bbox,
            pad * resolution,
            TILE_KM.get(config.sources.get("pnoa_series", "LIDA3"), 1),
            cache_dir,
        ),
        cache_dir=cache_dir,
        area_path=area_path,
        config_path=config_path,
    )


def _fits(count: int | None) -> str:
    if count is None:
        return "this machine does not say how much memory is available"
    available = available_bytes()
    room = f" in {available / 2**30:.1f} GiB" if available is not None else ""
    return f"up to {count} workers fit{room}"


def format_plan(plan: AreaPlan, config: CityConfig) -> str:
    """The human report; every estimate says so."""
    area = plan.area
    lines = [
        f"{plan.city_id}: area from {plan.source}",
        f"  {area.features} feature(s), {area.area_km2:.2f} km2 in {config.crs}"
        + (" (self-intersecting; repaired)" if area.repaired else ""),
        "",
        "bbox snapped to whole pixels",
        f"  {bbox_literal(plan.bbox)}",
        f"  {plan.cols} x {plan.rows} px at {config.resolution_m:g} m "
        f"({plan.rows * plan.cols / 1e6:.1f} Mpx), box {plan.box_km2:.2f} km2",
        f"  the area is {100.0 * area.area_km2 / plan.box_km2:.0f}% of its bounding box",
    ]
    if plan.bbox != plan.previous_bbox:
        lines.append(
            f"  note: {plan.config_path} still says {bbox_literal(plan.previous_bbox)}; "
            "existing artifacts stop matching until the city is rebuilt"
        )

    lines += [
        "",
        "sweep (estimates; measured at 64 sectors, 500 m, 1 m/px, --tile-size 256)",
    ]
    for saving in plan.savings:
        serial = sweep_seconds(
            saving.swept_px,
            config.horizon_sectors,
            config.horizon_max_distance_m,
            config.resolution_m,
            1,
        )
        parallel = sweep_seconds(
            saving.swept_px,
            config.horizon_sectors,
            config.horizon_max_distance_m,
            config.resolution_m,
            plan.workers,
        )
        marker = " <-" if saving.tile_size == plan.tile_size else ""
        lines.append(
            f"  tile {saving.tile_size:>3}: {saving.swept} of {saving.total} tiles swept, "
            f"{100.0 * saving.saved:.0f}% of the pixels skipped, "
            f"{format_duration(serial)} serial, "
            f"{format_duration(parallel)} on {plan.workers} workers{marker}"
        )
    lines.append(
        "  (the rate is measured at tile 256; 512 runs about 12% slower per pixel, "
        "128 is unmeasured)"
    )
    lines.append(
        f"  memory at tile {plan.tile_size}: {format_bytes(plan.sweep_worker_bytes)} per worker, "
        f"{_fits(plan.sweep_workers_fit)}"
    )

    lines += [
        "",
        "tiles",
        f"  {format_bytes(plan.tiles_worker_bytes)} per worker, {_fits(plan.tiles_workers_fit)}",
        "",
        "disk",
        f"  scratch {format_bytes(plan.scratch_bytes)} "
        f"(3 cubes of {config.horizon_sectors} x {plan.rows} x {plan.cols})",
        f"  artifacts about "
        f"{format_bytes(ARTIFACT_BYTES_PER_SWEPT_PIXEL * plan.chosen.swept_px)} (estimate)",
        "",
        f"lidar ({config.sources.get('pnoa_series', 'LIDA3')}, {plan.lidar.tile_km} km tiles)",
        f"  {plan.lidar.needed} tiles cover the padded bbox, "
        f"{plan.lidar.cached} already under {plan.cache_dir}",
    ]
    if plan.lidar.missing:
        lines.append(f"  missing: {', '.join(plan.lidar.missing)}")

    lines += [
        "",
        f"{plan.config_path}",
        f"  bbox: {bbox_literal(plan.bbox)}",
        f"  area: {plan.area_path}",
    ]
    return "\n".join(lines)


def _number(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:g}"


def bbox_literal(bbox: Bbox) -> str:
    """A bbox as the YAML flow sequence the city files already use."""
    return "[" + ", ".join(_number(value) for value in bbox) + "]"


def _rounded(value: object) -> object:
    """Round every coordinate to 6 decimals: ~0,1 m of longitude, plenty."""
    if isinstance(value, float):
        return round(value, 6)
    if isinstance(value, (list, tuple)):
        return [_rounded(item) for item in value]
    if isinstance(value, dict):
        return {key: _rounded(item) for key, item in value.items()}
    return value


def area_geojson(area: DrawnArea, city_id: str) -> str:
    """The normalized area as a one-feature FeatureCollection in EPSG:4326.

    Whatever came in -- several features, a bare geometry, a projected file --
    goes out as one polygon in the CRS the rest of the pipeline assumes.
    """
    feature = {
        "type": "Feature",
        "properties": {"city": city_id},
        "geometry": _rounded(mapping(area.wgs84)),
    }
    return json.dumps({"type": "FeatureCollection", "features": [feature]}, indent=2) + "\n"


_BBOX_LINE = re.compile(r"^bbox:\s*\[[^\]]*\](.*)$")
_AREA_LINE = re.compile(r"^area:\s*(\S*)(.*)$")


def rewrite_config(text: str, bbox: Bbox, area_path: Path) -> str:
    """Set ``bbox`` and ``area`` in a city YAML, touching nothing else.

    Deliberately textual rather than a parse-and-dump round trip: every
    ``cities/*.yaml`` is hand-annotated, and a YAML dumper would silently
    delete every one of those comments. Trailing comments on the two edited
    lines survive too.
    """
    lines = text.split("\n")
    at_bbox = [index for index, line in enumerate(lines) if _BBOX_LINE.match(line)]
    if len(at_bbox) != 1:
        raise AreaError(
            f"expected exactly one top-level 'bbox:' line to rewrite, found {len(at_bbox)}"
        )
    index = at_bbox[0]
    bbox_match = _BBOX_LINE.match(lines[index])
    assert bbox_match is not None
    lines[index] = f"bbox: {bbox_literal(bbox)}{bbox_match.group(1)}"

    at_area = [position for position, line in enumerate(lines) if _AREA_LINE.match(line)]
    if len(at_area) > 1:
        raise AreaError(f"found {len(at_area)} top-level 'area:' lines; expected at most one")
    if at_area:
        area_match = _AREA_LINE.match(lines[at_area[0]])
        assert area_match is not None
        lines[at_area[0]] = f"area: {area_path}{area_match.group(2)}"
    else:
        lines.insert(index + 1, f"area: {area_path}")
    return "\n".join(lines)


__all__ = [
    "AreaError",
    "AreaPlan",
    "DrawnArea",
    "LidarNeed",
    "TileSaving",
    "area_geojson",
    "bbox_literal",
    "coverage_mask",
    "format_plan",
    "lidar_needs",
    "plan_area",
    "read_area",
    "rewrite_config",
    "snap_bbox",
    "sweep_seconds",
    "tile_saving",
]
