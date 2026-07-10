import pytest

import synthetic
from shade_core.horizon import HorizonGrid, compute_horizon_reference
from shade_core.shade import ShadeScene


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
