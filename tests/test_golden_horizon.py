"""Golden horizon angles: literal degrees worked out by hand from the geometry.

Every other test of the sweep pins *agreement* -- the tiled block against the
oracle, one tile size against another, the city translated 400 m up against
itself. Agreement tests move with the engine: when phase 13 changed which cells
the ray visits and at what distance, the sweep and the oracle moved together and
the suite stayed green through a change of geometric convention. Four times in
one phase a defect hid behind consistency (shade-docs:
milestones/fase-13-deuda-del-motor.md).

So this file pins *values*. Four obstacles of known height around one observer,
each in a sector nothing else reaches, and the angle each one subtends written
as a literal computed from ``atan(dz / d)`` with the convention named beside it
-- including what the rejected conventions would have produced. A change of
convention cannot pass here in silence: it has to come and edit these numbers.

The scene sits at **367 m of elevation**, not at zero, because a flat fixture at
z=0 is blind to anything that depends on the height datum -- which is exactly
how the observer ended up at 1.600006 m city-wide until [[ADR-026]]
(shade-docs: learning/precision-de-alturas.md).

Conventions under test, all from [[ADR-027]]:

- a DSM cell is a 1x1 m **column**, and it blocks from the distance the ray
  **enters** it -- not from its centre, and not from ``hypot - 0.5``;
- the ray is **traversed**, so a cell a rounded half-pixel schedule used to skip
  is seen;
- the observer's eye is at DTM + 1.6 m, the obstacle's top is its DSM value.
"""

import math

import numpy as np
import pytest

from shade_core.shade import NO_BLOCKER, Landcover
from shade_pipeline.horizon import (
    ANGLE_MAX_DEG,
    HorizonParams,
    compute_horizon_block,
    height_datum,
    quantized_horizon_block,
)

GROUND_M = 367.0
"""Montilla's own ballpark: high enough that float32 ulps are visible."""

EYE_M = 1.6
OBSERVER_Z = GROUND_M + EYE_M
PAD = 25
"""Room for a 20 m ray in every direction."""

SIZE = 2 * PAD + 1
CENTRE = PAD
INNER = (CENTRE, CENTRE + 1, CENTRE, CENTRE + 1)
MAX_DISTANCE_M = 20.0
RESOLUTION_M = 1.0

# --- the four obstacles, each alone in its sector ------------------------------
#
# (d_row, d_col) from the observer; rows grow south, columns east. The entry
# distance is the ray's, from shade_core.raycast.ray_cells; on an axis it is
# simply k - 0.5, because the first cell boundary is half a pixel away.

SOUTH_TOWER = (10, 0)
SOUTH_HEIGHT_M = 20.0
SOUTH_SECTOR = 32  # azimuth 180 deg, the only sector that crosses (10, 0)
SOUTH_ENTRY_M = 9.5  # centre would be 10.0

EAST_TOWER = (0, 7)
EAST_HEIGHT_M = 12.0
EAST_SECTOR = 16  # azimuth 90 deg
EAST_ENTRY_M = 6.5  # centre would be 7.0; an odd cell, where the old
# half-to-even rounding used to land half a pixel long

WEST_TREE = (0, -6)
WEST_HEIGHT_M = 8.0
WEST_SECTOR = 48  # azimuth 270 deg
WEST_ENTRY_M = 5.5

# The interesting one. Sector 4 (22.5 deg) traverses (-4, +1) at 3.788373 m,
# a cell the old half-pixel-and-round sampling never sampled at all: it is one
# of the 15.2% of crossed cells that convention skipped.
THIN_WALL = (-4, 1)
WALL_HEIGHT_M = 12.0
WALL_SECTOR = 4
WALL_ENTRY_M = 3.788373  # 0.5 / cos(22.5 deg) + 3 / cos(22.5 deg)
WALL_CENTRE_M = 4.123106  # hypot(4, 1)
WALL_NEAR_EDGE_M = 3.623106  # hypot(4, 1) - 0.5

# --- the golden angles ---------------------------------------------------------
#
# atan((ground + height - observer_z) / entry), in degrees. Written out so a
# reader can check them with a calculator and no engine.

SOUTH_ANGLE_DEG = 62.692496  # atan(18.4 / 9.5); from the centre: 61.476881
EAST_ANGLE_DEG = 57.994617  # atan(10.4 / 6.5); from the centre: 56.056413
WEST_ANGLE_DEG = 49.325060  # atan(6.4 / 5.5)
WALL_ANGLE_DEG = 69.985006  # atan(10.4 / 3.788373)
WALL_ANGLE_FROM_CENTRE_DEG = 68.374026  # atan(10.4 / 4.123106)
WALL_ANGLE_FROM_NEAR_EDGE_DEG = 70.792911  # atan(10.4 / 3.623106)

# Quantized: round(angle * 255 / 90). Pinned as integers because that is what
# ships in the COG, and because a uint8 comparison is exactly the test that let
# a 1e-05 degree bias through in S2 -- here the values are far enough apart that
# it cannot.
SOUTH_Q = 178  # from the centre: 174
EAST_Q = 164  # from the centre: 159
WEST_Q = 140
WALL_Q = 198  # from the centre: 194; from the near edge: 201

FLOAT32_TOLERANCE_DEG = 2e-4
"""What float32 over datum-relative heights can cost; the quantum is 0.353 deg."""


def build_scene(ground_m: float = GROUND_M) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Flat ground at ``ground_m`` with the four obstacles standing on it."""
    dtm = np.full((SIZE, SIZE), ground_m, dtype=np.float64)
    dsm = dtm.copy()
    landcover = np.full((SIZE, SIZE), Landcover.GROUND, dtype=np.uint8)
    for (d_row, d_col), height, cover in (
        (SOUTH_TOWER, SOUTH_HEIGHT_M, Landcover.BUILDING),
        (EAST_TOWER, EAST_HEIGHT_M, Landcover.BUILDING),
        (THIN_WALL, WALL_HEIGHT_M, Landcover.BUILDING),
        (WEST_TREE, WEST_HEIGHT_M, Landcover.VEGETATION),
    ):
        dsm[CENTRE + d_row, CENTRE + d_col] = ground_m + height
        landcover[CENTRE + d_row, CENTRE + d_col] = cover
    return dsm, dtm, landcover


PARAMS = HorizonParams(sectors=64, max_distance_m=MAX_DISTANCE_M, observer_height_m=EYE_M)


@pytest.fixture
def golden_angles() -> np.ndarray:
    """Unquantized horizon of the single observer pixel, one value per sector."""
    dsm, dtm, landcover = build_scene()
    angles, _, _ = compute_horizon_block(
        dsm, dtm, landcover, RESOLUTION_M, PARAMS, INNER, height_datum(dtm)
    )
    return angles[:, 0, 0]


@pytest.fixture
def golden_cubes() -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    dsm, dtm, landcover = build_scene()
    angles_q, blocker, noveg_q = quantized_horizon_block(
        dsm, dtm, landcover, RESOLUTION_M, PARAMS, INNER, height_datum(dtm)
    )
    return angles_q[:, 0, 0], blocker[:, 0, 0], noveg_q[:, 0, 0]


def test_the_hand_arithmetic_matches_its_own_literals() -> None:
    """The literals above are atan(dz / d), and nothing here comes from the engine."""
    for height, entry, expected in (
        (SOUTH_HEIGHT_M, SOUTH_ENTRY_M, SOUTH_ANGLE_DEG),
        (EAST_HEIGHT_M, EAST_ENTRY_M, EAST_ANGLE_DEG),
        (WEST_HEIGHT_M, WEST_ENTRY_M, WEST_ANGLE_DEG),
        (WALL_HEIGHT_M, WALL_ENTRY_M, WALL_ANGLE_DEG),
    ):
        top = GROUND_M + height
        assert math.degrees(math.atan((top - OBSERVER_Z) / entry)) == pytest.approx(
            expected, abs=1e-6
        )


def test_south_tower_blocks_from_its_near_face(golden_angles: np.ndarray) -> None:
    """A 20 m tower ten cells south subtends 62.692 deg, measured from 9.5 m."""
    assert golden_angles[SOUTH_SECTOR] == pytest.approx(SOUTH_ANGLE_DEG, abs=FLOAT32_TOLERANCE_DEG)
    # And not from the cell centre, which is what the engine used to do: the two
    # are 1.2 degrees apart, three and a half quanta.
    assert abs(golden_angles[SOUTH_SECTOR] - 61.476881) > 1.0


def test_east_tower_at_an_odd_distance(golden_angles: np.ndarray) -> None:
    """Odd cells are where the old rounding fell half a pixel long."""
    assert golden_angles[EAST_SECTOR] == pytest.approx(EAST_ANGLE_DEG, abs=FLOAT32_TOLERANCE_DEG)
    assert abs(golden_angles[EAST_SECTOR] - 56.056413) > 1.0


def test_the_thin_wall_the_old_sampling_skipped(golden_angles: np.ndarray) -> None:
    """(-4, +1) is crossed by sector 4 and was never sampled before ADR-027.

    If someone goes back to sampling a rounded schedule, this sector loses its
    only obstacle and the angle collapses to open sky.
    """
    assert golden_angles[WALL_SECTOR] == pytest.approx(WALL_ANGLE_DEG, abs=FLOAT32_TOLERANCE_DEG)
    assert golden_angles[WALL_SECTOR] > 60.0, "sector 4 must see the wall at all"
    # The three conventions are far enough apart to tell without a microscope.
    assert abs(golden_angles[WALL_SECTOR] - WALL_ANGLE_FROM_CENTRE_DEG) > 1.5
    assert abs(golden_angles[WALL_SECTOR] - WALL_ANGLE_FROM_NEAR_EDGE_DEG) > 0.7


def test_sectors_with_nothing_in_them_are_open_sky(golden_angles: np.ndarray) -> None:
    """Flat ground below the eye is a negative angle, which clamps to open sky."""
    busy = {SOUTH_SECTOR, EAST_SECTOR, WEST_SECTOR}
    # The wall at (-4, +1) is crossed by sectors 2, 3 and 4 alike.
    busy |= {2, 3, 4}
    for sector in range(PARAMS.sectors):
        if sector not in busy:
            assert golden_angles[sector] <= 0.0, f"sector {sector} sees something it should not"


def test_quantized_values_are_pinned(golden_cubes: tuple[np.ndarray, ...]) -> None:
    """The integers that actually ship, as integers."""
    angles_q, _, _ = golden_cubes
    assert int(angles_q[SOUTH_SECTOR]) == SOUTH_Q
    assert int(angles_q[EAST_SECTOR]) == EAST_Q
    assert int(angles_q[WEST_SECTOR]) == WEST_Q
    assert int(angles_q[WALL_SECTOR]) == WALL_Q


def test_blocker_classes_are_pinned(golden_cubes: tuple[np.ndarray, ...]) -> None:
    _, blocker, _ = golden_cubes
    assert int(blocker[SOUTH_SECTOR]) == Landcover.BUILDING
    assert int(blocker[EAST_SECTOR]) == Landcover.BUILDING
    assert int(blocker[WALL_SECTOR]) == Landcover.BUILDING
    assert int(blocker[WEST_SECTOR]) == Landcover.VEGETATION
    assert int(blocker[0]) == NO_BLOCKER


def test_felling_the_tree_opens_its_sector_and_nothing_else(
    golden_cubes: tuple[np.ndarray, ...],
) -> None:
    """The no-vegetation cube keeps the buildings and loses the crown."""
    _, _, noveg_q = golden_cubes
    assert int(noveg_q[WEST_SECTOR]) == 0
    assert int(noveg_q[SOUTH_SECTOR]) == SOUTH_Q
    assert int(noveg_q[EAST_SECTOR]) == EAST_Q
    assert int(noveg_q[WALL_SECTOR]) == WALL_Q


def test_the_same_scene_at_sea_level_gives_the_same_integers() -> None:
    """Raising the whole city cannot change its skyline -- and the datum is why.

    At 367 m the float32 ulp is 3.05e-05 m, so 1.6 m of eye height lands on a
    different fraction than it does at 0 m. Working relative to the datum is
    what keeps these integers equal.
    """
    at_sea_level, _, _ = quantized_horizon_block(
        *build_scene(ground_m=0.0), RESOLUTION_M, PARAMS, INNER, 0.0
    )
    dsm, dtm, landcover = build_scene()
    high, _, _ = quantized_horizon_block(
        dsm, dtm, landcover, RESOLUTION_M, PARAMS, INNER, height_datum(dtm)
    )
    np.testing.assert_array_equal(at_sea_level[:, 0, 0], high[:, 0, 0])
    assert int(high[SOUTH_SECTOR, 0, 0]) == SOUTH_Q


def test_the_quantum_is_what_the_docstrings_claim() -> None:
    """0.353 deg per step, which is the ruler every integer above is written in."""
    assert pytest.approx(0.3529, abs=1e-4) == ANGLE_MAX_DEG / 255.0
    assert round(SOUTH_ANGLE_DEG * 255.0 / ANGLE_MAX_DEG) == SOUTH_Q
    assert round(EAST_ANGLE_DEG * 255.0 / ANGLE_MAX_DEG) == EAST_Q
    assert round(WEST_ANGLE_DEG * 255.0 / ANGLE_MAX_DEG) == WEST_Q
    assert round(WALL_ANGLE_DEG * 255.0 / ANGLE_MAX_DEG) == WALL_Q
