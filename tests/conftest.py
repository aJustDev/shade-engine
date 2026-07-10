import pytest

import synthetic
from shade_core.horizon import HorizonGrid, compute_horizon_reference


@pytest.fixture(scope="session")
def cube_grid() -> HorizonGrid:
    """Horizon of the 20 m cube scene; computed once, reused across test files."""
    dsm, dtm = synthetic.cube_scene()
    return compute_horizon_reference(dsm, dtm, synthetic.RESOLUTION_M, max_distance_m=80.0)
