"""Horizon grid against analytic geometry.

Oracle: from a point d meters north of a wall of height h, the horizon angle
toward the wall (azimuth 180) is atan((h - 1.6) / d); away from it, 0.
Tolerances absorb the half-pixel discretization of the reference sweep.
"""

import math

import numpy as np
import pytest

import synthetic
from shade_core.horizon import HorizonGrid, compute_horizon_reference

EYE = synthetic.CUBE_HEIGHT - 1.6  # wall top above observer eye level


def wall_angle(distance: float) -> float:
    return math.degrees(math.atan2(EYE, distance))


def test_south_sector_sees_the_wall(cube_grid: HorizonGrid) -> None:
    x, y = synthetic.CUBE_CENTER_X, synthetic.CUBE_NORTH_WALL_Y + 10
    assert cube_grid.horizon_at(x, y, 180.0) == pytest.approx(wall_angle(10), abs=2.0)


def test_far_point_sees_lower_wall(cube_grid: HorizonGrid) -> None:
    x, y = synthetic.CUBE_CENTER_X, synthetic.CUBE_NORTH_WALL_Y + 60
    assert cube_grid.horizon_at(x, y, 180.0) == pytest.approx(wall_angle(60), abs=1.0)


def test_north_sector_is_flat(cube_grid: HorizonGrid) -> None:
    x, y = synthetic.CUBE_CENTER_X, synthetic.CUBE_NORTH_WALL_Y + 10
    assert cube_grid.horizon_at(x, y, 0.0) == pytest.approx(0.0, abs=0.1)


def test_max_distance_truncates_the_horizon() -> None:
    dsm, dtm = synthetic.cube_scene()
    truncated = compute_horizon_reference(
        dsm, dtm, synthetic.RESOLUTION_M, sectors=8, max_distance_m=30.0
    )
    x, y = synthetic.CUBE_CENTER_X, synthetic.CUBE_NORTH_WALL_Y + 60
    assert truncated.horizon_at(x, y, 180.0) == 0.0


def test_azimuth_interpolation_wraps_around() -> None:
    # 4 sectors at 0/90/180/270 deg with angles 10/20/30/40: azimuth 315 sits
    # halfway between sector 3 (40) and sector 0 (10).
    angles = np.array([10.0, 20.0, 30.0, 40.0], dtype=np.float32).reshape(4, 1, 1)
    grid = HorizonGrid(angles_deg=angles, resolution_m=1.0, origin=(0.0, 1.0))
    assert grid.horizon_at(0.5, 0.5, 315.0) == pytest.approx(25.0)
    assert grid.horizon_at(0.5, 0.5, 90.0) == pytest.approx(20.0)


def test_point_outside_grid_rejected(cube_grid: HorizonGrid) -> None:
    with pytest.raises(ValueError, match="outside"):
        cube_grid.profile_at(-5.0, 60.0)
