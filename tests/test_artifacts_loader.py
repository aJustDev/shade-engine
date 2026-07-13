"""Core reads back what the pipeline modules write."""

import json
import shutil
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pytest
from numpy.testing import assert_allclose, assert_array_equal

import synthetic
from shade_core import artifacts
from shade_core.horizon import compute_horizon_reference
from shade_core.shade import ShadeState, ShadeType, is_shaded
from shade_core.solar import sun_position
from shade_pipeline.cog import write_cog
from shade_pipeline.grid import transform_from_bbox
from shade_pipeline.horizon import HorizonParams, compute_horizon_tiled

CORDOBA_LAT, CORDOBA_LON = 37.88, -4.78
NEAR = (synthetic.QUERY_X, synthetic.CUBE_NORTH_WALL_Y + 10.0)
WINTER_NOON = datetime(2026, 12, 21, 13, 20, tzinfo=ZoneInfo("Europe/Madrid"))
SUMMER_NOON = datetime(2026, 6, 21, 14, 20, tzinfo=ZoneInfo("Europe/Madrid"))

MAX_DISTANCE_M = 30.0
METADATA = {
    "schema_version": 1,
    "city_id": "cube",
    "artifact_version": "v1",
    "built_at": "2026-07-11T00:00:00Z",
    "crs": "EPSG:25830",
    "bbox": [0.0, 0.0, 120.0, 120.0],
    "resolution_m": 1.0,
    "horizon": {
        "sectors": 64,
        "max_distance_m": MAX_DISTANCE_M,
        "observer_height_m": 1.6,
        "angle_max_deg": 90.0,
        "step_mode": "exact",
        "tile_size": 512,
    },
    "landcover_classes": {"ground": 0, "vegetation": 1, "building": 2},
    "no_blocker_value": 255,
    "software": {"shade-pipeline": "0.1.0"},
    "inputs": [{"name": "cube.laz", "points": 14400}],
    "attribution": ["LiDAR PNOA (c) Instituto Geografico Nacional de Espana"],
}


@pytest.fixture(scope="module")
def artifact_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A hand-assembled artifact directory over the cube scene."""
    directory = tmp_path_factory.mktemp("artifacts")
    dsm, dtm = synthetic.cube_scene()
    landcover = synthetic.cube_landcover()
    result = compute_horizon_tiled(
        dsm, dtm, landcover, 1.0, HorizonParams(max_distance_m=MAX_DISTANCE_M)
    )
    transform = transform_from_bbox((0.0, 0.0, 120.0, 120.0), 1.0)
    crs = "EPSG:25830"
    write_cog(directory / artifacts.DSM_FILENAME, dsm.astype(np.float32), transform, crs)
    write_cog(directory / artifacts.DTM_FILENAME, dtm.astype(np.float32), transform, crs)
    write_cog(directory / artifacts.LANDCOVER_FILENAME, landcover, transform, crs)
    write_cog(
        directory / artifacts.HORIZON_FILENAME,
        result.angles_q,
        transform,
        crs,
        tags={"angle_max_deg": "90.0", "sectors": "64"},
    )
    write_cog(
        directory / artifacts.BLOCKER_CLASS_FILENAME,
        result.blocker_class,
        transform,
        crs,
        tags={"no_blocker": "255"},
    )
    # Canopy deliberately decoupled from the landcover (one pixel, far from
    # the golden queries): proves the loaders read the artifact, not the old
    # landcover-derived formula.
    canopy = np.zeros((120, 120), dtype=np.uint8)
    canopy[5, 7] = 1
    write_cog(directory / artifacts.CANOPY_FILENAME, canopy, transform, crs)
    (directory / artifacts.METADATA_FILENAME).write_text(json.dumps(METADATA))
    return directory


def test_load_horizon_matches_reference(artifact_dir: Path) -> None:
    grid = artifacts.load_horizon(artifact_dir / artifacts.HORIZON_FILENAME)
    dsm, dtm = synthetic.cube_scene()
    reference = compute_horizon_reference(dsm, dtm, 1.0, max_distance_m=MAX_DISTANCE_M)
    assert grid.sectors == 64
    assert grid.resolution_m == 1.0
    assert grid.origin == (0.0, 120.0)
    assert_allclose(grid.angles_deg, reference.angles_deg, atol=90.0 / 255.0 / 2.0 + 1e-4)


def test_load_scene_arrays(artifact_dir: Path) -> None:
    scene = artifacts.load_scene(artifact_dir)
    assert scene.landcover is not None and scene.canopy is not None
    assert scene.dsm is not None and scene.dtm is not None
    assert scene.sector_classes is not None
    assert scene.dsm.shape == scene.dtm.shape == scene.landcover.shape == (120, 120)
    assert scene.sector_classes.shape == (64, 120, 120)
    assert scene.dsm.dtype == np.float64 and scene.landcover.dtype == np.uint8
    assert scene.observer_height_m == 1.6
    expected_canopy = np.zeros((120, 120), dtype=bool)
    expected_canopy[5, 7] = True
    assert_array_equal(scene.canopy, expected_canopy)
    assert_array_equal(scene.landcover, synthetic.cube_landcover())


def test_load_metadata(artifact_dir: Path) -> None:
    metadata = artifacts.load_metadata(artifact_dir)
    assert metadata.city_id == "cube"
    assert metadata.horizon.sectors == 64
    assert metadata.attribution


def test_golden_queries_on_loaded_scene(artifact_dir: Path) -> None:
    """The spec's golden verdicts survive the full disk roundtrip."""
    scene = artifacts.load_scene(artifact_dir)
    winter = is_shaded(scene, *NEAR, sun_position(CORDOBA_LAT, CORDOBA_LON, WINTER_NOON))
    assert winter.state is ShadeState.SHADE
    assert winter.shade_type is ShadeType.BUILDING
    summer = is_shaded(scene, *NEAR, sun_position(CORDOBA_LAT, CORDOBA_LON, SUMMER_NOON))
    assert summer.state is ShadeState.SUN


def test_mismatched_georeference_raises(artifact_dir: Path, tmp_path: Path) -> None:
    tampered = tmp_path / "v1"
    shutil.copytree(artifact_dir, tampered)
    shifted = transform_from_bbox((5.0, 0.0, 125.0, 120.0), 1.0)
    dsm, _ = synthetic.cube_scene()
    write_cog(tampered / artifacts.DSM_FILENAME, dsm.astype(np.float32), shifted, "EPSG:25830")
    with pytest.raises(ValueError, match="georeference"):
        artifacts.load_scene(tampered)


def test_missing_canopy_raises(artifact_dir: Path, tmp_path: Path) -> None:
    """Pre-canopy artifact dirs fail loudly, naming the backfill command."""
    stripped = tmp_path / "v1"
    shutil.copytree(artifact_dir, stripped)
    (stripped / artifacts.CANOPY_FILENAME).unlink()
    with pytest.raises(FileNotFoundError, match="shade-engine canopy"):
        artifacts.load_scene(stripped)
