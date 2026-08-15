"""Recolour an existing tile tree without recomputing any shade.

The tiles are paletted PNGs: every pixel stores a *state index* (sun, building
shade, tree shadow, other, outside) and the colours live in the PLTE and tRNS
chunks. That was a deliberate choice when the renderer was written -- states
keep distinct palette indices even where two of them share a colour -- and it
is what makes this module possible: swapping a theme is rewriting 20 bytes per
tile, not re-running the sun.

Cost, measured on the real trees: a full render of Cordoba is 2-3 hours; this
pass is I/O bound over 169 files and finishes in minutes. Nothing about the
solar geometry is touched, so the two trees are guaranteed to agree pixel for
pixel; only their colours differ.

The output is a sibling tree (``v1/tiles-light/``). Caddy serves ``/tiles/*``
mirroring the on-disk layout, so a new tree needs no server-side change.

Why a second tree at all: a raster tile cannot be recoloured by the map client.
MapLibre can restyle vector layers, but a raster layer is pixels, and
``raster-opacity`` is not an escape hatch either because the alpha is baked in
too. Either the colours ship in the file or they do not exist.
"""

import json
import shutil
import zlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from pmtiles.reader import MmapSource, Reader, all_tiles
from pmtiles.tile import zxy_to_tileid
from pmtiles.writer import Writer

from shade_pipeline.shade_raster import (
    STATE_OUTSIDE,
    STATE_SHADE_BUILDING,
    STATE_SHADE_OTHER,
    STATE_SHADE_VEGETATION,
    STATE_SUN,
)
from shade_pipeline.tiles import (
    BASEMAP_FILENAME,
    BUILDINGS_TILES_FILENAME,
    CANOPY_TILES_FILENAME,
    MANIFEST_FILENAME,
    PALETTE_STATES,
    TILES_DIRNAME,
    palette_bytes,
)

RGBA = tuple[int, int, int, int]

# Light theme. The dark palette is tuned against a black basemap at alpha 200
# (0.78); over a light basemap that reads as a blanket and swallows the street
# names underneath. Shade is darkening, so the light theme keeps the same
# metaphor with a desaturated slate blue at ~43%.
LIGHT_ALPHA: Final = 110
LIGHT_SHADE_COLOR: Final[RGBA] = (90, 104, 150, LIGHT_ALPHA)
LIGHT_CANOPY_COLOR: Final[RGBA] = (58, 130, 106, LIGHT_ALPHA)
LIGHT_BUILDINGS_COLOR: Final[RGBA] = (120, 128, 145, LIGHT_ALPHA)

_TRANSPARENT: Final[RGBA] = (0, 0, 0, 0)


def _shade_palette(color: RGBA) -> dict[int, RGBA]:
    """Cast shade: buildings, trees and other share one colour, as in the dark theme."""
    return {
        STATE_SUN: _TRANSPARENT,
        STATE_SHADE_BUILDING: color,
        STATE_SHADE_VEGETATION: color,
        STATE_SHADE_OTHER: color,
        STATE_OUTSIDE: _TRANSPARENT,
    }


def _single_state_palette(state: int, color: RGBA) -> dict[int, RGBA]:
    return {s: (color if s == state else _TRANSPARENT) for s in PALETTE_STATES}


@dataclass(frozen=True)
class Palette:
    """A named theme: which colours each kind of tile file gets."""

    name: str
    shade: dict[int, RGBA]
    canopy: dict[int, RGBA]
    buildings: dict[int, RGBA]
    manifest_colors: dict[str, str]


def _hex(color: RGBA) -> str:
    return "#{:02x}{:02x}{:02x}".format(*color[:3])


LIGHT: Final = Palette(
    name="light",
    shade=_shade_palette(LIGHT_SHADE_COLOR),
    canopy=_single_state_palette(STATE_SHADE_VEGETATION, LIGHT_CANOPY_COLOR),
    buildings=_single_state_palette(STATE_SHADE_BUILDING, LIGHT_BUILDINGS_COLOR),
    manifest_colors={
        "shade": _hex(LIGHT_SHADE_COLOR),
        "canopy": _hex(LIGHT_CANOPY_COLOR),
        "buildings": _hex(LIGHT_BUILDINGS_COLOR),
        # Legacy aliases kept so an older client reading this manifest still
        # finds the keys it expects.
        "shade_building": _hex(LIGHT_SHADE_COLOR),
        "shade_vegetation": _hex(LIGHT_CANOPY_COLOR),
        "shade_other": _hex(LIGHT_SHADE_COLOR),
    },
)

PALETTES: Final[dict[str, Palette]] = {LIGHT.name: LIGHT}

_PNG_SIGNATURE: Final = b"\x89PNG\r\n\x1a\n"


def rewrite_png_palette(png: bytes, rgb: bytes, trns: bytes) -> bytes:
    """Replace the PLTE and tRNS chunks, leaving the pixel data untouched.

    The lengths must match the original chunks exactly. A palette with a
    different number of entries would leave pixel indices pointing outside it,
    which decodes to garbage rather than to an error -- so this is checked, not
    assumed.
    """
    if not png.startswith(_PNG_SIGNATURE):
        raise ValueError("tile is not a PNG")

    out = bytearray(_PNG_SIGNATURE)
    pos = len(_PNG_SIGNATURE)
    replaced: set[bytes] = set()

    while pos < len(png):
        length = int.from_bytes(png[pos : pos + 4], "big")
        chunk_type = png[pos + 4 : pos + 8]
        payload = png[pos + 8 : pos + 8 + length]
        pos += 12 + length

        if chunk_type in (b"PLTE", b"tRNS"):
            replacement = rgb if chunk_type == b"PLTE" else trns
            if len(replacement) != length:
                raise ValueError(
                    f"{chunk_type.decode()} would change from {length} to "
                    f"{len(replacement)} bytes; palette entry count must match"
                )
            payload = replacement
            replaced.add(chunk_type)

        out += len(payload).to_bytes(4, "big")
        out += chunk_type
        out += payload
        out += zlib.crc32(chunk_type + payload).to_bytes(4, "big")

    missing = {b"PLTE", b"tRNS"} - replaced
    if missing:
        names = ", ".join(sorted(chunk.decode() for chunk in missing))
        raise ValueError(f"tile has no {names} chunk: not a paletted tile")

    return bytes(out)


def recolor_archive(source: Path, destination: Path, colors: Mapping[int, RGBA]) -> int:
    """Rewrite every tile of one PMTiles archive with a new palette."""
    rgb, trns = palette_bytes(colors)
    written = 0

    with open(source, "rb") as handle:
        get_bytes = MmapSource(handle)
        reader = Reader(get_bytes)
        header = dict(reader.header())
        metadata = reader.metadata()

        destination.parent.mkdir(parents=True, exist_ok=True)
        with open(destination, "wb") as sink:
            writer = Writer(sink)
            # all_tiles walks the directory in tile-id order, so the archive
            # stays clustered without sorting anything here.
            for (z, x, y), data in all_tiles(get_bytes):
                writer.write_tile(int(zxy_to_tileid(z, x, y)), rewrite_png_palette(data, rgb, trns))
                written += 1
            # finalize recomputes offsets, counts and zoom range; the
            # geographic bounds and tile type come from the source header.
            writer.finalize(header, metadata)

    return written


@dataclass(frozen=True)
class RecolorReport:
    palette: str
    archives: int
    tiles: int
    copied: list[str]
    destination: Path


def _colors_for(filename: str, palette: Palette) -> Mapping[int, RGBA] | None:
    """Which palette a file gets, or None when it is not a shade raster."""
    if filename.startswith("shade-"):
        return palette.shade
    if filename == CANOPY_TILES_FILENAME:
        return palette.canopy
    if filename == BUILDINGS_TILES_FILENAME:
        return palette.buildings
    return None


def recolor_city(
    root: Path,
    city_id: str,
    palette: Palette,
    *,
    artifact_version: str = "v1",
    progress: Callable[[str], None] | None = None,
) -> RecolorReport:
    """Build a sibling tile tree in a different theme.

    The output is self-contained: the basemap (vector, 3 MB, no baked colours)
    is copied rather than shared, so the tree can be rsynced on its own and the
    client only swaps one path segment.
    """
    source_dir = root / city_id / artifact_version / TILES_DIRNAME
    if not source_dir.is_dir():
        raise FileNotFoundError(f"no tile tree at {source_dir}")

    destination_dir = root / city_id / artifact_version / f"{TILES_DIRNAME}-{palette.name}"
    destination_dir.mkdir(parents=True, exist_ok=True)

    archives = 0
    tiles = 0
    copied: list[str] = []

    for path in sorted(source_dir.iterdir()):
        if not path.is_file():
            continue
        colors = _colors_for(path.name, palette)
        if colors is not None:
            tiles += recolor_archive(path, destination_dir / path.name, colors)
            archives += 1
            if progress is not None:
                progress(f"  recoloured {path.name}")
        elif path.name == MANIFEST_FILENAME:
            manifest = json.loads(path.read_text(encoding="utf-8"))
            manifest["colors"] = {**manifest.get("colors", {}), **palette.manifest_colors}
            manifest["palette"] = palette.name
            (destination_dir / path.name).write_text(
                json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
            )
            copied.append(path.name)
        elif path.name == BASEMAP_FILENAME:
            shutil.copy2(path, destination_dir / path.name)
            copied.append(path.name)
        else:
            shutil.copy2(path, destination_dir / path.name)
            copied.append(path.name)

    return RecolorReport(
        palette=palette.name,
        archives=archives,
        tiles=tiles,
        copied=copied,
        destination=destination_dir,
    )
