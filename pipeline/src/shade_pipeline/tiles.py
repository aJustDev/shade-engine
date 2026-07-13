"""Per-instant shade overlays as raster PMTiles (see docs/learning/map-tiles-pmtiles.md).

Web maps consume square 256 px tiles addressed by z/x/y in Web Mercator
(EPSG:3857): at zoom z the world is a 2^z x 2^z grid, x grows east and y
grows *south*. PMTiles packs a whole tile pyramid into one static file with
a Hilbert-ordered directory at the front, so a browser fetches any tile
with plain HTTP range requests -- the COG trick applied to web pyramids,
and the reason serving these needs no tile server, only Caddy.

Zoom bounds: Web Mercator inflates distances by 1/cos(lat) (see
docs/learning/web-mercator.md), so at Cordoba's latitude (37.9 N) zoom 17
is 156543/2^17 * cos(37.9) = 0.94 m/px -- our native 1 m resolution. Higher
zooms would only upsample (the map client already overzooms past max_zoom);
zoom 12 (~30 m/px) fits the whole city on two tiles.

Each instant becomes two files -- building+other shade and vegetation shade
-- so the web client can toggle the vegetation layer independently. Tiles
for a fixed sun position are immutable, which is what makes the static
approach work (and cacheable forever). The engine itself is never consulted
at view time.
"""

import io
import json
import math
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, tzinfo
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
from pyproj import Transformer
from rasterio.enums import Resampling
from rasterio.io import MemoryFile
from rasterio.transform import from_origin
from rasterio.vrt import WarpedVRT
from rasterio.windows import Window

from shade_core.artifacts import LANDCOVER_FILENAME, load_metadata
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
SHADE_COLORS: Final[dict[int, tuple[int, int, int, int]]] = {
    STATE_SUN: (0, 0, 0, 0),  # sun = transparent: the overlay only paints shade
    STATE_SHADE_BUILDING: (36, 48, 94, OVERLAY_ALPHA),  # deep indigo
    STATE_SHADE_VEGETATION: (31, 90, 74, OVERLAY_ALPHA),  # green-teal
    STATE_SHADE_OTHER: (44, 52, 66, OVERLAY_ALPHA),  # neutral blue-grey
    STATE_OUTSIDE: (0, 0, 0, 0),
}

# The 2026 solstice/equinox preset, civil local hours per date. Hours are
# picked to stay within daylight at Iberian latitudes (Cordoba's winter
# sunset is ~18:05 local); the two equinoxes share hours on purpose -- solar
# declination is ~0 at both, so their shade patterns nearly coincide, which
# the map makes visible.
SEASON_PRESET_2026: Final[tuple[tuple[str, tuple[str, ...]], ...]] = (
    ("2026-03-20", ("09:00", "12:00", "15:00", "18:00")),  # spring equinox (CET)
    ("2026-06-21", ("09:00", "14:00", "17:00", "20:00")),  # summer solstice (CEST)
    ("2026-09-22", ("09:00", "12:00", "15:00", "18:00")),  # autumn equinox (CEST)
    ("2026-12-21", ("10:00", "12:00", "14:00", "16:00")),  # winter solstice (CET)
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
_PALETTE_RGB: Final = bytes(
    channel for state in _PALETTE_STATES for channel in SHADE_COLORS[state][:3]
)
_PALETTE_TRNS: Final = bytes(SHADE_COLORS[state][3] for state in _PALETTE_STATES)


def _palette_index() -> npt.NDArray[np.uint8]:
    index = np.zeros(256, dtype=np.uint8)
    for position, state in enumerate(_PALETTE_STATES):
        index[state] = position
    return index


_INDEX_OF_STATE: Final = _palette_index()


def season_preset_instants(tz: tzinfo) -> list[datetime]:
    """The 2026 season preset as aware datetimes in the city's zone.

    DST trap: the preset straddles the March/October changes (Spain runs
    UTC+1 in March/December, UTC+2 in June/September). ``ZoneInfo`` resolves
    each date's offset; never bake a fixed offset into a list of instants
    that crosses a DST boundary.
    """
    return [
        datetime.fromisoformat(f"{day}T{hhmm}").replace(tzinfo=tz)
        for day, hours in SEASON_PRESET_2026
        for hhmm in hours
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


def _encode_png(state_tile: npt.NDArray[np.uint8]) -> bytes:
    image = Image.fromarray(_INDEX_OF_STATE[state_tile], mode="P")
    image.putpalette(_PALETTE_RGB)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True, transparency=_PALETTE_TRNS)
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
                            int(zxy_to_tileid(tile.z, tile.x, tile.y)), _encode_png(data)
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
    """Render two shade PMTiles per instant plus the ``index.json`` manifest.

    Each instant splits into a *building* set (building + other shade) and a
    *vegetation* set, so the web client can toggle the vegetation layer.
    Building interiors are masked transparent in both -- nobody stands on a
    roof, and the basemap already draws the buildings -- turning the overlay
    into street-level shade only.

    The manifest is what the web client consumes: available instants (with
    the naive local ``at`` string ready for the API's ``?at=`` parameter),
    relative tile URLs with a ``?v=`` epoch (cache busting against
    long-lived immutable caching), bounds, colors and attribution. Output
    lands under ``<artifact_dir>/tiles/``; the basemap referenced by
    ``basemap_url`` is produced out of band (see docs/adding-a-city.md).
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

    # Roof mask, read once (instant-invariant). Applied AFTER
    # compute_state_raster so that function stays in pixel parity with
    # is_shaded: the mask is presentation only. Roofs become STATE_OUTSIDE
    # (the warp nodata, alpha 0 in the palette) rather than STATE_SUN, so a
    # decoded tile still distinguishes "roof" from "sunlit street".
    with rasterio.open(Path(artifact_dir) / LANDCOVER_FILENAME) as src:
        roof = src.read()[0] == Landcover.BUILDING

    tiles_dir = Path(artifact_dir) / TILES_DIRNAME
    tiles_dir.mkdir(parents=True, exist_ok=True)
    build_start = time.monotonic()
    total_written = 0
    total_skipped = 0
    total_bytes = 0
    version = int(time.time())
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
        echo(
            f"[{index}/{len(ordered)}] {instant_id}: state raster in "
            f"{format_duration(time.monotonic() - phase_start)}"
        )
        # The building set keeps SHADE_OTHER too (ground/open-sky blockers);
        # dropped states become STATE_SUN (transparent), keeping
        # STATE_OUTSIDE strictly for roofs and out-of-coverage pixels.
        building_state = state.copy()
        building_state[state == STATE_SHADE_VEGETATION] = STATE_SUN
        vegetation_state = state.copy()
        vegetation_state[(state != STATE_SHADE_VEGETATION) & (state != STATE_OUTSIDE)] = STATE_SUN

        urls: dict[str, str] = {}
        for kind, layer_state in (("building", building_state), ("vegetation", vegetation_state)):
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
                # Legacy single-url field (= the building set) so a deployed
                # client still on schema 1 keeps rendering during the swap.
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
            "shade_building": _hex(SHADE_COLORS[STATE_SHADE_BUILDING]),
            "shade_vegetation": _hex(SHADE_COLORS[STATE_SHADE_VEGETATION]),
            "shade_other": _hex(SHADE_COLORS[STATE_SHADE_OTHER]),
            "alpha": round(OVERLAY_ALPHA / 255.0, 2),
        },
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
        f"({len(ordered)} instants, {2 * len(ordered)} pmtiles, {format_bytes(total_bytes)}, "
        f"{total_written} tiles written, {total_skipped} skipped)"
    )
    return tiles_dir


def _hex(color: tuple[int, int, int, int]) -> str:
    red, green, blue, _alpha = color
    return f"#{red:02x}{green:02x}{blue:02x}"
