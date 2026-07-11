"""SceneReader answers point queries identically to the whole-array loader."""

import shutil
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pytest

import synthetic
from shade_core.artifacts import LANDCOVER_FILENAME, SceneReader, load_scene
from shade_core.shade import ShadeScene, is_shaded, shade_timeline
from shade_core.solar import SunPosition, sun_position
from shade_pipeline.cog import write_cog
from shade_pipeline.grid import transform_from_bbox

CORDOBA_LAT, CORDOBA_LON = 37.88, -4.78
WINTER_NOON = datetime(2026, 12, 21, 13, 20, tzinfo=ZoneInfo("Europe/Madrid"))
SUMMER_NOON = datetime(2026, 6, 21, 14, 20, tzinfo=ZoneInfo("Europe/Madrid"))

X0, Y0 = synthetic.UTM_ORIGIN
X_MIN, Y_MIN = X0 + 20.0, Y0 + 20.0
X_MAX, Y_MAX = X0 + 100.0, Y0 + 100.0

# Interior, block-boundary (default block 64: the column boundary of the 80x80
# grid falls at x = X_MIN + 64), exact corner, last row/col, west of the cube.
POINTS = [
    (X0 + synthetic.QUERY_X, Y0 + 60.0),  # the golden NEAR point
    (X_MIN + 64.0, Y_MIN + 40.0),  # exactly on the block boundary
    (X_MIN + 64.0 - 1e-3, Y_MIN + 40.0),
    (X_MIN + 64.0 + 1e-3, Y_MIN + 40.0),
    (X_MIN, Y_MAX),  # exact top-left corner
    (X_MIN + 79.9, Y_MIN + 0.1),  # bottom-right pixel
    (X0 + 40.5, Y0 + 40.5),  # west of the cube
]

# Low suns around the compass exercise every flank of the cube; the golden
# noons are added per-test (they need the ephemeris).
SUNS = [SunPosition(azimuth_deg=float(az), elevation_deg=10.0) for az in range(0, 360, 45)]


@pytest.fixture(scope="module")
def full_scene(built_city: Path) -> ShadeScene:
    return load_scene(built_city)


@pytest.fixture
def reader(built_city: Path) -> Iterator[SceneReader]:
    with SceneReader(built_city) as instance:
        yield instance


def test_parity_with_full_scene(reader: SceneReader, full_scene: ShadeScene) -> None:
    suns = [
        *SUNS,
        sun_position(CORDOBA_LAT, CORDOBA_LON, WINTER_NOON),
        sun_position(CORDOBA_LAT, CORDOBA_LON, SUMMER_NOON),
    ]
    for x, y in POINTS:
        scene, center_x, center_y = reader.scene_for(x, y)
        for sun in suns:
            expected = is_shaded(full_scene, center_x, center_y, sun)
            assert is_shaded(scene, center_x, center_y, sun) == expected, (x, y, sun)


def test_pixel_center_snap_is_semantically_free(
    reader: SceneReader, full_scene: ShadeScene
) -> None:
    """Querying the pixel center equals querying the raw point (nearest sampling)."""
    for x, y in POINTS:
        _, center_x, center_y = reader.scene_for(x, y)
        for sun in SUNS:
            assert is_shaded(full_scene, x, y, sun) == is_shaded(
                full_scene, center_x, center_y, sun
            ), (x, y, sun)


def test_timeline_parity(reader: SceneReader, full_scene: ShadeScene) -> None:
    x, y = POINTS[0]
    scene, center_x, center_y = reader.scene_for(x, y)
    tz = ZoneInfo("Europe/Madrid")
    args = (center_x, center_y, CORDOBA_LAT, CORDOBA_LON, WINTER_NOON.date(), tz)
    assert shade_timeline(scene, *args) == shade_timeline(full_scene, *args)


def test_lru_stays_bounded_and_correct(built_city: Path, full_scene: ShadeScene) -> None:
    point_a = (X_MIN + 5.0, Y_MAX - 5.0)
    point_b = (X_MIN + 70.0, Y_MIN + 5.0)
    sun = SunPosition(azimuth_deg=180.0, elevation_deg=10.0)
    with SceneReader(built_city, block_size=16, max_blocks=1) as reader:
        for x, y in (point_a, point_b, point_a, point_b):
            scene, center_x, center_y = reader.scene_for(x, y)
            assert reader.cached_blocks == 1
            expected = is_shaded(full_scene, center_x, center_y, sun)
            assert is_shaded(scene, center_x, center_y, sun) == expected


def test_blocks_accumulate_per_block(reader: SceneReader) -> None:
    reader.scene_for(X_MIN + 5.0, Y_MAX - 5.0)
    reader.scene_for(X_MIN + 5.0, Y_MAX - 6.0)  # same block: cache hit
    assert reader.cached_blocks == 1
    reader.scene_for(X_MIN + 70.0, Y_MIN + 5.0)
    assert reader.cached_blocks == 2


def test_outside_grid(reader: SceneReader) -> None:
    for x, y in [
        (X_MAX, Y_MIN + 40.0),  # x == max_x is already outside (half-open)
        (X_MIN - 1e-3, Y_MIN + 40.0),
        (X_MIN + 40.0, Y_MIN),  # y == min_y is outside too
    ]:
        assert not reader.contains(x, y)
        with pytest.raises(ValueError, match="outside"):
            reader.scene_for(x, y)
    assert reader.contains(X_MIN, Y_MAX)
    assert not reader.contains(float("inf"), Y_MIN + 40.0)
    assert not reader.contains(float("nan"), float("nan"))


def test_metadata_is_loaded(reader: SceneReader) -> None:
    assert reader.metadata.city_id == "cube"
    assert reader.metadata.horizon.sectors == 64


def test_mixed_georeference_raises(built_city: Path, tmp_path: Path) -> None:
    tampered = tmp_path / "v1"
    shutil.copytree(built_city, tampered)
    shifted = transform_from_bbox((X_MIN + 5.0, Y_MIN, X_MAX + 5.0, Y_MAX), 1.0)
    landcover = np.zeros((80, 80), dtype=np.uint8)
    write_cog(tampered / LANDCOVER_FILENAME, landcover, shifted, "EPSG:25830")
    with pytest.raises(ValueError, match="georeference"):
        SceneReader(tampered)
