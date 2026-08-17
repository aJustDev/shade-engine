"""Per-instant shade overlays as raster PMTiles (see shade-docs: learning/map-tiles-pmtiles.md).

Web maps consume square 256 px tiles addressed by z/x/y in Web Mercator
(EPSG:3857): at zoom z the world is a 2^z x 2^z grid, x grows east and y
grows *south*. PMTiles packs a whole tile pyramid into one static file with
a Hilbert-ordered directory at the front, so a browser fetches any tile
with plain HTTP range requests -- the COG trick applied to web pyramids,
and the reason serving these needs no tile server, only Caddy.

Zoom bounds: Web Mercator inflates distances by 1/cos(lat) (see
shade-docs: learning/web-mercator.md), so at Cordoba's latitude (37.9 N) zoom 17
is 156543/2^17 * cos(37.9) = 0.94 m/px -- our native 1 m resolution -- and zoom
12 (~30 m/px) fits the whole city on two tiles. The pyramid nevertheless goes
to zoom 19, because a tile is no longer a resampled verdict: the horizon margin
travels to the tile grid as a continuous field and the sign is taken there (see
:func:`write_pyramid`), so the levels past the raster carry sub-pixel boundary
position rather than magnified texels. That is what stops a shadow edge from
being a staircase of whole metres, and it is the only reason to render them.

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
from contextlib import ExitStack, closing, contextmanager
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

from shade_core.artifacts import CANOPY_FILENAME, LANDCOVER_FILENAME, load_coverage, load_metadata
from shade_core.config import Bbox, CityConfig
from shade_core.shade import Landcover
from shade_core.solar import SunPosition, sun_position
from shade_pipeline.area import read_area, wgs84_geometry
from shade_pipeline.budget import (
    check_worker_budget,
    cpu_budget,
    estimate_tiles_worker_bytes,
    warn_if_serial_is_tight,
)
from shade_pipeline.events import EventSink, emit
from shade_pipeline.grid import grid_shape, transform_from_bbox
from shade_pipeline.progress import format_bytes, format_duration
from shade_pipeline.shade_raster import (
    STATE_OUTSIDE,
    STATE_SHADE_BOTH,
    STATE_SHADE_BUILDING,
    STATE_SHADE_OTHER,
    STATE_SHADE_VEGETATION,
    STATE_SUN,
    compose_state,
    read_shade_fields,
    signed_distance,
)

DEFAULT_MIN_ZOOM: Final = 12
DEFAULT_MAX_ZOOM: Final = 19
"""Deepest zoom rendered: ~0.23 m/px at lat 37.9, four times the raster.

It used to be 17, because 17 is the native 1 m and anything past it could only
repeat pixels. Since the verdict is thresholded on the tile grid rather than at
1 m (see :func:`write_pyramid`), the deeper levels carry real sub-pixel
position instead of magnified texels, which is where a shadow edge stops being
a staircase. Two levels cost about 4x the pyramid on disk -- measured, not
16x, because the added tiles are mostly flat and compress to nothing.
"""
TILE_SIZE: Final = 256
GDAL_CACHE_MB: Final = 512
"""Block cache one render worker is allowed, in MiB (GDAL's default is 5% of RAM).

Pinned so the phase's memory is a number instead of a fraction of whatever
machine is running -- which is what lets :mod:`shade_pipeline.budget` price a
worker at all.

512 MiB because that is what GDAL's own default came to on the machine this
was developed on, so pinning it changes nothing that was ever measured while
making the number independent of the host.

A smaller cap was tried and looked much slower, but those timings were taken
against a moving background (competing renders, and a laptop that spent part
of the afternoon throttled on battery), so the honest claim is only this: 128
MiB was not shown to be safe, and 512 MiB reproduces the behaviour every other
measurement here was made under. Retiming it on a quiet machine is cheap if
the phase ever needs the memory back.
"""

TileState = npt.NDArray[np.uint8]
"""One tile's state codes, ``TILE_SIZE`` square."""
TILES_DIRNAME: Final = "tiles"
MANIFEST_FILENAME: Final = "index.json"
BASEMAP_FILENAME: Final = "basemap.pmtiles"
PARTIAL_SUFFIX: Final = ".partial"
"""Appended while an archive is being written; removed by the rename that ends it."""
RENDER_STATE_FILENAME: Final = "render.json"
"""What the pyramids in this directory were rendered from, for :func:`build_tiles`.

Existence of a ``.pmtiles`` says it finished; this says it finished *from the
artifacts and zoom range being asked for now*. Without it a resumed render
would happily keep pyramids built at another ``--max-zoom``, or from the
rasters of a previous build of the same city."""

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
CAST_KINDS: Final = ("building", "trees")
"""The two disjoint cast-shade sets every instant writes, in filename order."""

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


@contextmanager
def _as_dataset(
    bands: npt.NDArray[np.floating] | npt.NDArray[np.uint8],
    transform: Affine,
    crs: str,
    nodata: float | None,
) -> Generator[rasterio.DatasetReader]:
    """A (bands, rows, cols) stack as an in-memory GTiff, open for reading.

    In RAM and not on scratch, which was measured rather than assumed: the same
    instant of ``cordoba-test`` renders in 68 s from a ``MemoryFile`` and in
    137 s from a temporary GTiff. The warper re-reads blocks as it walks
    neighbouring tiles, and paying the file system for that doubles the phase.
    The cost is one extra copy of the stack while the pyramid is written, which
    :mod:`shade_pipeline.budget` prices.
    """
    count, rows, cols = bands.shape
    with MemoryFile() as memory:
        with memory.open(
            driver="GTiff",
            width=cols,
            height=rows,
            count=count,
            dtype=bands.dtype.name,
            crs=crs,
            transform=transform,
            nodata=nodata,
        ) as dataset:
            dataset.write(bands)
        with memory.open() as source:
            yield source


@dataclass(frozen=True)
class PyramidOutput:
    """One file a warp pass produces: where it goes and how a tile is made."""

    path: Path
    compose: Callable[[npt.NDArray[np.float32] | None, npt.NDArray[np.uint8] | None], TileState]
    colors: Mapping[int, tuple[int, int, int, int]]
    metadata: dict[str, object] | None = None


def write_pyramids(
    outputs: Sequence[PyramidOutput],
    *,
    continuous: npt.NDArray[np.float32] | None,
    categorical: npt.NDArray[np.uint8] | None,
    categorical_nodata: float | None = None,
    transform: Affine,
    crs: str,
    bounds: Bbox,
    min_zoom: int = DEFAULT_MIN_ZOOM,
    max_zoom: int = DEFAULT_MAX_ZOOM,
) -> list[tuple[int, int]]:
    """Warp the sources once and write every output from it; (written, skipped) each.

    Zooms are walked ascending and tiles within a zoom in Hilbert (tileid)
    order, so each writer stays clustered. Per zoom one ``WarpedVRT`` per source
    reprojects onto a Web Mercator grid anchored on the tile lattice, and every
    tile is a plain 256 px window read.

    **The verdict is composed per tile, not before.** ``continuous`` carries
    fields whose sign is the answer (the horizon margin, the signed distance of
    a mask); they are resampled and only then compared against zero by each
    output's ``compose``, so the boundary lands at a sub-pixel position and its
    corners come out rounded instead of stepping in whole metres.
    ``categorical`` carries labels, which have no meaningful average and travel
    nearest.

    Resampling of the continuous stack follows the direction of the zoom:
    ``bilinear`` where a zoom is finer than the raster (the case this exists
    for) and ``average`` where it is coarser, because a 2x2 bilinear probe of a
    downsample aliases thin shadows that an area mean keeps.

    **Several outputs share one pass on purpose.** An instant's two cast-shade
    sets differ only in how the same composed state is folded, and warping is
    what the phase costs -- rendering them separately would resample every tile
    of the city twice for nothing.

    Fully transparent tiles (all sun or outside) are skipped above
    ``min_zoom``: an absent tile renders as nothing, and most of a pyramid
    is sun. At ``min_zoom`` tiles are always written -- the writer cannot
    finalize an empty archive, and deduplication stores the shared blank
    PNG only once.
    """
    west, south, east, north = bounds
    palettes = [palette_bytes(output.colors) for output in outputs]
    counts = [[0, 0] for _ in outputs]
    source_resolution = abs(transform.a)
    # GDAL's block cache defaults to 5% of physical RAM *per process*, which on
    # a render pool is several GiB of budget nobody asked for. Capped here
    # because the access pattern earns almost nothing from it: each zoom walks
    # its tiles once, so a block is read and never wanted again.
    stack_env = rasterio.Env(GDAL_CACHEMAX=GDAL_CACHE_MB)
    ground_scale = math.cos(math.radians((south + north) / 2.0))
    with ExitStack() as stack:
        stack.enter_context(stack_env)
        sources: list[tuple[rasterio.DatasetReader, bool, float | None]] = []
        if continuous is not None:
            sources.append(
                (
                    stack.enter_context(_as_dataset(continuous, transform, crs, float("nan"))),
                    True,
                    float("nan"),
                )
            )
        if categorical is not None:
            # Nodata is the caller's call, and the two paths need opposite
            # answers: a lone state raster wants STATE_OUTSIDE beyond its edge,
            # while the render path must NOT mask 255, which is a real blocker
            # class (NO_BLOCKER). There the continuous NaN marks what is off
            # the raster, so the fill value never gets read.
            sources.append(
                (
                    stack.enter_context(
                        _as_dataset(categorical, transform, crs, categorical_nodata)
                    ),
                    False,
                    categorical_nodata,
                )
            )
        # Written under a scratch name and renamed once finalized, so the real
        # filename only ever exists on a complete archive. A PMTiles writer
        # lays its directory down in `finalize`, which means an interrupted
        # render used to leave a plausible-looking file (often 0 bytes) exactly
        # where a finished one belongs -- and nothing downstream could tell
        # them apart. This is what makes resuming a render a question of
        # whether the path exists.
        partials = [output.path.with_name(output.path.name + PARTIAL_SUFFIX) for output in outputs]
        writers = [Writer(stack.enter_context(open(path, "wb"))) for path in partials]
        for zoom in range(min_zoom, max_zoom + 1):
            tiles = sorted(
                mercantile.tiles(west, south, east, north, [zoom]),
                key=lambda t: int(zxy_to_tileid(t.z, t.x, t.y)),
            )
            x0 = min(t.x for t in tiles)
            y0 = min(t.y for t in tiles)
            resolution = _WEB_MERCATOR_CIRCUMFERENCE / (2**zoom * TILE_SIZE)
            upsampling = resolution * ground_scale <= source_resolution
            corner = mercantile.xy_bounds(x0, y0, zoom)
            grid = {
                "crs": "EPSG:3857",
                "transform": from_origin(corner.left, corner.top, resolution, resolution),
                "width": (max(t.x for t in tiles) - x0 + 1) * TILE_SIZE,
                "height": (max(t.y for t in tiles) - y0 + 1) * TILE_SIZE,
            }
            with ExitStack() as warps:
                vrts = [
                    warps.enter_context(
                        WarpedVRT(
                            source,
                            resampling=(
                                (Resampling.bilinear if upsampling else Resampling.average)
                                if smooth
                                else Resampling.nearest
                            ),
                            nodata=nodata,
                            **grid,
                        )
                    )
                    for source, smooth, nodata in sources
                ]
                fields = vrts[0] if continuous is not None else None
                labels = vrts[-1] if categorical is not None else None
                for tile in tiles:
                    window = Window(
                        (tile.x - x0) * TILE_SIZE,
                        (tile.y - y0) * TILE_SIZE,
                        TILE_SIZE,
                        TILE_SIZE,
                    )
                    read_fields = None if fields is None else fields.read(window=window)
                    read_labels = None if labels is None else labels.read(window=window)
                    tileid = int(zxy_to_tileid(tile.z, tile.x, tile.y))
                    for index, output in enumerate(outputs):
                        data = output.compose(read_fields, read_labels)
                        blank = bool(np.all((data == STATE_SUN) | (data == STATE_OUTSIDE)))
                        if blank and zoom > min_zoom:
                            counts[index][1] += 1
                            continue
                        writers[index].write_tile(tileid, _encode_png(data, palettes[index]))
                        counts[index][0] += 1
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
        for writer, output in zip(writers, outputs, strict=True):
            writer.finalize(header, output.metadata or {})
    # Outside the ExitStack, so every handle is closed before the rename: on a
    # single filesystem `replace` is atomic, and a reader either sees the old
    # archive or the new one, never a half-written directory.
    for partial, output in zip(partials, outputs, strict=True):
        partial.replace(output.path)
    return [(written, skipped) for written, skipped in counts]


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
    """Pyramid from a finished state raster: categorical, nearest, no threshold.

    The plain case, for callers that already hold the verdict. The render path
    does not take it -- it hands :func:`write_pyramid` the continuous fields
    precisely so the verdict is not fixed at 1 m.
    """

    def take_state(
        _fields: npt.NDArray[np.float32] | None, labels: npt.NDArray[np.uint8] | None
    ) -> TileState:
        assert labels is not None
        band: TileState = labels[0]
        return band

    (result,) = write_pyramids(
        [PyramidOutput(path=path, compose=take_state, colors=colors, metadata=metadata)],
        continuous=None,
        categorical=state[np.newaxis, ...],
        categorical_nodata=float(STATE_OUTSIDE),
        transform=transform,
        crs=crs,
        bounds=bounds,
        min_zoom=min_zoom,
        max_zoom=max_zoom,
    )
    return result


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

    @property
    def filenames(self) -> tuple[str, ...]:
        """The archives this unit is responsible for.

        Derived from the job rather than reported by the render, so the parent
        can ask whether a unit is already on disk without opening a raster --
        and so the two answers cannot drift apart, because the render names its
        outputs from here too.
        """
        if self.static is not None:
            return (CANOPY_TILES_FILENAME if self.static == "canopy" else BUILDINGS_TILES_FILENAME,)
        return tuple(f"shade-{self.label}-{kind}.pmtiles" for kind in CAST_KINDS)

    def is_rendered(self) -> bool:
        """True when every archive of this unit exists under its final name."""
        return all((self.tiles_dir / name).exists() for name in self.filenames)


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
    reused: bool = False
    """True when the archives were already on disk and this run did not touch them.

    Such a unit still has to appear in ``results``: the manifest is assembled
    from them, so a resumed render that dropped them would publish a timeline
    missing every instant it did not personally write."""

    @property
    def written(self) -> int:
        return sum(summary.written for summary in self.files)

    @property
    def skipped(self) -> int:
        return sum(summary.skipped for summary in self.files)

    @property
    def size(self) -> int:
        return sum(summary.size for summary in self.files)


def _reused_result(job: RenderJob) -> RenderResult:
    """The report a unit would have made, read off the archives already there.

    Written and skipped tile counts come back as zero because this run wrote
    none; the totals line reports reuse separately rather than inventing
    numbers it cannot know.
    """
    names = job.filenames
    files = tuple(FileSummary(name, 0, 0, (job.tiles_dir / name).stat().st_size) for name in names)
    return RenderResult(
        # Statics label themselves by filename and instants by id; matched here
        # so a resumed run's progress lines read like an ordinary one's.
        label=names[0] if job.static is not None else job.label,
        files=files,
        elapsed_s=0.0,
        state_s=0.0,
        when=job.when,
        sun=job.sun,
        urls=() if job.static is not None else tuple(zip(CAST_KINDS, names, strict=True)),
        reused=True,
    )


def render_state(min_zoom: int, max_zoom: int, built_at: datetime) -> dict[str, object]:
    """What a pyramid directory was rendered from; compared verbatim on resume.

    Deliberately about *inputs*, not outputs: the build timestamp changes
    whenever the city's rasters are rebuilt, and the zoom range changes what
    every archive contains. It does not fingerprint the pipeline's own code, so
    ``--resume`` after editing the render is the caller's judgement call --
    which is why resuming is opt-in and not the default.
    """
    return {
        "artifact_built_at": built_at.isoformat(),
        "min_zoom": min_zoom,
        "max_zoom": max_zoom,
    }


def read_render_state(tiles_dir: Path) -> dict[str, object] | None:
    """The recorded render state of a tiles directory, or None if absent/unreadable."""
    path = tiles_dir / RENDER_STATE_FILENAME
    try:
        loaded: dict[str, object] = json.loads(path.read_text(encoding="utf-8"))
    except OSError, json.JSONDecodeError:
        return None
    return loaded


def render_unit(job: RenderJob) -> RenderResult:
    """Produce a unit's pyramids and report on them; the unit of work.

    The same function on both paths -- called directly in serial, submitted to
    the pool in parallel -- so the two cannot drift apart.
    """
    return _render_static(job) if job.static is not None else _render_instant(job)


def _write(
    job: RenderJob,
    plans: Sequence[
        tuple[
            str,
            Callable[[npt.NDArray[np.float32] | None, npt.NDArray[np.uint8] | None], TileState],
            str,
            Mapping[int, tuple[int, int, int, int]],
        ]
    ],
    *,
    continuous: npt.NDArray[np.float32],
    categorical: npt.NDArray[np.uint8],
) -> list[FileSummary]:
    """Write every plan (filename, compose, label, colors) in a single warp pass."""
    results = write_pyramids(
        [
            PyramidOutput(
                path=job.tiles_dir / filename,
                compose=compose,
                colors=colors,
                metadata={
                    "name": f"{job.city_name} {label}",
                    "attribution": " / ".join(job.attribution),
                },
            )
            for filename, compose, label, colors in plans
        ],
        continuous=continuous,
        categorical=categorical,
        transform=job.transform,
        crs=job.crs,
        bounds=job.bounds,
        min_zoom=job.min_zoom,
        max_zoom=job.max_zoom,
    )
    return [
        FileSummary(filename, written, skipped, (job.tiles_dir / filename).stat().st_size)
        for (filename, _compose, _label, _colors), (written, skipped) in zip(
            plans, results, strict=True
        )
    ]


def _uncovered_band(directory: Path, shape: tuple[int, ...]) -> npt.NDArray[np.uint8]:
    """1 where the build never computed anything, 0 elsewhere; all zeros if no area.

    Every raster of a city covers the whole bbox, area or no area, so the
    layers have to hide what was never computed themselves. STATE_OUTSIDE is
    already transparent in all three palettes and already means "nothing to
    say here" (it is what roofs get), so an uncovered pixel needs no new state
    and no client change: it simply never paints.

    It travels as a categorical band rather than as a mask applied up front
    because the state is now composed per tile, and this is one of its inputs.
    """
    covered = load_coverage(directory)
    if covered is None:
        return np.zeros(shape, dtype=np.uint8)
    return (~covered).astype(np.uint8)


def _mask_compose(
    present: int,
) -> Callable[[npt.NDArray[np.float32] | None, npt.NDArray[np.uint8] | None], TileState]:
    """Tile from one signed-distance band: inside the mask gets ``present``.

    The threshold is the whole point of doing it here: a mask resampled as a
    label steps in whole source pixels, while the zero crossing of its
    resampled distance lands between them and rounds the corners.
    """

    def compose(
        fields: npt.NDArray[np.float32] | None, labels: npt.NDArray[np.uint8] | None
    ) -> TileState:
        assert fields is not None and labels is not None
        distance = fields[0]
        state = np.where(distance > 0.0, np.uint8(present), np.uint8(STATE_SUN)).astype(np.uint8)
        state[np.isnan(distance) | (labels[0] != 0)] = STATE_OUTSIDE
        return state

    return compose


def _render_static(job: RenderJob) -> RenderResult:
    """The two hour-independent sets: the crowns and the LiDAR footprint.

    No roof mask on the canopy: a crown overhanging a roof is still a tree.
    """
    start = time.monotonic()
    directory = Path(job.artifact_dir)
    if job.static == "canopy":
        with rasterio.open(directory / CANOPY_FILENAME) as src:
            mask = src.read()[0] != 0
        present, label, colors = (STATE_SHADE_VEGETATION, "tree canopy", CANOPY_COLORS)
    else:
        with rasterio.open(directory / LANDCOVER_FILENAME) as src:
            mask = src.read()[0] == Landcover.BUILDING
        present, label, colors = (STATE_SHADE_BUILDING, "buildings (lidar)", BUILDINGS_COLORS)
    (filename,) = job.filenames
    (summary,) = _write(
        job,
        [(filename, _mask_compose(present), label, colors)],
        continuous=signed_distance(mask)[np.newaxis, ...],
        categorical=_uncovered_band(directory, mask.shape)[np.newaxis, ...],
    )
    return RenderResult(
        label=summary.filename,
        files=(summary,),
        elapsed_s=time.monotonic() - start,
        state_s=0.0,
    )


def _cast_shade_compose(
    kind: str,
) -> Callable[[npt.NDArray[np.float32] | None, npt.NDArray[np.uint8] | None], TileState]:
    """One tile of a cast-shade set, thresholded on the tile's own grid.

    Two disjoint sets of the same color, cut by "would this hold without the
    trees" and not by which obstacle won the argmax -- which is why
    ``STATE_SHADE_BOTH`` goes in the building set. Hiding the trees set is then
    literally the street without its trees, and hiding the buildings set is the
    shade the trees *add*. Disjoint matters: both files paint at
    ``OVERLAY_ALPHA``, so an overlap would double-darken.
    """

    def compose(
        fields: npt.NDArray[np.float32] | None, labels: npt.NDArray[np.uint8] | None
    ) -> TileState:
        assert fields is not None and labels is not None
        margin, margin_noveg, roof_distance, canopy_distance = fields
        blocker, uncovered = labels
        under_canopy = canopy_distance > 0.0
        # NaN marks what the warp filled beyond the raster: sunlit is precisely
        # the wrong answer where there is no data, and neither is roof.
        state = compose_state(
            shaded=margin > 0.0,
            holds=margin_noveg > 0.0,
            blocker=blocker,
            under_canopy=under_canopy,
            roof=roof_distance > 0.0,
            outside=np.isnan(margin) | (uncovered != 0),
        )
        # Under-canopy pixels move to the static canopy layer: the per-instant
        # sets keep only *cast* shade. Dropped pixels become STATE_SUN,
        # keeping STATE_OUTSIDE strictly for roofs and uncovered pixels. Under
        # a crown that also sits in a building's shadow the state is BOTH,
        # which stays: felling that tree would not put the pixel in the sun.
        state[under_canopy & (state == STATE_SHADE_VEGETATION)] = STATE_SUN
        if kind == "building":
            state[state == STATE_SHADE_VEGETATION] = STATE_SUN
            state[state == STATE_SHADE_BOTH] = STATE_SHADE_BUILDING
        else:
            state[(state != STATE_SHADE_VEGETATION) & (state != STATE_OUTSIDE)] = STATE_SUN
        return state

    return compose


def _render_instant(job: RenderJob) -> RenderResult:
    """One instant: its continuous fields, then its two disjoint cast-shade sets.

    Nothing here decides sol/sombra. The fields go to the pyramid as they are
    and the comparison happens per tile, which is what puts the shadow edge at
    a sub-pixel position instead of on the 1 m lattice.
    """
    assert job.when is not None and job.sun is not None  # the parent validated both
    start = time.monotonic()
    directory = Path(job.artifact_dir)
    fields = read_shade_fields(directory, job.sun)
    continuous = np.stack(
        [fields.margin, fields.margin_noveg, fields.roof_distance, fields.canopy_distance]
    )
    categorical = np.stack([fields.blocker, _uncovered_band(directory, fields.blocker.shape)])
    del fields
    state_s = time.monotonic() - start

    names = job.filenames
    # Both sets in one pass: they fold the same composed state differently, and
    # warping the city twice to do that is the whole cost of the phase.
    files = _write(
        job,
        [
            (
                name,
                _cast_shade_compose(kind),
                f"shade ({kind}) {job.when.isoformat()}",
                SHADE_COLORS,
            )
            for kind, name in zip(CAST_KINDS, names, strict=True)
        ],
        continuous=continuous,
        categorical=categorical,
    )
    urls = list(zip(CAST_KINDS, names, strict=True))

    return RenderResult(
        label=job.label,
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
    resume: bool = False,
    progress: Callable[[str], None] | None = None,
    events: EventSink | None = None,
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
    changes. Output lands under ``<artifact_dir>/tiles/``; the basemap is cut
    by its own chain step (:mod:`shade_pipeline.basemap`) and ``basemap_url``
    appears only when that file is actually on disk.

    ``resume`` skips units whose archives are already on disk, and only when
    ``render.json`` says they came from these artifacts and this zoom range.
    It is off by default because that file tracks the *inputs* and not the
    pipeline code that read them: continuing an interrupted render is exactly
    what it is for, re-running after editing the renderer is not. A render of
    83 instants is six hours, and before this the nineteenth failure threw away
    the eighteen units that had succeeded.
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

    # What this render is about to produce, and what a later one compares
    # against to decide whether anything on disk can be trusted.
    state = render_state(min_zoom, max_zoom, metadata.built_at)
    pending = jobs
    reused: list[RenderResult] = []
    if resume:
        recorded = read_render_state(tiles_dir)
        if recorded == state:
            pending = [job for job in jobs if not job.is_rendered()]
            reused = [_reused_result(job) for job in jobs if job.is_rendered()]
        elif recorded is None:
            echo("resume: no render.json in the tiles directory; rendering every unit")
        else:
            echo(
                "resume: the tiles on disk came from other inputs "
                "(rebuilt artifacts or another zoom range); rendering every unit"
            )
    if reused:
        echo(f"resume: {len(reused)} of {len(jobs)} units already rendered")
        emit(events, "tiles", "resumed", reused=len(reused), total=len(jobs))
    # Written now rather than on success, and that is the whole point: an
    # interrupted render has to leave behind what it was rendering, or the
    # resume it exists for has nothing to compare against.
    (tiles_dir / RENDER_STATE_FILENAME).write_text(
        json.dumps(state, indent=2) + "\n", encoding="utf-8"
    )

    workers = max(1, workers)
    rows, cols = grid_shape(metadata.bbox, metadata.resolution_m)
    per_worker = estimate_tiles_worker_bytes(rows, cols)
    if workers > 1:
        # Before the pool exists, never after. Unlike the sweep this footprint
        # is fixed by the city, so the only lever left is fewer workers.
        check_worker_budget(workers, per_worker)
    else:
        # ...and when there is no lever left, say so anyway. A metropolitan
        # bbox at 1 m/px puts tens of GiB in one instant, and without this the
        # serial path walks into the OOM killer in silence.
        warn_if_serial_is_tight(per_worker, progress)

    build_start = time.monotonic()
    total_written = 0
    total_skipped = 0
    total_bytes = sum(result.size for result in reused)
    version = int(time.time())
    if not pending:
        echo("nothing to render; rewriting the manifest from the tiles already there")
    elif workers > 1:
        echo(f"rendering {len(pending)} units on {workers} workers")
    else:
        echo(
            f"rendering {len(pending)} units serially "
            f"({cpu_budget()} cores available; --workers N to parallelise)"
        )
    emit(events, "tiles", "started", units=len(pending), reused=len(reused), workers=workers)

    results: list[RenderResult] = list(reused)
    producer: Generator[RenderResult] = (
        (render_unit(job) for job in pending)
        if workers == 1
        else _render_parallel(pending, workers)
    )
    with closing(producer) as finished:
        for done, result in enumerate(finished, start=1):
            results.append(result)
            total_written += result.written
            total_skipped += result.skipped
            total_bytes += result.size
            for summary in result.files:
                echo(
                    f"[{done}/{len(pending)}] {summary.filename}: {summary.written} tiles written, "
                    f"{summary.skipped} transparent skipped ({format_bytes(summary.size)})"
                )
            # Reported on completion, not on start: with N units in flight
            # there is no meaningful "current" one. The ETA is held back until
            # the first full batch has landed, because until then the elapsed
            # time covers units that have not finished.
            line = (
                f"[{done}/{len(pending)}] {result.label} done in "
                f"{format_duration(result.elapsed_s)} "
                f"(state raster in {format_duration(result.state_s)})"
            )
            eta_s = None
            if done >= workers:
                average = (time.monotonic() - build_start) / done
                eta_s = average * (len(pending) - done)
                line += f", eta {format_duration(eta_s)}"
            echo(line)
            emit(
                events,
                "tiles",
                "unit",
                label=result.label,
                done=done,
                total=len(pending),
                elapsed_s=round(result.elapsed_s, 1),
                eta_s=None if eta_s is None else round(eta_s, 1),
            )

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
        # Present exactly when the file is. The key is a promise the client
        # believes: given it, the viewer declares a PMTiles source and, when
        # that source 404s, draws black rather than falling back to OSM. Written
        # unconditionally, it turned a missing basemap into an unreadable map
        # instead of a plainer one.
        **({"basemap_url": BASEMAP_FILENAME} if (tiles_dir / BASEMAP_FILENAME).exists() else {}),
        "instants": entries,
        "generated_at": datetime.now(tz=UTC).isoformat(timespec="seconds"),
        "attribution": metadata.attribution,
    }
    (tiles_dir / MANIFEST_FILENAME).write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    elapsed = time.monotonic() - build_start
    echo(
        f"tiles done in {format_duration(elapsed)} "
        f"({len(ordered)} instants, {2 * len(ordered) + 2} pmtiles, "
        f"{format_bytes(total_bytes)}, {total_written} tiles written, "
        f"{total_skipped} skipped" + (f", {len(reused)} units reused" if reused else "") + ")"
    )
    emit(
        events,
        "tiles",
        "finished",
        instants=len(ordered),
        units=len(pending),
        reused=len(reused),
        bytes=total_bytes,
        elapsed_s=round(elapsed, 1),
        directory=str(tiles_dir),
    )
    return tiles_dir


def _hex(color: tuple[int, int, int, int]) -> str:
    red, green, blue, _alpha = color
    return f"#{red:02x}{green:02x}{blue:02x}"
