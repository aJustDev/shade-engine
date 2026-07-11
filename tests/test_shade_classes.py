"""The sector-class strategy must agree with the reference ray-march.

Both scenes share the *same* (quantized, dequantized) horizon grid so every
shade verdict is identical by construction; what is under test is that the
blocker-class artifact attributes each shaded sample to the same shade type
the ray-march derives from the elevation models.
"""

from datetime import date

import numpy as np
import pytest

import synthetic
from shade_core.horizon import HorizonGrid
from shade_core.shade import ShadeScene, is_shaded
from shade_core.solar import sun_positions_for_day
from shade_pipeline.horizon import HorizonParams, compute_horizon_tiled

CORDOBA_LAT, CORDOBA_LON = 37.88, -4.78
NEAR = (synthetic.QUERY_X, synthetic.CUBE_NORTH_WALL_Y + 10.0)
WEST = (40.5, 40.5)  # west of the cube: shaded in the morning instead of noon


@pytest.fixture(scope="module")
def paired_scenes() -> tuple[ShadeScene, ShadeScene]:
    dsm, dtm = synthetic.cube_scene()
    landcover = synthetic.cube_landcover()
    result = compute_horizon_tiled(dsm, dtm, landcover, 1.0, HorizonParams(max_distance_m=30.0))
    grid = HorizonGrid(
        angles_deg=result.angles_q.astype(np.float32) * np.float32(90.0 / 255.0),
        resolution_m=1.0,
        origin=(0.0, 120.0),
    )
    by_classes = ShadeScene(horizon=grid, sector_classes=result.blocker_class)
    by_march = ShadeScene(horizon=grid, landcover=landcover, dsm=dsm, dtm=dtm)
    return by_classes, by_march


@pytest.mark.parametrize("day", [date(2026, 12, 21), date(2026, 6, 21)])
@pytest.mark.parametrize("point", [NEAR, WEST])
def test_classification_parity(
    paired_scenes: tuple[ShadeScene, ShadeScene],
    day: date,
    point: tuple[float, float],
) -> None:
    by_classes, by_march = paired_scenes
    x, y = point
    for _, sun in sun_positions_for_day(CORDOBA_LAT, CORDOBA_LON, day, "Europe/Madrid", 15):
        if not sun.is_up:
            continue
        assert is_shaded(by_classes, x, y, sun) == is_shaded(by_march, x, y, sun)
