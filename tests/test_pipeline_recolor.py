"""Recolouring a tile tree must change colours and nothing else."""

import io
import json
import zlib
from pathlib import Path

import numpy as np
import pytest
from affine import Affine
from PIL import Image
from pmtiles.reader import MmapSource, Reader, all_tiles

from shade_pipeline.recolor import (
    LIGHT,
    RGBA,
    recolor_archive,
    recolor_city,
    rewrite_png_palette,
)
from shade_pipeline.shade_raster import (
    STATE_OUTSIDE,
    STATE_SHADE_BUILDING,
    STATE_SHADE_OTHER,
    STATE_SHADE_VEGETATION,
    STATE_SUN,
)
from shade_pipeline.tiles import (
    SHADE_COLORS,
    bounds_wgs84,
    palette_bytes,
    write_instant_pmtiles,
)

# A tile carrying every state, so the palette rewrite is exercised end to end.
STATES = (STATE_SUN, STATE_SHADE_BUILDING, STATE_SHADE_VEGETATION, STATE_SHADE_OTHER, STATE_OUTSIDE)

# A 256 x 256 m patch of Cordoba at 1 m/px, enough to produce real tiles.
CRS = "EPSG:25830"
BBOX = (341000.0, 4198744.0, 341256.0, 4199000.0)
TRANSFORM = Affine(1.0, 0.0, BBOX[0], 0.0, -1.0, BBOX[3])


def _write_archive(path: Path, state: np.ndarray, **kwargs: object) -> int:
    written, _skipped = write_instant_pmtiles(
        path,
        state,
        TRANSFORM,
        CRS,
        bounds_wgs84(CRS, BBOX),
        min_zoom=12,
        max_zoom=14,
        colors=SHADE_COLORS,
        **kwargs,  # type: ignore[arg-type]
    )
    return written


def _paletted_png(colors: dict[int, RGBA]) -> bytes:
    from shade_pipeline.tiles import _encode_png

    tile = np.array([[STATES[(x + y) % len(STATES)] for x in range(8)] for y in range(8)], np.uint8)
    return _encode_png(tile, palette_bytes(colors))


def _chunks(png: bytes) -> dict[bytes, bytes]:
    out: dict[bytes, bytes] = {}
    pos = 8
    while pos < len(png):
        length = int.from_bytes(png[pos : pos + 4], "big")
        out[png[pos + 4 : pos + 8]] = png[pos + 8 : pos + 8 + length]
        pos += 12 + length
    return out


def _indices(png: bytes) -> np.ndarray:
    """Decode the palette INDICES, not the colours."""
    with Image.open(io.BytesIO(png)) as image:
        assert image.mode == "P"
        return np.array(image)


class TestRewritePngPalette:
    def test_pixel_indices_survive_untouched(self) -> None:
        """The whole point: the state of every pixel is preserved."""
        original = _paletted_png(SHADE_COLORS)
        rgb, trns = palette_bytes(LIGHT.shade)

        recoloured = rewrite_png_palette(original, rgb, trns)

        np.testing.assert_array_equal(_indices(original), _indices(recoloured))

    def test_idat_is_byte_identical(self) -> None:
        """Nothing is re-encoded, so the compressed pixel data must be the same bytes."""
        original = _paletted_png(SHADE_COLORS)
        rgb, trns = palette_bytes(LIGHT.shade)

        recoloured = rewrite_png_palette(original, rgb, trns)

        assert _chunks(recoloured)[b"IDAT"] == _chunks(original)[b"IDAT"]

    def test_palette_carries_the_new_colours(self) -> None:
        original = _paletted_png(SHADE_COLORS)
        rgb, trns = palette_bytes(LIGHT.shade)

        chunks = _chunks(rewrite_png_palette(original, rgb, trns))

        assert chunks[b"PLTE"] == rgb
        assert chunks[b"tRNS"] == trns

    def test_crc_is_recomputed(self) -> None:
        """A stale CRC makes the tile undecodable in a browser, silently for us."""
        original = _paletted_png(SHADE_COLORS)
        rgb, trns = palette_bytes(LIGHT.shade)

        recoloured = rewrite_png_palette(original, rgb, trns)

        pos = 8
        while pos < len(recoloured):
            length = int.from_bytes(recoloured[pos : pos + 4], "big")
            chunk = recoloured[pos + 4 : pos + 8 + length]
            stored = int.from_bytes(recoloured[pos + 8 + length : pos + 12 + length], "big")
            assert zlib.crc32(chunk) == stored, f"CRC roto en {chunk[:4]!r}"
            pos += 12 + length

    def test_rejects_a_palette_of_a_different_size(self) -> None:
        """A shorter palette leaves indices pointing nowhere: garbage, not an error."""
        original = _paletted_png(SHADE_COLORS)

        with pytest.raises(ValueError, match="palette entry count must match"):
            rewrite_png_palette(original, b"\x00" * 9, b"\x00" * 3)

    def test_rejects_something_that_is_not_a_png(self) -> None:
        with pytest.raises(ValueError, match="not a PNG"):
            rewrite_png_palette(b"clearly not a png", b"", b"")


class TestRecolorArchive:
    def test_archive_keeps_every_tile_and_swaps_the_palette(self, tmp_path: Path) -> None:
        source = tmp_path / "shade-test.pmtiles"
        state = np.array(
            [[STATES[(x + y) % len(STATES)] for x in range(256)] for y in range(256)], np.uint8
        )
        written = _write_archive(source, state)

        destination = tmp_path / "light" / "shade-test.pmtiles"
        recoloured = recolor_archive(source, destination, LIGHT.shade)

        assert recoloured == written
        with open(destination, "rb") as handle:
            tiles = list(all_tiles(MmapSource(handle)))
        assert len(tiles) == written
        expected_rgb, _ = palette_bytes(LIGHT.shade)
        for _, data in tiles:
            assert _chunks(data)[b"PLTE"] == expected_rgb

    def test_geographic_header_survives(self, tmp_path: Path) -> None:
        """Bounds and tile type come from the source; only offsets are recomputed."""
        source = tmp_path / "shade-test.pmtiles"
        state = np.full((256, 256), STATE_SHADE_BUILDING, np.uint8)
        _write_archive(source, state)
        destination = tmp_path / "light" / "shade-test.pmtiles"
        recolor_archive(source, destination, LIGHT.shade)

        with open(source, "rb") as handle:
            before = Reader(MmapSource(handle)).header()
        with open(destination, "rb") as handle:
            after = Reader(MmapSource(handle)).header()

        for key in ("min_lon_e7", "min_lat_e7", "max_lon_e7", "max_lat_e7", "tile_type"):
            assert after[key] == before[key], key


class TestRecolorCity:
    def test_writes_a_sibling_tree_with_its_own_manifest(self, tmp_path: Path) -> None:
        tiles_dir = tmp_path / "testcity" / "v1" / "tiles"
        tiles_dir.mkdir(parents=True)
        state = np.full((256, 256), STATE_SHADE_BUILDING, np.uint8)
        _write_archive(tiles_dir / "shade-20260321T1200-building.pmtiles", state)
        (tiles_dir / "index.json").write_text(
            json.dumps({"schema_version": 2, "city": "testcity", "colors": {"alpha": 0.78}}),
            encoding="utf-8",
        )
        (tiles_dir / "basemap.pmtiles").write_bytes(b"vector basemap, no baked colours")

        report = recolor_city(tmp_path, "testcity", LIGHT)

        assert report.destination == tmp_path / "testcity" / "v1" / "tiles-light"
        assert report.archives == 1
        assert "index.json" in report.copied
        # The basemap is vector: it travels verbatim so the tree is self-contained.
        assert (report.destination / "basemap.pmtiles").read_bytes() == (
            tiles_dir / "basemap.pmtiles"
        ).read_bytes()

        manifest = json.loads((report.destination / "index.json").read_text(encoding="utf-8"))
        assert manifest["palette"] == "light"
        assert manifest["colors"]["shade"] == "#5e708a"
        # The alpha belongs to the palette too. Leaving the source value would
        # publish a manifest claiming 0.78 for tiles baked at 120/255, and a
        # manifest that lies about its own tiles is not something this project
        # can ship -- even while no client reads the field.
        assert manifest["colors"]["alpha"] == pytest.approx(120 / 255, abs=1e-3)
        assert manifest["colors"]["alpha"] != 0.78  # el valor del arbol oscuro de origen
        # Untouched keys survive.
        assert manifest["schema_version"] == 2

    def test_missing_tree_is_an_accionable_error(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError, match="no tile tree"):
            recolor_city(tmp_path, "nope", LIGHT)
