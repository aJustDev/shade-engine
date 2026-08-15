"""Shade tiles: state raster parity with the engine, PMTiles output, manifest."""

import io
import json
import re
import shutil
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import mercantile
import numpy as np
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
from shade_core.solar import SunPosition, sun_position
from shade_pipeline import budget
from shade_pipeline.budget import MemoryBudgetError
from shade_pipeline.cli import app
from shade_pipeline.cog import write_cog
from shade_pipeline.grid import transform_from_bbox
from shade_pipeline.shade_raster import (
    STATE_OUTSIDE,
    STATE_SHADE_BOTH,
    STATE_SHADE_BUILDING,
    STATE_SHADE_OTHER,
    STATE_SHADE_VEGETATION,
    STATE_SUN,
    compute_state_raster,
)
from shade_pipeline.tiles import (
    BUILDINGS_COLORS,
    BUILDINGS_TILES_FILENAME,
    CANOPY_COLORS,
    CANOPY_TILES_FILENAME,
    MANIFEST_FILENAME,
    PALETTE_STATES,
    SHADE_COLORS,
    bounds_wgs84,
    build_tiles,
    season_preset_instants,
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
    (ShadeState.SHADE, ShadeType.BOTH): STATE_SHADE_BOTH,
    (ShadeState.SHADE, None): STATE_SHADE_OTHER,
}


def _overwrite_band(artifact_dir: Path, filename: str, row: int, col: int, value: int) -> None:
    """Set one pixel across every band of a cube, in place on disk."""
    with rasterio.open(artifact_dir / filename) as src:
        cube = src.read()
        tags = src.tags()
        transform = src.transform
        crs = str(src.crs)
    cube[:, row, col] = value
    write_cog(artifact_dir / filename, cube, transform, crs, tags=tags)


def _shaded_by_building(artifact_dir: Path, sun: SunPosition) -> tuple[int, int]:
    """A pixel the artifacts put in a building's shadow; first one in raster order."""
    state = compute_state_raster(artifact_dir, sun)
    rows, cols = np.nonzero(state == STATE_SHADE_BUILDING)
    assert rows.size, "fixture has no building shade at this instant"
    return int(rows[0]), int(cols[0])


def _expected_state(result: ShadeResult) -> int:
    return _STATE_OF_RESULT[(result.state, result.shade_type)]


def _decode_tile(
    reader: Reader, crs: str, x: float, y: float, zoom: int
) -> tuple[Image.Image, int, int]:
    """The decoded PNG tile containing projected (x, y), plus the pixel offsets."""
    to_wgs84 = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
    lon, lat = to_wgs84.transform(x, y)
    tile = mercantile.tile(lon, lat, zoom)
    data = reader.get(tile.z, tile.x, tile.y)
    assert data is not None
    image = Image.open(io.BytesIO(data))
    merc_x, merc_y = mercantile.xy(lon, lat)
    tile_bounds = mercantile.xy_bounds(tile.x, tile.y, tile.z)
    resolution = (tile_bounds.right - tile_bounds.left) / image.width
    px = int((merc_x - tile_bounds.left) / resolution)
    py = int((tile_bounds.top - merc_y) / resolution)
    return image, px, py


def _rgba_at(reader: Reader, crs: str, x: float, y: float, zoom: int) -> tuple[int, int, int, int]:
    image, px, py = _decode_tile(reader, crs, x, y, zoom)
    pixel = image.convert("RGBA").getpixel((px, py))
    assert isinstance(pixel, tuple) and len(pixel) == 4
    return (pixel[0], pixel[1], pixel[2], pixel[3])


def _state_at(reader: Reader, crs: str, x: float, y: float, zoom: int) -> int:
    """The raw palette state code: distinguishes OUTSIDE from SUN (both alpha 0)."""
    image, px, py = _decode_tile(reader, crs, x, y, zoom)
    assert image.mode == "P"
    index = image.getpixel((px, py))
    assert isinstance(index, int)
    return PALETTE_STATES[index]


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
    """A pixel under the canopy mask is vegetation-shaded even where the horizon says sun."""
    target = tmp_path / "city"
    shutil.copytree(built_city, target)
    with rasterio.open(target / artifacts.CANOPY_FILENAME) as src:
        canopy = src.read()[0]
        transform = src.transform
        crs = str(src.crs)
    row, col = 5, 7  # far from the cube: sunlit at both golden instants
    # Written directly: at this flat pixel dsm == dtm, so the height-threshold
    # formula would say False. The file, not the formula, must drive the
    # override.
    canopy[row, col] = 1
    write_cog(target / artifacts.CANOPY_FILENAME, canopy, transform, crs)

    sun = sun_position(CORDOBA_LAT, CORDOBA_LON, SUMMER_NOON)
    state = compute_state_raster(target, sun)
    assert int(state[row, col]) == STATE_SHADE_VEGETATION


def test_state_raster_splits_vegetation_by_the_counterfactual(
    built_city: Path, tmp_path: Path
) -> None:
    """Same crown, two answers, decided by what the sky looks like without it.

    The blocker class is forced to VEGETATION on a pixel the cube shades, which
    is the Corredera case in miniature: a crown wins the argmax over a wall
    that closes the sky anyway.
    """
    sun = sun_position(CORDOBA_LAT, CORDOBA_LON, WINTER_NOON)
    row, col = _shaded_by_building(built_city, sun)

    both = tmp_path / "both"
    shutil.copytree(built_city, both)
    _overwrite_band(both, artifacts.BLOCKER_CLASS_FILENAME, row, col, Landcover.VEGETATION)
    assert int(compute_state_raster(both, sun)[row, col]) == STATE_SHADE_BOTH

    only_trees = tmp_path / "only-trees"
    shutil.copytree(built_city, only_trees)
    _overwrite_band(only_trees, artifacts.BLOCKER_CLASS_FILENAME, row, col, Landcover.VEGETATION)
    # Open the vegetation-free sky at that pixel: now the crown is all there is.
    _overwrite_band(only_trees, artifacts.HORIZON_NOVEG_FILENAME, row, col, 0)
    assert int(compute_state_raster(only_trees, sun)[row, col]) == STATE_SHADE_VEGETATION


def test_state_raster_canopy_inside_building_shade_is_both(
    built_city: Path, tmp_path: Path
) -> None:
    """Standing under a tree in a wall's shadow: felling it changes nothing."""
    sun = sun_position(CORDOBA_LAT, CORDOBA_LON, WINTER_NOON)
    row, col = _shaded_by_building(built_city, sun)
    target = tmp_path / "city"
    shutil.copytree(built_city, target)
    with rasterio.open(target / artifacts.CANOPY_FILENAME) as src:
        canopy = src.read()[0]
        transform = src.transform
        crs = str(src.crs)
    canopy[row, col] = 1
    write_cog(target / artifacts.CANOPY_FILENAME, canopy, transform, crs)

    assert int(compute_state_raster(target, sun)[row, col]) == STATE_SHADE_BOTH


def test_shade_that_holds_without_trees_goes_to_the_building_set(
    built_city: Path, tmp_path: Path
) -> None:
    """A BOTH pixel paints in the building set and stays out of the trees set.

    That is the whole point of the split: unchecking trees must not erase
    shade the buildings cast anyway.
    """
    sun = sun_position(CORDOBA_LAT, CORDOBA_LON, WINTER_NOON)
    row, col = _shaded_by_building(built_city, sun)
    target = tmp_path / "city"
    shutil.copytree(built_city, target)
    _overwrite_band(target, artifacts.BLOCKER_CLASS_FILENAME, row, col, Landcover.VEGETATION)

    metadata = artifacts.load_metadata(target)
    min_x, _, _, max_y = metadata.bbox
    point = (
        min_x + (col + 0.5) * metadata.resolution_m,
        max_y - (row + 0.5) * metadata.resolution_m,
    )

    tiles_dir = build_tiles(CUBE_CITY, target, [WINTER_NOON], min_zoom=14, max_zoom=18)
    manifest = json.loads((tiles_dir / MANIFEST_FILENAME).read_text(encoding="utf-8"))
    urls = manifest["instants"][0]["urls"]

    with open(tiles_dir / str(urls["building"]).split("?")[0], "rb") as handle:
        reader = Reader(MmapSource(handle))
        assert _rgba_at(reader, metadata.crs, *point, 18) == SHADE_COLORS[STATE_SHADE_BUILDING]
        assert _state_at(reader, metadata.crs, *point, 18) == STATE_SHADE_BUILDING
    with open(tiles_dir / str(urls["trees"]).split("?")[0], "rb") as handle:
        # Read at min_zoom: with nothing left for the trees set, the archive
        # holds only its blank overview tiles.
        reader = Reader(MmapSource(handle))
        assert _rgba_at(reader, metadata.crs, *point, 14)[3] == 0


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

    with open(path, "rb") as handle:
        reader = Reader(MmapSource(handle))
        header = reader.header()
        assert header["tile_type"] == TileType.PNG
        assert header["tile_compression"] == Compression.NONE

        # NEAR sits deep in the cube's winter shadow: building color.
        assert _rgba_at(reader, metadata.crs, *NEAR, 17) == SHADE_COLORS[STATE_SHADE_BUILDING]
        # A corner far from the cube is sunlit: fully transparent.
        sunny = (synthetic.UTM_ORIGIN[0] + 25.0, synthetic.UTM_ORIGIN[1] + 95.0)
        assert _rgba_at(reader, metadata.crs, *sunny, 17)[3] == 0


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
    assert manifest["schema_version"] == 2
    assert [entry["id"] for entry in instants] == ["20260621T1420", "20261221T1320"]
    summer, winter = instants
    assert summer["at"] == "2026-06-21T14:20"
    assert summer["utc_offset"] == "+02:00"  # CEST
    assert winter["utc_offset"] == "+01:00"  # CET: the preset spans DST changes

    canopy_url = manifest["canopy_url"]
    assert str(canopy_url).split("?")[0] == CANOPY_TILES_FILENAME
    assert (tiles_dir / CANOPY_TILES_FILENAME).exists()
    assert str(manifest["buildings_url"]).split("?")[0] == BUILDINGS_TILES_FILENAME
    assert (tiles_dir / BUILDINGS_TILES_FILENAME).exists()
    assert manifest["colors"]["buildings"] == "#3d4350"
    assert manifest["colors"]["shade"] == manifest["colors"]["shade_building"]
    # Legacy vegetation color = the static canopy's color (see build_tiles).
    assert manifest["colors"]["shade_vegetation"] == manifest["colors"]["canopy"]
    # Declination ladder: 7 rungs whose covers partition the whole year.
    ladder = manifest["ladder"]
    assert [rung["date"] for rung in ladder] == [
        "2026-02-07",
        "2026-03-01",
        "2026-03-21",
        "2026-04-10",
        "2026-05-04",
        "2026-06-21",
        "2026-12-21",
    ]
    covered = sum(
        (date.fromisoformat(b) - date.fromisoformat(a)).days + 1
        for rung in ladder
        for a, b in rung["covers"]
    )
    assert covered == 365
    for entry in instants:
        urls = entry["urls"]
        assert set(urls) == {"building", "trees", "vegetation"}
        # Legacy aliases: url = the building cast set; vegetation = canopy.
        assert entry["url"] == urls["building"]
        assert urls["vegetation"] == canopy_url
        for kind in ("building", "trees"):
            assert (tiles_dir / str(urls[kind]).split("?")[0]).exists()
        assert entry["sun"]["elevation_deg"] > 0


def test_roof_mask_and_canopy_split(built_city: Path, tmp_path: Path) -> None:
    """Roofs are OUTSIDE in the shade set; canopy pixels live in the static set only."""
    target = tmp_path / "city"
    shutil.copytree(built_city, target)
    # Plant a 3x3 canopy patch far from the cube (sunlit at the golden
    # instants), directly in canopy.tif: the split must follow the file.
    with rasterio.open(target / artifacts.CANOPY_FILENAME) as src:
        canopy = src.read()[0]
        canopy_transform = src.transform
        canopy_crs = str(src.crs)
    canopy[5:8, 5:8] = 1
    write_cog(target / artifacts.CANOPY_FILENAME, canopy, canopy_transform, canopy_crs)

    tiles_dir = build_tiles(CUBE_CITY, target, [WINTER_NOON], min_zoom=14, max_zoom=18)
    manifest = json.loads((tiles_dir / MANIFEST_FILENAME).read_text(encoding="utf-8"))
    urls = manifest["instants"][0]["urls"]
    metadata = artifacts.load_metadata(target)
    min_x, _, _, max_y = metadata.bbox
    cube_center = (
        synthetic.UTM_ORIGIN[0] + synthetic.CUBE_CENTER_X,
        synthetic.UTM_ORIGIN[1] + (synthetic.CUBE_Y[0] + synthetic.CUBE_Y[1]) / 2.0,
    )
    crown_center = (min_x + 6.5, max_y - 6.5)  # center of the planted patch

    with open(tiles_dir / str(urls["building"]).split("?")[0], "rb") as handle:
        reader = Reader(MmapSource(handle))
        # NEAR is street in the cube's winter shadow: shade color survives.
        assert _rgba_at(reader, metadata.crs, *NEAR, 16) == SHADE_COLORS[STATE_SHADE_BUILDING]
        # The cube interior (landcover BUILDING) is masked to OUTSIDE, not
        # SUN: both are transparent, but the palette index proves the roof
        # mask ran (unmasked it would be building shade, seen from inside).
        assert _state_at(reader, metadata.crs, *cube_center, 16) == STATE_OUTSIDE
        # Under-canopy pixels are excluded from the per-instant sets: SUN
        # (transparent), not vegetation shade.
        assert _state_at(reader, metadata.crs, *crown_center, 18) == STATE_SUN

    with open(tiles_dir / str(urls["trees"]).split("?")[0], "rb") as handle:
        reader = Reader(MmapSource(handle))
        # The planted crown sits on flat ground (no DSM bump), so it casts
        # nothing: the trees set holds only blank min_zoom tiles. Building
        # shade never leaks in, and the under-canopy crown stays out too.
        assert _rgba_at(reader, metadata.crs, *NEAR, 14)[3] == 0
        assert _state_at(reader, metadata.crs, *crown_center, 14) == STATE_SUN

    with open(tiles_dir / CANOPY_TILES_FILENAME, "rb") as handle:
        reader = Reader(MmapSource(handle))
        # The static set paints exactly the crowns, in the canopy green.
        expected = CANOPY_COLORS[STATE_SHADE_VEGETATION]
        assert _rgba_at(reader, metadata.crs, *crown_center, 18) == expected
        # Cast building shade never leaks into the canopy set.
        assert _rgba_at(reader, metadata.crs, *NEAR, 16)[3] == 0

    with open(tiles_dir / BUILDINGS_TILES_FILENAME, "rb") as handle:
        reader = Reader(MmapSource(handle))
        # The LiDAR footprint paints the cube itself and nothing else.
        expected = BUILDINGS_COLORS[STATE_SHADE_BUILDING]
        assert _rgba_at(reader, metadata.crs, *cube_center, 16) == expected
        assert _rgba_at(reader, metadata.crs, *NEAR, 16)[3] == 0


def test_declination_ladder_preset() -> None:
    """83 hourly instants over 7 canonical declination dates."""
    instants = season_preset_instants(ZoneInfo("Europe/Madrid"))
    assert len(instants) == 83
    assert len({when.date() for when in instants}) == 7
    june = [when for when in instants if when.month == 6]
    assert [when.hour for when in june] == list(range(8, 22))
    december = [when for when in instants if when.month == 12]
    assert [when.hour for when in december] == list(range(9, 18))


def _pyramids(tiles_dir: Path) -> dict[str, bytes]:
    return {path.name: path.read_bytes() for path in sorted(tiles_dir.glob("*.pmtiles"))}


def _stable_manifest(tiles_dir: Path) -> dict[str, object]:
    """The manifest minus what carries the clock on purpose.

    ``generated_at`` and the ``?v=`` cache-buster differ between any two runs
    by design; everything else has to match.
    """
    manifest = json.loads((tiles_dir / MANIFEST_FILENAME).read_text(encoding="utf-8"))
    del manifest["generated_at"]
    stable: dict[str, object] = json.loads(re.sub(r"\?v=\d+", "?v=X", json.dumps(manifest)))
    return stable


@pytest.mark.parametrize("workers", [2, 5])
def test_parallel_render_matches_serial(built_city: Path, tmp_path: Path, workers: int) -> None:
    """The exit criterion: workers change the schedule, never a byte of output.

    Five workers against three units also puts more processes than work in the
    pool, which is where an out-of-order manifest would show up.
    """
    instants = [WINTER_NOON, WINTER_NOON.replace(hour=12), SUMMER_NOON]
    serial, parallel = (tmp_path / name for name in ("serial", "parallel"))
    for target in (serial, parallel):
        shutil.copytree(built_city, target)
    serial_dir = build_tiles(CUBE_CITY, serial, instants, min_zoom=14, max_zoom=16)
    parallel_dir = build_tiles(
        CUBE_CITY, parallel, instants, min_zoom=14, max_zoom=16, workers=workers
    )

    assert _pyramids(parallel_dir) == _pyramids(serial_dir)
    assert _stable_manifest(parallel_dir) == _stable_manifest(serial_dir)


def test_manifest_stays_chronological(built_city: Path, tmp_path: Path) -> None:
    """Units land out of order; the client reads this list as a timeline."""
    target = tmp_path / "city"
    shutil.copytree(built_city, target)
    instants = [SUMMER_NOON, WINTER_NOON, WINTER_NOON.replace(hour=12)]
    tiles_dir = build_tiles(CUBE_CITY, target, instants, min_zoom=14, max_zoom=15, workers=3)

    manifest = json.loads((tiles_dir / MANIFEST_FILENAME).read_text(encoding="utf-8"))
    ids = [entry["id"] for entry in manifest["instants"]]
    assert ids == sorted(ids)
    assert ids == [f"{when:%Y%m%dT%H%M}" for when in sorted(instants)]


def test_failed_unit_leaves_no_manifest(built_city: Path, tmp_path: Path) -> None:
    """A unit that blows up ends the render before anything is announced.

    A manifest promising instants whose .pmtiles nobody wrote is worse than no
    manifest: the client would 404 on every one of them. Here the canopy
    artifact goes missing after the parent has read the metadata, so the
    failure happens inside a worker -- which is the case the parent cannot see
    coming. (The sibling failure, a worker killed outright, arrives as
    ``BrokenProcessPool`` and is translated the same three lines as in the
    sweep, where ``fork`` makes it testable.)
    """
    target = tmp_path / "city"
    shutil.copytree(built_city, target)
    (target / artifacts.CANOPY_FILENAME).unlink()
    # OSError, not a narrower class: whichever unit touches the missing file
    # first decides whether it arrives from rasterio or from the loader, and
    # the contract under test is the manifest, not the spelling of the error.
    with pytest.raises(OSError):
        build_tiles(CUBE_CITY, target, [WINTER_NOON], min_zoom=14, max_zoom=15, workers=2)
    assert not (target / "tiles" / MANIFEST_FILENAME).exists()


def test_render_refuses_workers_that_do_not_fit(
    built_city: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The guardrail fires before the pool exists, naming what would fit."""
    monkeypatch.setattr(budget, "available_bytes", lambda: 2 * budget.TILES_BASE_BYTES)
    target = tmp_path / "city"
    shutil.copytree(built_city, target)
    with pytest.raises(MemoryBudgetError, match="--workers 1 or fewer"):
        build_tiles(CUBE_CITY, target, [WINTER_NOON], min_zoom=14, max_zoom=15, workers=8)


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
    assert "shade-20261221T1320-building.pmtiles" in result.output
    assert "shade-20261221T1320-trees.pmtiles" in result.output
    assert CANOPY_TILES_FILENAME in result.output
    assert "state raster in" in result.output
    assert "tiles done in" in result.output
    assert "tiles written to" in result.output
    tiles_dir = output_root / "cube" / "v1" / "tiles"
    assert (tiles_dir / "shade-20261221T1320-building.pmtiles").exists()
    assert (tiles_dir / "shade-20261221T1320-trees.pmtiles").exists()
    assert (tiles_dir / CANOPY_TILES_FILENAME).exists()
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
