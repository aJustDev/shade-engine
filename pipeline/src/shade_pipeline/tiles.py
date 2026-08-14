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
from collections.abc import Callable, Mapping, Sequence
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

from shade_core.artifacts import CANOPY_FILENAME, LANDCOVER_FILENAME, load_metadata
from shade_core.config import Bbox, CityConfig
from shade_core.shade import Landcover
from shade_core.solar import sun_position
from shade_pipeline.grid import transform_from_bbox
from shade_pipeline.progress import format_bytes, format_duration
from shade_pipeline.shade_raster import (
    STATE_OUTSIDE,
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
_PALETTE_STATES: Final = (
    STATE_SUN,
    STATE_SHADE_BUILDING,
    STATE_SHADE_VEGETATION,
    STATE_SHADE_OTHER,
    STATE_OUTSIDE,
)


def _palette_bytes(colors: Mapping[int, tuple[int, int, int, int]]) -> tuple[bytes, bytes]:
    """(RGB palette, per-index alpha) PNG chunks for a state->RGBA mapping."""
    rgb = bytes(channel for state in _PALETTE_STATES for channel in colors[state][:3])
    trns = bytes(colors[state][3] for state in _PALETTE_STATES)
    return rgb, trns


def _palette_index() -> npt.NDArray[np.uint8]:
    index = np.zeros(256, dtype=np.uint8)
    for position, state in enumerate(_PALETTE_STATES):
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
    palette = _palette_bytes(colors)
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


def build_tiles(
    config: CityConfig,
    artifact_dir: str | Path,
    instants: Sequence[datetime],
    *,
    min_zoom: int = DEFAULT_MIN_ZOOM,
    max_zoom: int = DEFAULT_MAX_ZOOM,
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
    for when in ordered:
        if when.tzinfo is None:
            raise ValueError(f"naive instant {when.isoformat()}; attach the city timezone")

    # Roof and canopy masks, read once (instant-invariant). Applied AFTER
    # compute_state_raster so that function stays in pixel parity with
    # is_shaded: both are presentation only. Roofs become STATE_OUTSIDE
    # (the warp nodata, alpha 0 in the palette) rather than STATE_SUN, so a
    # decoded tile still distinguishes "roof" from "sunlit street".
    with rasterio.open(Path(artifact_dir) / LANDCOVER_FILENAME) as src:
        roof = src.read()[0] == Landcover.BUILDING
    with rasterio.open(Path(artifact_dir) / CANOPY_FILENAME) as src:
        canopy = src.read()[0] != 0

    tiles_dir = Path(artifact_dir) / TILES_DIRNAME
    tiles_dir.mkdir(parents=True, exist_ok=True)
    build_start = time.monotonic()
    total_written = 0
    total_skipped = 0
    total_bytes = 0
    version = int(time.time())

    # Static (hour-independent) sets, one file per city each: the crowns'
    # vertical projection and the LiDAR building footprint (the same mask
    # the shade sets punch out as roofs -- the layers tile together). No
    # roof mask on the canopy: a crown overhanging a roof is still a tree.
    canopy_state = np.where(canopy, STATE_SHADE_VEGETATION, STATE_SUN).astype(np.uint8)
    buildings_state = np.where(roof, STATE_SHADE_BUILDING, STATE_SUN).astype(np.uint8)
    static_sets = (
        (CANOPY_TILES_FILENAME, "tree canopy", canopy_state, CANOPY_COLORS),
        (BUILDINGS_TILES_FILENAME, "buildings (lidar)", buildings_state, BUILDINGS_COLORS),
    )
    for filename, label, static_state, colors in static_sets:
        phase_start = time.monotonic()
        written, skipped = write_instant_pmtiles(
            tiles_dir / filename,
            static_state,
            transform,
            metadata.crs,
            (west, south, east, north),
            min_zoom=min_zoom,
            max_zoom=max_zoom,
            metadata={
                "name": f"{config.name} {label}",
                "attribution": " / ".join(metadata.attribution),
            },
            colors=colors,
        )
        size = (tiles_dir / filename).stat().st_size
        total_written += written
        total_skipped += skipped
        total_bytes += size
        echo(
            f"{filename}: {written} tiles written, {skipped} transparent "
            f"skipped ({format_bytes(size)}, "
            f"{format_duration(time.monotonic() - phase_start)})"
        )
    canopy_url = f"{CANOPY_TILES_FILENAME}?v={version}"
    buildings_url = f"{BUILDINGS_TILES_FILENAME}?v={version}"

    entries: list[dict[str, object]] = []
    for index, when in enumerate(ordered, start=1):
        # One sun for the whole city: across an 8 km bbox the sun's position
        # varies by well under the horizon quantization step.
        sun = sun_position(center_lat, center_lon, when)
        if not sun.is_up:
            raise ValueError(
                f"{when.isoformat()}: sun elevation is {sun.elevation_deg:.1f} deg "
                "(night); pick a daylight instant"
            )
        instant_id = f"{when:%Y%m%dT%H%M}"
        phase_start = time.monotonic()
        state = compute_state_raster(artifact_dir, sun)
        state[roof] = STATE_OUTSIDE
        # Under-canopy pixels move to the static canopy layer: the
        # per-instant sets keep only *cast* shade. Dropped pixels become
        # STATE_SUN (transparent), keeping STATE_OUTSIDE strictly for roofs
        # and out-of-coverage pixels.
        state[canopy & (state == STATE_SHADE_VEGETATION)] = STATE_SUN
        echo(
            f"[{index}/{len(ordered)}] {instant_id}: state raster in "
            f"{format_duration(time.monotonic() - phase_start)}"
        )

        # Two independently toggleable cast-shade sets, same color: hiding
        # the trees set answers "how much street shade does the canopy add"
        # (the streets-without-trees scenario, intervention-planning bait).
        building_state = state.copy()
        building_state[state == STATE_SHADE_VEGETATION] = STATE_SUN
        trees_state = state.copy()
        trees_state[(state != STATE_SHADE_VEGETATION) & (state != STATE_OUTSIDE)] = STATE_SUN

        urls: dict[str, str] = {"vegetation": canopy_url}
        for kind, layer_state in (("building", building_state), ("trees", trees_state)):
            filename = f"shade-{instant_id}-{kind}.pmtiles"
            phase_start = time.monotonic()
            written, skipped = write_instant_pmtiles(
                tiles_dir / filename,
                layer_state,
                transform,
                metadata.crs,
                (west, south, east, north),
                min_zoom=min_zoom,
                max_zoom=max_zoom,
                metadata={
                    "name": f"{config.name} shade ({kind}) {when.isoformat()}",
                    "attribution": " / ".join(metadata.attribution),
                },
            )
            size = (tiles_dir / filename).stat().st_size
            total_written += written
            total_skipped += skipped
            total_bytes += size
            urls[kind] = f"{filename}?v={version}"
            echo(
                f"[{index}/{len(ordered)}] {filename}: {written} tiles written, "
                f"{skipped} transparent skipped ({format_bytes(size)}, "
                f"{format_duration(time.monotonic() - phase_start)})"
            )
        offset = f"{when:%z}"
        entries.append(
            {
                "id": instant_id,
                "date": f"{when:%Y-%m-%d}",
                "time": f"{when:%H:%M}",
                "at": when.replace(tzinfo=None).isoformat(timespec="minutes"),
                "utc_offset": f"{offset[:3]}:{offset[3:]}",
                # url is the legacy alias of the building set (same semantics
                # as the original split); urls.vegetation points a schema-2
                # client's toggle at the static canopy.
                "url": urls["building"],
                "urls": dict(urls),
                "sun": {
                    "azimuth_deg": round(sun.azimuth_deg, 2),
                    "elevation_deg": round(sun.elevation_deg, 2),
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
