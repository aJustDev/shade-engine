from pathlib import Path

import pytest

import laz_fixture
import synthetic
from shade_core.config import CityConfig
from shade_core.horizon import HorizonGrid, compute_horizon_reference
from shade_core.shade import ShadeScene
from shade_pipeline.build import build_city
from shade_pipeline.sources import LocalDirectory


@pytest.fixture(scope="session")
def cube_grid() -> HorizonGrid:
    """Horizon of the 20 m cube scene; computed once, reused across test files."""
    dsm, dtm = synthetic.cube_scene()
    return compute_horizon_reference(dsm, dtm, synthetic.RESOLUTION_M, max_distance_m=80.0)


@pytest.fixture(scope="session")
def cube_shade_scene(cube_grid: HorizonGrid) -> ShadeScene:
    dsm, dtm = synthetic.cube_scene()
    return ShadeScene(horizon=cube_grid, landcover=synthetic.cube_landcover(), dsm=dsm, dtm=dtm)


@pytest.fixture(scope="session")
def tree_shade_scene() -> ShadeScene:
    dsm, dtm, landcover, canopy = synthetic.tree_scene()
    grid = compute_horizon_reference(dsm, dtm, synthetic.RESOLUTION_M, max_distance_m=40.0)
    return ShadeScene(horizon=grid, landcover=landcover, canopy=canopy, dsm=dsm, dtm=dtm)


# The built-city fixture config, at real Cordoba UTM coordinates so lat/lon
# queries through the API resolve into the scene. Single source of truth for
# every test that needs the city's georeference or metadata.
CUBE_CITY = CityConfig(
    id="cube",
    name="Cube",
    country="ES",
    timezone="Europe/Madrid",
    crs="EPSG:25830",
    bbox=(
        synthetic.UTM_ORIGIN[0] + 20.0,
        synthetic.UTM_ORIGIN[1] + 20.0,
        synthetic.UTM_ORIGIN[0] + 100.0,
        synthetic.UTM_ORIGIN[1] + 100.0,
    ),
    resolution_m=1.0,
    horizon_sectors=64,
    horizon_max_distance_m=20.0,
    attribution=["Synthetic LiDAR (test fixture)"],
)


@pytest.fixture(scope="session")
def built_city(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Full pipeline run over the cube LAZ; returns the artifact directory.

    The city bbox is the inner 80x80 of the synthetic scene and max_distance
    is 20 m, so the padded bbox is exactly the full 120x120 scene: coverage
    passes with nothing to spare, exercising the padding arithmetic end to
    end. The golden NEAR point stays inside the inner bbox.
    """
    root = tmp_path_factory.mktemp("built_city")
    lidar_dir = root / "lidar"
    lidar_dir.mkdir()
    laz_fixture.write_cube_laz(lidar_dir / "cube.laz", origin=synthetic.UTM_ORIGIN)
    return build_city(CUBE_CITY, LocalDirectory(lidar_dir), root / "data")
