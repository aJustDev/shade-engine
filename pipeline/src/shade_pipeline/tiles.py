"""Per-instant shade overlays as raster PMTiles (see shade-docs: learning/map-tiles-pmtiles.md).

Web maps consume square 256 px tiles addressed by z/x/y in Web Mercator
(EPSG:3857): at zoom z the world is a 2^z x 2^z grid, x grows east and y
grows *south*. PMTiles packs a whole tile pyramid into one static file with
a Hilbert-ordered directory at the front, so a browser fetches any tile
with plain HTTP range requests -- the COG trick applied to web pyramids,
and the reason serving these needs no tile server, only Caddy.

Zoom bounds: Web Mercator inflates distances by 1/cos(lat) (see
shade-docs: learning/web-mercator.md), so at Cordoba's latitude (37.9 N) zoom 17
is 156543/2^17 * cos(37.9) = 0.94 m/px -- our native 1 m resolution. Higher
zooms would only upsample (the map client already overzooms past max_zoom);
zoom 12 (~30 m/px) fits the whole city on two tiles.

Each instant becomes TWO cast-shade files sharing one color -- buildings
(plus "other") and tree shadow -- independently toggleable (hiding the
trees set shows the streets-without-trees scenario), while the crowns
themselves live in a single static ``canopy.pmtiles`` per city: the
vertical projection of ``canopy.tif``, identical at every hour. The split
follows the physics: under-canopy shade never moves (opaque-crown
assumption), cast shadows rotate with the sun. Tiles for a fixed sun
position are immutable, which is what makes the static approach work (and
cacheable forever). The engine itself is never consulted at view time.

The PNG palette keeps a distinct index per state even where colors
coincide, so a decoded tile still knows which pixels are tree shadow;
recoloring is a palette edit away, no re-render needed.
"""

import io
import itertools
import json
import math
import time
from collections.abc import Callable, Generator, Mapping, Sequence
from concurrent.futures import ProcessPoolExecutor, as_completed
from concurrent.futures.process import BrokenProcessPool
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, tzinfo
from pathlib import Path
from typing import Final

import mercantile
import numpy as np
import numpy.typing as npt
import rasterio
from affine import Affine
from PIL import Image
from pmtiles.tile import Compression, TileType, zxy_to_tileid
from pmtiles.writer import Writer
from pvlib.solarposition import declination_spencer71
from pyproj import Transformer
from rasterio.enums import Resampling
from rasterio.io import MemoryFile
from rasterio.transform import from_origin
from rasterio.vrt import WarpedVRT
from rasterio.windows import Window

from shade_core.artifacts import (
    CANOPY_FILENAME,
    COVERAGE_FILENAME,
    LANDCOVER_FILENAME,
    load_metadata,
)
from shade_core.config import Bbox, CityConfig
from shade_core.shade import Landcover
from shade_core.solar import SunPosition, sun_position
from shade_pipeline.area import read_area, wgs84_geometry
from shade_pipeline.budget import check_worker_budget, cpu_budget, estimate_tiles_worker_bytes
from shade_pipeline.grid import grid_shape, transform_from_bbox
from shade_pipeline.progress import format_bytes, format_duration
from shade_pipeline.shade_raster import (
    STATE_OUTSIDE,
    STATE_SHADE_BOTH,
    STATE_SHADE_BUILDING,
    STATE_SHADE_OTHER,
    STATE_SHADE_VEGETATION,
    STATE_SUN,
    compute_state_raster,
)

DEFAULT_MIN_ZOOM: Final = 12
DEFAULT_MAX_ZOOM: Final = 17  # ~1 m/px at lat 37.9; see module docstring
TILE_SIZE: Final = 256
TILES_DIRNAME: Final = "tiles"
MANIFEST_FILENAME: Final = "index.json"
BASEMAP_FILENAME: Final = "basemap.pmtiles"

_WEB_MERCATOR_CIRCUMFERENCE: Final = 2.0 * math.pi * 6378137.0

OVERLAY_ALPHA: Final = 200
SHADE_COLOR: Final = (36, 48, 94, OVERLAY_ALPHA)  # deep indigo: any cast shade
CANOPY_COLOR: Final = (31, 90, 74, OVERLAY_ALPHA)  # green-teal: static tree crowns

SHADE_COLORS: Final[dict[int, tuple[int, int, int, int]]] = {
    STATE_SUN: (0, 0, 0, 0),  # sun = transparent: the overlay only paints shade
    STATE_SHADE_BUILDING: SHADE_COLOR,
    # Cast tree shadow renders exactly like building shade (indigo): its own
    # palette index survives in the file, only the color coincides.
    STATE_SHADE_VEGETATION: SHADE_COLOR,
    STATE_SHADE_OTHER: SHADE_COLOR,
    STATE_OUTSIDE: (0, 0, 0, 0),
}
CANOPY_COLORS: Final[dict[int, tuple[int, int, int, int]]] = {
    STATE_SUN: (0, 0, 0, 0),
    STATE_SHADE_BUILDING: (0, 0, 0, 0),
    STATE_SHADE_VEGETATION: CANOPY_COLOR,
    STATE_SHADE_OTHER: (0, 0, 0, 0),
    STATE_OUTSIDE: (0, 0, 0, 0),
}

CANOPY_TILES_FILENAME: Final = "canopy.pmtiles"

BUILDINGS_COLOR: Final = (61, 67, 80, OVERLAY_ALPHA)  # slate grey: LiDAR footprint
BUILDINGS_COLORS: Final[dict[int, tuple[int, int, int, int]]] = {
    STATE_SUN: (0, 0, 0, 0),
    STATE_SHADE_BUILDING: BUILDINGS_COLOR,
    STATE_SHADE_VEGETATION: (0, 0, 0, 0),
    STATE_SHADE_OTHER: (0, 0, 0, 0),
    STATE_OUTSIDE: (0, 0, 0, 0),
}
BUILDINGS_TILES_FILENAME: Final = "buildings.pmtiles"

# The 2026 declination-ladder preset: 7 canonical dates at ~even solar
# declination steps (-23.4 to +23.4 in ~7.8 deg rungs), each rendered hourly
# within safe daylight (both boundary hours verified > 1.4 deg of apparent
# elevation at Cordoba and Montilla; more-eastern cities would shift solar
# time and should re-check). Why declination and not calendar weeks: a fixed
# instant's shade depends only on the sun's (azimuth, elevation), which
# depend only on declination and local solar time -- and declination is
# SYMMETRIC around the solstices (May 4 == Aug 9, Mar 21 == Sep 22...), so 7
# rungs cover the whole year and any calendar date maps to its declination
# twin with < ~4 deg of error at the rung midpoint (see the manifest
# ``ladder`` field, which ships that mapping). Entries: (date, first hour,
# last hour), civil local hours; ZoneInfo resolves each date's DST offset.
LADDER_PRESET_2026: Final[tuple[tuple[str, int, int], ...]] = (
    ("2026-02-07", 9, 18),  # decl -15.6 (CET)
    ("2026-03-01", 8, 18),  # decl -7.9 (CET)
    ("2026-03-21", 8, 19),  # decl ~0: equinox (CET)
    ("2026-04-10", 8, 20),  # decl +7.7 (CEST)
    ("2026-05-04", 8, 21),  # decl +15.7 (CEST)
    ("2026-06-21", 8, 21),  # decl +23.4: summer solstice (CEST)
    ("2026-12-21", 9, 17),  # decl -23.4: winter solstice (CET)
)

# PNG palette: state code -> palette index; colors and per-index alpha (tRNS
# chunk) travel with every tile. Browsers decode paletted PNG natively and
# the flat-color tiles compress to a few KB each.
PALETTE_STATES: Final = (
    STATE_SUN,
    STATE_SHADE_BUILDING,
    STATE_SHADE_VEGETATION,
    STATE_SHADE_OTHER,
    STATE_OUTSIDE,
)


def palette_bytes(colors: Mapping[int, tuple[int, int, int, int]]) -> tuple[bytes, bytes]:
    """(RGB palette, per-index alpha) PNG chunks for a state->RGBA mapping."""
    rgb = bytes(channel for state in PALETTE_STATES for channel in colors[state][:3])
    trns = bytes(colors[state][3] for state in PALETTE_STATES)
    return rgb, trns


def _palette_index() -> npt.NDArray[np.uint8]:
    index = np.zeros(256, dtype=np.uint8)
    for position, state in enumerate(PALETTE_STATES):
        index[state] = position
    return index


_INDEX_OF_STATE: Final = _palette_index()


def season_preset_instants(tz: tzinfo) -> list[datetime]:
    """The 2026 declination-ladder preset as aware datetimes in the city's zone.

    DST trap: the preset straddles the March/October changes (Spain runs
    UTC+1 in March/December, UTC+2 in June/September). ``ZoneInfo`` resolves
    each date's offset; never bake a fixed offset into a list of instants
    that crosses a DST boundary.
    """
    return [
        datetime.fromisoformat(f"{day}T{hour:02d}:00").replace(tzinfo=tz)
        for day, first, last in LADDER_PRESET_2026
        for hour in range(first, last + 1)
    ]


def declination_ladder() -> list[dict[str, object]]:
    """The manifest ``ladder``: each rung's declination and the dates it covers.

    Every 2026 calendar day is assigned to the rung with the closest solar
    declination (Spencer 1971 series, the same approximation family pvlib
    uses for fast solar position). Because declination is symmetric around
    the solstices, most rungs cover two date ranges (one per half-year):
    August 9 maps to the May 4 rung, October days to the March ones. The
    client resolves any picked date to its rung through ``covers``.
    """

    def decl(day: date) -> float:
        return float(np.degrees(declination_spencer71(day.timetuple().tm_yday)))

    rungs = [date.fromisoformat(day) for day, _first, _last in LADDER_PRESET_2026]
    rung_decl = {day: decl(day) for day in rungs}
    year = [date(2026, 1, 1) + timedelta(days=n) for n in range(365)]
    assigned = {day: min(rungs, key=lambda rung: abs(decl(day) - rung_decl[rung])) for day in year}
    covers: dict[date, list[list[str]]] = {day: [] for day in rungs}
    run_start = year[0]
    for previous, day in itertools.pairwise(year):
        if assigned[day] is not assigned[previous]:
            covers[assigned[previous]].append([run_start.isoformat(), previous.isoformat()])
            run_start = day
    covers[assigned[year[-1]]].append([run_start.isoformat(), year[-1].isoformat()])
    return [
        {
            "date": day.isoformat(),
            "declination_deg": round(rung_decl[day], 2),
            "covers": covers[day],
        }
        for day in rungs
    ]


def bounds_wgs84(crs: str, bbox: Bbox) -> Bbox:
    """(west, south, east, north) in degrees covering a projected bbox.

    Transforms all four corners and takes the envelope: projected edges
    curve in lon/lat, so transforming only two corners can clip the extent.
    """
    transformer = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
    min_x, min_y, max_x, max_y = bbox
    lons, lats = transformer.transform([min_x, max_x, min_x, max_x], [min_y, min_y, max_y, max_y])
    return (min(lons), min(lats), max(lons), max(lats))


def _encode_png(state_tile: npt.NDArray[np.uint8], palette: tuple[bytes, bytes]) -> bytes:
    rgb, trns = palette
    image = Image.fromarray(_INDEX_OF_STATE[state_tile], mode="P")
    image.putpalette(rgb)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True, transparency=trns)
    return buffer.getvalue()


def write_instant_pmtiles(
    path: Path,
    state: npt.NDArray[np.uint8],
    transform: Affine,
    crs: str,
    bounds: Bbox,
    *,
    min_zoom: int = DEFAULT_MIN_ZOOM,
    max_zoom: int = DEFAULT_MAX_ZOOM,
    metadata: dict[str, object] | None = None,
    colors: Mapping[int, tuple[int, int, int, int]] = SHADE_COLORS,
) -> tuple[int, int]:
    """Warp a state raster into one PMTiles pyramid; returns (written, skipped).

    Zooms are walked ascending and tiles within a zoom in Hilbert (tileid)
    order, so the writer stays clustered. Per zoom, one ``WarpedVRT``
    reprojects the native raster to a Web Mercator grid anchored on the
    tile lattice (nearest resampling: states are categorical), and every
    tile is a plain 256 px window read.

    Fully transparent tiles (all sun or outside) are skipped above
    ``min_zoom``: an absent tile renders as nothing, and most of a pyramid
    is sun. At ``min_zoom`` tiles are always written -- the writer cannot
    finalize an empty archive, and deduplication stores the shared blank
    PNG only once.
    """
    west, south, east, north = bounds
    palette = palette_bytes(colors)
    written = 0
    skipped = 0
    with MemoryFile() as memory:
        rows, cols = state.shape
        with memory.open(
            driver="GTiff",
            width=cols,
            height=rows,
            count=1,
            dtype="uint8",
            crs=crs,
            transform=transform,
            nodata=int(STATE_OUTSIDE),
        ) as dataset:
            dataset.write(state, 1)
        with memory.open() as source, open(path, "wb") as sink:
            writer = Writer(sink)
            for zoom in range(min_zoom, max_zoom + 1):
                tiles = sorted(
                    mercantile.tiles(west, south, east, north, [zoom]),
                    key=lambda t: int(zxy_to_tileid(t.z, t.x, t.y)),
                )
                x0 = min(t.x for t in tiles)
                y0 = min(t.y for t in tiles)
                resolution = _WEB_MERCATOR_CIRCUMFERENCE / (2**zoom * TILE_SIZE)
                corner = mercantile.xy_bounds(x0, y0, zoom)
                with WarpedVRT(
                    source,
                    crs="EPSG:3857",
                    transform=from_origin(corner.left, corner.top, resolution, resolution),
                    width=(max(t.x for t in tiles) - x0 + 1) * TILE_SIZE,
                    height=(max(t.y for t in tiles) - y0 + 1) * TILE_SIZE,
                    resampling=Resampling.nearest,
                    nodata=float(STATE_OUTSIDE),
                ) as vrt:
                    for tile in tiles:
                        window = Window(
                            (tile.x - x0) * TILE_SIZE,
                            (tile.y - y0) * TILE_SIZE,
                            TILE_SIZE,
                            TILE_SIZE,
                        )
                        # List index: rasterio's int-index path trips a numpy
                        # 2.5 in-place reshape deprecation.
                        data = vrt.read([1], window=window)[0]
                        blank = bool(np.all((data == STATE_SUN) | (data == STATE_OUTSIDE)))
                        if blank and zoom > min_zoom:
                            skipped += 1
                            continue
                        writer.write_tile(
                            int(zxy_to_tileid(tile.z, tile.x, tile.y)), _encode_png(data, palette)
                        )
                        written += 1
            header = {
                "tile_type": TileType.PNG,
                # PNG is already compressed; a GZIP wrapper here would make
                # clients "decompress" bytes that are not further encoded.
                "tile_compression": Compression.NONE,
                "min_lon_e7": round(west * 1e7),
                "min_lat_e7": round(south * 1e7),
                "max_lon_e7": round(east * 1e7),
                "max_lat_e7": round(north * 1e7),
                "center_zoom": (min_zoom + max_zoom) // 2,
                "center_lon_e7": round((west + east) / 2 * 1e7),
                "center_lat_e7": round((south + north) / 2 * 1e7),
            }
            writer.finalize(header, metadata or {})
    return written, skipped


@dataclass(frozen=True)
class RenderJob:
    """One unit of rendering: an instant (two files) or a static set (one).

    Deliberately self-contained and picklable. Unlike the horizon sweep, whose
    workers inherit city-sized rasters through ``fork``, everything a tile
    worker needs fits in a message and it opens the artifacts itself -- so any
    start method works and nothing has to survive a fork after GDAL has run.
    """

    artifact_dir: Path
    tiles_dir: Path
    transform: Affine
    crs: str
    bounds: Bbox
    min_zoom: int
    max_zoom: int
    city_name: str
    attribution: tuple[str, ...]
    when: datetime | None = None
    sun: SunPosition | None = None
    static: str | None = None

    @property
    def label(self) -> str:
        return self.static if self.static is not None else f"{self.when:%Y%m%dT%H%M}"


@dataclass(frozen=True)
class FileSummary:
    """One finished pyramid, for the progress line and the totals."""

    filename: str
    written: int
    skipped: int
    size: int


@dataclass(frozen=True)
class RenderResult:
    """What a finished unit reports back: its files, its timings, its identity.

    Instants carry ``when``, ``sun`` and the bare filenames they wrote; the
    manifest entry itself is assembled by the parent, which is the one holding
    the cache-busting version and the legacy aliases. Static sets carry none of
    that.
    """

    label: str
    files: tuple[FileSummary, ...]
    elapsed_s: float
    state_s: float
    when: datetime | None = None
    sun: SunPosition | None = None
    urls: tuple[tuple[str, str], ...] = ()

    @property
    def written(self) -> int:
        return sum(summary.written for summary in self.files)

    @property
    def skipped(self) -> int:
        return sum(summary.skipped for summary in self.files)

    @property
    def size(self) -> int:
        return sum(summary.size for summary in self.files)


def render_unit(job: RenderJob) -> RenderResult:
    """Produce a unit's pyramids and report on them; the unit of work.

    The same function on both paths -- called directly in serial, submitted to
    the pool in parallel -- so the two cannot drift apart.
    """
    return _render_static(job) if job.static is not None else _render_instant(job)


def _write(
    job: RenderJob,
    filename: str,
    state: npt.NDArray[np.uint8],
    label: str,
    colors: Mapping[int, tuple[int, int, int, int]],
) -> FileSummary:
    written, skipped = write_instant_pmtiles(
        job.tiles_dir / filename,
        state,
        job.transform,
        job.crs,
        job.bounds,
        min_zoom=job.min_zoom,
        max_zoom=job.max_zoom,
        metadata={
            "name": f"{job.city_name} {label}",
            "attribution": " / ".join(job.attribution),
        },
        colors=colors,
    )
    return FileSummary(filename, written, skipped, (job.tiles_dir / filename).stat().st_size)


def _outside_the_area(directory: Path) -> npt.NDArray[np.bool_] | None:
    """Pixels the build never computed, or None when the city has no area.

    Every raster of a city covers the whole bbox, area or no area, so the
    layers have to hide what was never computed themselves. STATE_OUTSIDE is
    already transparent in all three palettes and already means "nothing to
    say here" (it is what roofs get), so an uncovered pixel needs no new state
    and no client change: it simply never paints.
    """
    path = directory / COVERAGE_FILENAME
    if not path.exists():
        return None
    with rasterio.open(path) as src:
        outside: npt.NDArray[np.bool_] = src.read(1) == 0
    return outside


def _render_static(job: RenderJob) -> RenderResult:
    """The two hour-independent sets: the crowns and the LiDAR footprint.

    No roof mask on the canopy: a crown overhanging a roof is still a tree.
    """
    start = time.monotonic()
    directory = Path(job.artifact_dir)
    outside = _outside_the_area(directory)
    if job.static == "canopy":
        with rasterio.open(directory / CANOPY_FILENAME) as src:
            mask = src.read()[0] != 0
        state = np.where(mask, STATE_SHADE_VEGETATION, STATE_SUN).astype(np.uint8)
        if outside is not None:
            state[outside] = STATE_OUTSIDE
        summary = _write(job, CANOPY_TILES_FILENAME, state, "tree canopy", CANOPY_COLORS)
    else:
        with rasterio.open(directory / LANDCOVER_FILENAME) as src:
            mask = src.read()[0] == Landcover.BUILDING
        state = np.where(mask, STATE_SHADE_BUILDING, STATE_SUN).astype(np.uint8)
        if outside is not None:
            state[outside] = STATE_OUTSIDE
        summary = _write(
            job, BUILDINGS_TILES_FILENAME, state, "buildings (lidar)", BUILDINGS_COLORS
        )
    return RenderResult(
        label=summary.filename,
        files=(summary,),
        elapsed_s=time.monotonic() - start,
        state_s=0.0,
    )


def _render_instant(job: RenderJob) -> RenderResult:
    """One instant: the state raster, then its two disjoint cast-shade sets."""
    assert job.when is not None and job.sun is not None  # the parent validated both
    start = time.monotonic()
    directory = Path(job.artifact_dir)
    state = compute_state_raster(directory, job.sun)
    state_s = time.monotonic() - start

    # Roof and canopy masks, applied AFTER compute_state_raster so that
    # function stays in pixel parity with is_shaded: both are presentation
    # only. Roofs become STATE_OUTSIDE (the warp nodata, alpha 0 in the
    # palette) rather than STATE_SUN, so a decoded tile still distinguishes
    # "roof" from "sunlit street".
    with rasterio.open(directory / LANDCOVER_FILENAME) as src:
        roof = src.read()[0] == Landcover.BUILDING
    with rasterio.open(directory / CANOPY_FILENAME) as src:
        canopy = src.read()[0] != 0
    state[roof] = STATE_OUTSIDE
    outside = _outside_the_area(directory)
    if outside is not None:
        # Before anything else reads it: outside the computation area the cubes
        # are zeros, and compute_state_raster has already read those zeros as an
        # open sky. Sunlit is precisely the wrong answer where there is no data.
        state[outside] = STATE_OUTSIDE
    # Under-canopy pixels move to the static canopy layer: the per-instant sets
    # keep only *cast* shade. Dropped pixels become STATE_SUN (transparent),
    # keeping STATE_OUTSIDE strictly for roofs and out-of-coverage pixels.
    # Under a crown that also sits in a building's shadow the state is BOTH,
    # which stays here: felling that tree would not put the pixel in the sun.
    state[canopy & (state == STATE_SHADE_VEGETATION)] = STATE_SUN
    del roof, canopy

    # Two disjoint cast-shade sets of the same color, cut by "would this hold
    # without the trees" and not by which obstacle won the argmax -- which is
    # why STATE_SHADE_BOTH goes in the building set. Hiding the trees set is
    # then literally the street without its trees, and hiding the buildings set
    # is the shade the trees *add*. Disjoint matters: both files paint at
    # OVERLAY_ALPHA, so an overlap would double-darken.
    instant_id = f"{job.when:%Y%m%dT%H%M}"
    building_state = state.copy()
    building_state[state == STATE_SHADE_VEGETATION] = STATE_SUN
    building_state[state == STATE_SHADE_BOTH] = STATE_SHADE_BUILDING
    trees_state = state.copy()
    trees_state[(state != STATE_SHADE_VEGETATION) & (state != STATE_OUTSIDE)] = STATE_SUN
    del state

    files = []
    urls = []
    for kind, layer_state in (("building", building_state), ("trees", trees_state)):
        filename = f"shade-{instant_id}-{kind}.pmtiles"
        files.append(
            _write(
                job, filename, layer_state, f"shade ({kind}) {job.when.isoformat()}", SHADE_COLORS
            )
        )
        urls.append((kind, filename))

    return RenderResult(
        label=instant_id,
        files=tuple(files),
        elapsed_s=time.monotonic() - start,
        state_s=state_s,
        when=job.when,
        sun=job.sun,
        urls=tuple(urls),
    )


def _render_parallel(jobs: list[RenderJob], workers: int) -> Generator[RenderResult]:
    """Yield finished units as they land, out of order.

    No start-method pinning, unlike the sweep: a job is a message, so there is
    nothing to inherit. A dead worker (usually the OOM killer) ends the render
    loudly rather than leaving a manifest that promises files nobody wrote.
    """
    executor = ProcessPoolExecutor(max_workers=workers)
    try:
        futures = [executor.submit(render_unit, job) for job in jobs]
        try:
            for future in as_completed(futures):
                yield future.result()
        except BrokenProcessPool as exc:
            raise RuntimeError(
                f"a tile render worker died (of {workers}); the usual cause is the "
                "OOM killer -- retry with fewer --workers"
            ) from exc
    finally:
        executor.shutdown(wait=True, cancel_futures=True)


def build_tiles(
    config: CityConfig,
    artifact_dir: str | Path,
    instants: Sequence[datetime],
    *,
    min_zoom: int = DEFAULT_MIN_ZOOM,
    max_zoom: int = DEFAULT_MAX_ZOOM,
    workers: int = 1,
    progress: Callable[[str], None] | None = None,
) -> Path:
    """Render two cast-shade PMTiles per instant, a static canopy set, and the manifest.

    The per-instant sets carry cast shadow only -- ``building`` (plus
    "other") and ``trees`` -- in one shared color; under-canopy pixels are
    excluded from both: they belong to the static *canopy* set, the crowns'
    vertical projection, written once per city because it is identical at
    every hour. Building interiors are masked transparent in the cast sets
    -- nobody stands on a roof, and the basemap already draws the buildings
    -- turning the overlay into street-level shade only.

    The manifest is what the web client consumes: available instants (with
    the naive local ``at`` string ready for the API's ``?at=`` parameter),
    relative tile URLs with a ``?v=`` epoch (cache busting against
    long-lived immutable caching), bounds, colors, the declination
    ``ladder`` (any calendar date -> its rung) and attribution. It stays
    ``schema_version`` 2 with additive fields: ``urls.{building,trees}``,
    ``canopy_url`` and ``ladder`` are the current contract, while the
    legacy ``url``/``urls.building`` (cast building shade, same semantics
    as the original split) and ``urls.vegetation`` (pointing at the static
    canopy) keep a deployed schema-2 client rendering sensibly without
    changes. Output lands under ``<artifact_dir>/tiles/``; the basemap
    referenced by ``basemap_url`` is produced out of band (see
    shade-docs: ops/anadir-ciudad.md).
    """
    echo = progress if progress is not None else lambda _message: None
    metadata = load_metadata(artifact_dir)
    transform = transform_from_bbox(metadata.bbox, metadata.resolution_m)
    west, south, east, north = bounds_wgs84(metadata.crs, metadata.bbox)
    center_lon = (west + east) / 2.0
    center_lat = (south + north) / 2.0

    ordered = sorted(instants)
    tiles_dir = Path(artifact_dir) / TILES_DIRNAME
    tiles_dir.mkdir(parents=True, exist_ok=True)
    common = {
        "artifact_dir": Path(artifact_dir),
        "tiles_dir": tiles_dir,
        "transform": transform,
        "crs": metadata.crs,
        "bounds": (west, south, east, north),
        "min_zoom": min_zoom,
        "max_zoom": max_zoom,
        "city_name": config.name,
        "attribution": tuple(metadata.attribution),
    }

    # Validate every instant in the parent, before a single worker exists: a
    # naive datetime or a night sun should cost nothing, not a phase.
    jobs = [RenderJob(**common, static=name) for name in ("canopy", "buildings")]
    for when in ordered:
        if when.tzinfo is None:
            raise ValueError(f"naive instant {when.isoformat()}; attach the city timezone")
        # One sun for the whole city: across an 8 km bbox the sun's position
        # varies by well under the horizon quantization step.
        sun = sun_position(center_lat, center_lon, when)
        if not sun.is_up:
            raise ValueError(
                f"{when.isoformat()}: sun elevation is {sun.elevation_deg:.1f} deg "
                "(night); pick a daylight instant"
            )
        jobs.append(RenderJob(**common, when=when, sun=sun))

    workers = max(1, workers)
    if workers > 1:
        rows, cols = grid_shape(metadata.bbox, metadata.resolution_m)
        # Before the pool exists, never after. Unlike the sweep this footprint
        # is fixed by the city, so the only lever left is fewer workers.
        check_worker_budget(workers, estimate_tiles_worker_bytes(rows, cols))

    build_start = time.monotonic()
    total_written = 0
    total_skipped = 0
    total_bytes = 0
    version = int(time.time())
    if workers > 1:
        echo(f"rendering {len(jobs)} units on {workers} workers")
    else:
        echo(
            f"rendering {len(jobs)} units serially "
            f"({cpu_budget()} cores available; --workers N to parallelise)"
        )

    results: list[RenderResult] = []
    producer: Generator[RenderResult] = (
        (render_unit(job) for job in jobs) if workers == 1 else _render_parallel(jobs, workers)
    )
    with closing(producer) as finished:
        for done, result in enumerate(finished, start=1):
            results.append(result)
            total_written += result.written
            total_skipped += result.skipped
            total_bytes += result.size
            for summary in result.files:
                echo(
                    f"[{done}/{len(jobs)}] {summary.filename}: {summary.written} tiles written, "
                    f"{summary.skipped} transparent skipped ({format_bytes(summary.size)})"
                )
            # Reported on completion, not on start: with N units in flight
            # there is no meaningful "current" one. The ETA is held back until
            # the first full batch has landed, because until then the elapsed
            # time covers units that have not finished.
            line = (
                f"[{done}/{len(jobs)}] {result.label} done in "
                f"{format_duration(result.elapsed_s)} "
                f"(state raster in {format_duration(result.state_s)})"
            )
            if done >= workers:
                average = (time.monotonic() - build_start) / done
                line += f", eta {format_duration(average * (len(jobs) - done))}"
            echo(line)

    canopy_url = f"{CANOPY_TILES_FILENAME}?v={version}"
    buildings_url = f"{BUILDINGS_TILES_FILENAME}?v={version}"

    # Chronological, not order of arrival: workers finish out of order and the
    # client reads this list as a timeline.
    rendered = [
        (item, item.when, item.sun)
        for item in results
        if item.when is not None and item.sun is not None
    ]
    rendered.sort(key=lambda triple: triple[1])
    entries: list[dict[str, object]] = []
    for item, moment, position in rendered:
        # urls.vegetation points a schema-2 client's toggle at the static
        # canopy, and url is the legacy alias of the building set. The ?v=
        # lands here because the parent is the one holding the version.
        urls = {"vegetation": canopy_url}
        urls.update({kind: f"{name}?v={version}" for kind, name in item.urls})
        offset = f"{moment:%z}"
        entries.append(
            {
                "id": item.label,
                "date": f"{moment:%Y-%m-%d}",
                "time": f"{moment:%H:%M}",
                "at": moment.replace(tzinfo=None).isoformat(timespec="minutes"),
                "utc_offset": f"{offset[:3]}:{offset[3:]}",
                "url": urls["building"],
                "urls": urls,
                "sun": {
                    "azimuth_deg": round(position.azimuth_deg, 2),
                    "elevation_deg": round(position.elevation_deg, 2),
                },
            }
        )

    manifest: dict[str, object] = {
        "schema_version": 2,
        "city": config.id,
        "name": config.name,
        "timezone": config.timezone,
        "bounds_wgs84": [round(value, 6) for value in (west, south, east, north)],
        "center_wgs84": [round(center_lon, 6), round(center_lat, 6)],
        "min_zoom": min_zoom,
        "max_zoom": max_zoom,
        "tile_size": TILE_SIZE,
        "colors": {
            "shade": _hex(SHADE_COLOR),
            "canopy": _hex(CANOPY_COLOR),
            "buildings": _hex(BUILDINGS_COLOR),
            # Legacy keys for schema-2 clients: building/other equal the
            # unified shade color; shade_vegetation is the color of the file
            # their vegetation toggle now points at (the static canopy).
            "shade_building": _hex(SHADE_COLORS[STATE_SHADE_BUILDING]),
            "shade_vegetation": _hex(CANOPY_COLOR),
            "shade_other": _hex(SHADE_COLORS[STATE_SHADE_OTHER]),
            "alpha": round(OVERLAY_ALPHA / 255.0, 2),
        },
        "canopy_url": canopy_url,
        "buildings_url": buildings_url,
        # Additive, and absent for a city without an area. bounds_wgs84 is the
        # rectangle the rasters cover; this is the part of it that was actually
        # computed, which is what a client should veil.
        **(
            {"coverage": wgs84_geometry(read_area(Path(config.area), config.crs))}
            if config.area is not None
            else {}
        ),
        "ladder": declination_ladder(),
        "basemap_url": BASEMAP_FILENAME,
        "instants": entries,
        "generated_at": datetime.now(tz=UTC).isoformat(timespec="seconds"),
        "attribution": metadata.attribution,
    }
    (tiles_dir / MANIFEST_FILENAME).write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    echo(
        f"tiles done in {format_duration(time.monotonic() - build_start)} "
        f"({len(ordered)} instants, {2 * len(ordered) + 2} pmtiles, "
        f"{format_bytes(total_bytes)}, {total_written} tiles written, "
        f"{total_skipped} skipped)"
    )
    return tiles_dir


def _hex(color: tuple[int, int, int, int]) -> str:
    red, green, blue, _alpha = color
    return f"#{red:02x}{green:02x}{blue:02x}"
