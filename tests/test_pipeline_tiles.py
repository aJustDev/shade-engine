"""Shade tiles: state raster parity with the engine, PMTiles output, manifest."""

import io
import json
import shutil
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import mercantile
import pytest
import rasterio
import yaml
from PIL import Image
from pmtiles.reader import MmapSource, Reader
from pmtiles.tile import Compression, TileType
from pyproj import Transformer
from typer.testing import CliRunner

import synthetic
from conftest import CUBE_CITY
from shade_core import artifacts
from shade_core.shade import Landcover, ShadeResult, ShadeState, ShadeType, is_shaded
from shade_core.solar import sun_position
from shade_pipeline.cli import app
from shade_pipeline.cog import write_cog
from shade_pipeline.grid import transform_from_bbox
from shade_pipeline.shade_raster import (
    STATE_SHADE_BUILDING,
    STATE_SHADE_OTHER,
    STATE_SHADE_VEGETATION,
    STATE_SUN,
    compute_state_raster,
)
from shade_pipeline.tiles import (
    MANIFEST_FILENAME,
    SHADE_COLORS,
    bounds_wgs84,
    build_tiles,
    write_instant_pmtiles,
)

CORDOBA_LAT, CORDOBA_LON = 37.88, -4.78
NEAR = (
    synthetic.UTM_ORIGIN[0] + synthetic.QUERY_X,
    synthetic.UTM_ORIGIN[1] + synthetic.CUBE_NORTH_WALL_Y + 10.0,
)
WINTER_NOON = datetime(2026, 12, 21, 13, 20, tzinfo=ZoneInfo("Europe/Madrid"))
SUMMER_NOON = datetime(2026, 6, 21, 14, 20, tzinfo=ZoneInfo("Europe/Madrid"))

_STATE_OF_RESULT = {
    (ShadeState.SUN, None): STATE_SUN,
    (ShadeState.SHADE, ShadeType.BUILDING): STATE_SHADE_BUILDING,
    (ShadeState.SHADE, ShadeType.VEGETATION): STATE_SHADE_VEGETATION,
    (ShadeState.SHADE, None): STATE_SHADE_OTHER,
}


def _expected_state(result: ShadeResult) -> int:
    return _STATE_OF_RESULT[(result.state, result.shade_type)]


@pytest.mark.parametrize("when", [WINTER_NOON, SUMMER_NOON], ids=["winter", "summer"])
def test_state_raster_parity_with_engine(built_city: Path, when: datetime) -> None:
    """Every pixel agrees with is_shaded, except exact float-boundary ties."""
    sun = sun_position(CORDOBA_LAT, CORDOBA_LON, when)
    state = compute_state_raster(built_city, sun)
    scene = artifacts.load_scene(built_city)
    metadata = artifacts.load_metadata(built_city)
    min_x, _, _, max_y = metadata.bbox
    resolution = metadata.resolution_m

    rows, cols = state.shape
    mismatches = []
    for row in range(rows):
        for col in range(cols):
            x = min_x + (col + 0.5) * resolution
            y = max_y - (row + 0.5) * resolution
            if abs(sun.elevation_deg - scene.horizon.horizon_at(x, y, sun.azimuth_deg)) < 1e-6:
                continue  # legitimate float-boundary tie, either verdict is fine
            expected = _expected_state(is_shaded(scene, x, y, sun))
            if int(state[row, col]) != expected:
                mismatches.append((row, col, expected, int(state[row, col])))
    assert not mismatches, mismatches[:10]


def test_state_raster_rejects_night(built_city: Path) -> None:
    midnight = sun_position(CORDOBA_LAT, CORDOBA_LON, WINTER_NOON.replace(hour=23))
    with pytest.raises(ValueError, match="night"):
        compute_state_raster(built_city, midnight)


def test_state_raster_canopy_overrides_sun(built_city: Path, tmp_path: Path) -> None:
    """A pixel under vegetation is vegetation-shaded even where the horizon says sun."""
    target = tmp_path / "city"
    shutil.copytree(built_city, target)
    with rasterio.open(target / artifacts.LANDCOVER_FILENAME) as src:
        landcover = src.read()[0]
        transform = src.transform
        crs = str(src.crs)
    row, col = 5, 7  # far from the cube: sunlit at both golden instants
    landcover[row, col] = Landcover.VEGETATION
    write_cog(target / artifacts.LANDCOVER_FILENAME, landcover, transform, crs)

    sun = sun_position(CORDOBA_LAT, CORDOBA_LON, SUMMER_NOON)
    state = compute_state_raster(target, sun)
    assert int(state[row, col]) == STATE_SHADE_VEGETATION


def test_pmtiles_roundtrip(built_city: Path, tmp_path: Path) -> None:
    """Written archive reads back: PNG type, no tile compression, right pixels."""
    metadata = artifacts.load_metadata(built_city)
    sun = sun_position(CORDOBA_LAT, CORDOBA_LON, WINTER_NOON)
    state = compute_state_raster(built_city, sun)
    bounds = bounds_wgs84(metadata.crs, metadata.bbox)
    path = tmp_path / "winter.pmtiles"
    written, _skipped = write_instant_pmtiles(
        path,
        state,
        transform_from_bbox(metadata.bbox, metadata.resolution_m),
        metadata.crs,
        bounds,
        min_zoom=12,
        max_zoom=17,
    )
    assert written > 0

    to_wgs84 = Transformer.from_crs(metadata.crs, "EPSG:4326", always_xy=True)
    with open(path, "rb") as handle:
        reader = Reader(MmapSource(handle))
        header = reader.header()
        assert header["tile_type"] == TileType.PNG
        assert header["tile_compression"] == Compression.NONE

        def rgba_at(x: float, y: float, zoom: int) -> tuple[int, int, int, int]:
            lon, lat = to_wgs84.transform(x, y)
            tile = mercantile.tile(lon, lat, zoom)
            data = reader.get(tile.z, tile.x, tile.y)
            assert data is not None
            image = Image.open(io.BytesIO(data)).convert("RGBA")
            merc_x, merc_y = mercantile.xy(lon, lat)
            tile_bounds = mercantile.xy_bounds(tile.x, tile.y, tile.z)
            resolution = (tile_bounds.right - tile_bounds.left) / image.width
            px = int((merc_x - tile_bounds.left) / resolution)
            py = int((tile_bounds.top - merc_y) / resolution)
            pixel = image.getpixel((px, py))
            assert isinstance(pixel, tuple) and len(pixel) == 4
            return (pixel[0], pixel[1], pixel[2], pixel[3])

        # NEAR sits deep in the cube's winter shadow: building color.
        assert rgba_at(*NEAR, 17) == SHADE_COLORS[STATE_SHADE_BUILDING]
        # A corner far from the cube is sunlit: fully transparent.
        sunny = (synthetic.UTM_ORIGIN[0] + 25.0, synthetic.UTM_ORIGIN[1] + 95.0)
        assert rgba_at(*sunny, 17)[3] == 0


def test_transparent_tiles_skipped(built_city: Path, tmp_path: Path) -> None:
    """Blank tiles are absent above min_zoom; min_zoom is always written."""
    metadata = artifacts.load_metadata(built_city)
    sun = sun_position(CORDOBA_LAT, CORDOBA_LON, SUMMER_NOON)  # high sun, small shadow
    state = compute_state_raster(built_city, sun)
    bounds = bounds_wgs84(metadata.crs, metadata.bbox)
    path = tmp_path / "summer.pmtiles"
    # Overzoom to 20 (~29 m tiles) so the 80 m scene spans several tiles,
    # some fully sunlit.
    _written, skipped = write_instant_pmtiles(
        path,
        state,
        transform_from_bbox(metadata.bbox, metadata.resolution_m),
        metadata.crs,
        bounds,
        min_zoom=12,
        max_zoom=20,
    )
    assert skipped > 0

    to_wgs84 = Transformer.from_crs(metadata.crs, "EPSG:4326", always_xy=True)
    # NEAR is sunlit in summer; the cube's own footprint (horizon seen from
    # inside the building) is always building-shade, so its tile must exist.
    cube_center = (
        synthetic.UTM_ORIGIN[0] + synthetic.CUBE_CENTER_X,
        synthetic.UTM_ORIGIN[1] + (synthetic.CUBE_Y[0] + synthetic.CUBE_Y[1]) / 2.0,
    )
    cube_lon, cube_lat = to_wgs84.transform(*cube_center)
    with open(path, "rb") as handle:
        reader = Reader(MmapSource(handle))
        cube_tile = mercantile.tile(cube_lon, cube_lat, 20)
        assert reader.get(cube_tile.z, cube_tile.x, cube_tile.y) is not None
        # The scene's NW corner is sunlit in summer; its z20 tile was skipped.
        west, _, _, north = bounds
        corner_tile = mercantile.tile(west + 1e-5, north - 1e-5, 20)
        assert corner_tile != cube_tile
        assert reader.get(corner_tile.z, corner_tile.x, corner_tile.y) is None
        # min_zoom always written, even where a blank tile would be skipped.
        base_tile = mercantile.tile(cube_lon, cube_lat, 12)
        assert reader.get(base_tile.z, base_tile.x, base_tile.y) is not None


def test_build_tiles_manifest(built_city: Path, tmp_path: Path) -> None:
    target = tmp_path / "city"
    shutil.copytree(built_city, target)
    tiles_dir = build_tiles(CUBE_CITY, target, [SUMMER_NOON, WINTER_NOON], min_zoom=14, max_zoom=16)
    manifest = json.loads((tiles_dir / MANIFEST_FILENAME).read_text(encoding="utf-8"))

    assert manifest["city"] == "cube"
    assert manifest["timezone"] == "Europe/Madrid"
    assert manifest["attribution"] == ["Synthetic LiDAR (test fixture)"]
    west, south, east, north = manifest["bounds_wgs84"]
    assert -4.9 < west < east < -4.7
    assert 37.8 < south < north < 38.0

    instants = manifest["instants"]
    assert [entry["id"] for entry in instants] == ["20260621T1420", "20261221T1320"]
    summer, winter = instants
    assert summer["at"] == "2026-06-21T14:20"
    assert summer["utc_offset"] == "+02:00"  # CEST
    assert winter["utc_offset"] == "+01:00"  # CET: the preset spans DST changes
    for entry in instants:
        filename = str(entry["url"]).split("?")[0]
        assert (tiles_dir / filename).exists()
        assert entry["sun"]["elevation_deg"] > 0


def test_build_tiles_rejects_night_instant(built_city: Path, tmp_path: Path) -> None:
    target = tmp_path / "city"
    shutil.copytree(built_city, target)
    with pytest.raises(ValueError, match="night"):
        build_tiles(CUBE_CITY, target, [WINTER_NOON.replace(hour=23)])


def test_cli_tiles_smoke(built_city: Path, tmp_path: Path) -> None:
    cities_dir = tmp_path / "cities"
    cities_dir.mkdir()
    (cities_dir / "cube.yaml").write_text(yaml.safe_dump(CUBE_CITY.model_dump(mode="json")))
    output_root = tmp_path / "data"
    shutil.copytree(built_city, output_root / "cube" / "v1")

    result = CliRunner().invoke(
        app,
        [
            "tiles",
            "cube",
            "--at",
            "2026-12-21T13:20",
            "--min-zoom",
            "14",
            "--max-zoom",
            "17",
            "--cities-dir",
            str(cities_dir),
            "--output-root",
            str(output_root),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "shade-20261221T1320.pmtiles" in result.output
    assert "tiles written to" in result.output
    tiles_dir = output_root / "cube" / "v1" / "tiles"
    assert (tiles_dir / "shade-20261221T1320.pmtiles").exists()
    assert (tiles_dir / MANIFEST_FILENAME).exists()

    night = CliRunner().invoke(
        app,
        [
            "tiles",
            "cube",
            "--at",
            "2026-12-21T23:00",
            "--cities-dir",
            str(cities_dir),
            "--output-root",
            str(output_root),
        ],
    )
    assert night.exit_code == 1
