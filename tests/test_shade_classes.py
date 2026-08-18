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
from shade_core.shade import (
    OPAQUE_CANOPY_CAVEAT,
    Landcover,
    ShadeScene,
    ShadeState,
    ShadeType,
    caveats_for,
    is_shaded,
)
from shade_core.solar import SunPosition, sun_positions_for_day
from shade_pipeline.horizon import HorizonParams, compute_horizon_tiled

CORDOBA_LAT, CORDOBA_LON = 37.88, -4.78
NEAR = (synthetic.QUERY_X, synthetic.CUBE_NORTH_WALL_Y + 10.0)
WEST = (40.5, 40.5)  # west of the cube: shaded in the morning instead of noon

MIDDAY = SunPosition(azimuth_deg=180.0, elevation_deg=30.0)
POINT = (1.5, 1.5)


def _flat_grid(angle_deg: float) -> HorizonGrid:
    """A 3x3 scene whose skyline is the same angle in every sector.

    Uniform on purpose: with no variation between sectors the azimuth
    interpolation is a no-op, so each test states exactly one thing.
    """
    return HorizonGrid(
        angles_deg=np.full((64, 3, 3), angle_deg, dtype=np.float32),
        resolution_m=1.0,
        origin=(0.0, 3.0),
    )


def _crown_scene(
    horizon_deg: float, noveg_deg: float | None, *, canopy: bool = False
) -> ShadeScene:
    """A pixel whose blocker is a crown, over a skyline of the caller's choosing."""
    return ShadeScene(
        horizon=_flat_grid(horizon_deg),
        sector_classes=np.full((64, 3, 3), Landcover.VEGETATION, dtype=np.uint8),
        canopy=np.full((3, 3), canopy, dtype=np.bool_),
        horizon_noveg=None if noveg_deg is None else _flat_grid(noveg_deg),
    )


def test_crown_over_a_closed_sky_is_both() -> None:
    """The wall behind the tree would shade this pixel anyway."""
    result = is_shaded(_crown_scene(60.0, 40.0), *POINT, MIDDAY)
    assert result.state is ShadeState.SHADE
    assert result.shade_type is ShadeType.BOTH


def test_crown_over_an_open_sky_is_vegetation() -> None:
    """Fell this one and the sun reaches the ground."""
    result = is_shaded(_crown_scene(60.0, 20.0), *POINT, MIDDAY)
    assert result.shade_type is ShadeType.VEGETATION


def test_without_the_second_horizon_nothing_is_both() -> None:
    """Artifacts predating the cube keep answering exactly what they used to."""
    result = is_shaded(_crown_scene(60.0, None), *POINT, MIDDAY)
    assert result.shade_type is ShadeType.VEGETATION


def test_under_canopy_inside_a_shadow_is_both() -> None:
    """Canopy short-circuits the horizon, but not the counterfactual."""
    result = is_shaded(_crown_scene(0.0, 40.0, canopy=True), *POINT, MIDDAY)
    assert result.state is ShadeState.SHADE
    assert result.shade_type is ShadeType.BOTH


def test_under_canopy_in_the_open_is_vegetation() -> None:
    result = is_shaded(_crown_scene(0.0, 0.0, canopy=True), *POINT, MIDDAY)
    assert result.shade_type is ShadeType.VEGETATION


def test_the_opaque_canopy_caveat_rides_on_vegetation_shade() -> None:
    """Where the crowns are the whole reason, the caller hears about winter."""
    assert caveats_for([ShadeType.VEGETATION]) == [OPAQUE_CANOPY_CAVEAT]


def test_a_shade_the_skyline_would_hold_carries_no_caveat() -> None:
    """BOTH means a wall closes the sky anyway, so bare branches change nothing.

    This is the case that decides whether the field is worth reading: a caveat
    attached to verdicts it cannot alter is a caveat nobody reads.
    """
    assert caveats_for([ShadeType.BOTH]) == []
    assert caveats_for([ShadeType.BUILDING]) == []
    assert caveats_for([None]) == []


def test_one_vegetal_interval_is_enough_for_a_whole_day() -> None:
    """A day is caveated if any of its hours is, which is what a timeline asks."""
    day = [ShadeType.BUILDING, None, ShadeType.VEGETATION, ShadeType.BOTH]
    assert caveats_for(day) == [OPAQUE_CANOPY_CAVEAT]
    assert caveats_for([]) == []


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
