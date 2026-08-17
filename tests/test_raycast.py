"""Grid traversal: the properties the sweep and the arbiter both lean on."""

import itertools

import pytest

from shade_core.raycast import ray_cells


@pytest.mark.parametrize("azimuth", [0.0, 45.0, 90.0, 123.75, 180.0, 270.0, 337.5])
def test_traversal_is_a_contiguous_ordered_walk(azimuth: float) -> None:
    """Every step lands on a 4-neighbour, distances never go back, no repeats."""
    cells = ray_cells(azimuth, 60.0, 1.0)
    assert cells, "a ray of 60 m must cross something"
    seen = {(0, 0)}
    previous_row, previous_col, previous_entry = 0, 0, 0.0
    for row, col, entry, exit_m in cells:
        assert abs(row - previous_row) + abs(col - previous_col) == 1, "jumped a cell"
        assert entry >= previous_entry, "distances must ascend"
        assert exit_m >= entry
        assert (row, col) not in seen, "a cell is entered once"
        seen.add((row, col))
        previous_row, previous_col, previous_entry = row, col, entry


def test_first_cell_is_the_neighbour_half_a_pixel_away() -> None:
    """Due north the ray leaves its own cell at half a pixel, not at one."""
    first = ray_cells(0.0, 10.0, 1.0)[0]
    assert first[:2] == (-1, 0)
    assert first[2] == pytest.approx(0.5)
    assert first[3] == pytest.approx(1.5)


def test_axis_entries_are_half_integers() -> None:
    """Along an axis, cell n is entered at n - 0.5. The nominal schedule was
    only right on the even ones (round() is half-to-even), which is the
    inconsistency ADR-027 removes."""
    for row, col, entry, _ in ray_cells(0.0, 12.0, 1.0):
        assert col == 0
        assert entry == pytest.approx(-row - 0.5)


def test_exit_of_one_cell_is_the_entry_of_the_next() -> None:
    cells = ray_cells(30.0, 25.0, 1.0)
    for (_, _, _, exit_m), (_, _, entry, _) in itertools.pairwise(cells):
        assert exit_m == pytest.approx(entry)


def test_traversal_stays_within_the_padding_the_sweep_reserves() -> None:
    """No offset may exceed ceil(max_distance / resolution).

    That bound is what makes a padded tile enough; if the traversal could
    exceed it, a sample would read outside the window and the result would stop
    being independent of tile_size.
    """
    from shade_pipeline.grid import buffer_pixels

    for sectors in (64, 128):
        for k in range(sectors):
            azimuth = k * 360.0 / sectors
            bound = buffer_pixels(100.0, 1.0)
            for row, col, _, _ in ray_cells(azimuth, 100.0, 1.0):
                assert abs(row) <= bound and abs(col) <= bound


def test_distances_are_horizontal_and_scale_with_resolution() -> None:
    """At 2 m/px the same cell offsets sit twice as far away."""
    fine = ray_cells(20.0, 40.0, 1.0)
    coarse = ray_cells(20.0, 80.0, 2.0)
    assert [(r, c) for r, c, _, _ in fine] == [(r, c) for r, c, _, _ in coarse]
    for (_, _, entry, _), (_, _, coarse_entry, _) in zip(fine, coarse, strict=True):
        assert coarse_entry == pytest.approx(2.0 * entry)


def test_diagonal_keeps_the_corner_cells() -> None:
    """Exactly 45 degrees the ray runs through corners; the grazed cell stays.

    Four cells meet at the corner and the walk emits two: the diagonal, plus one
    orthogonal neighbour with zero thickness. Keeping that one is measured to
    beat emitting both (0.722%) or neither (0.739%) against the arbiter -- 0.714%
    of wrong verdicts, open sky, on montilla-test.
    """
    cells = ray_cells(45.0, 10.0, 1.0)
    zero_thickness = [c for c in cells if c[3] == pytest.approx(c[2])]
    assert zero_thickness, "a 45 degree ray must clip corners"
    assert all(abs(row) + abs(col) <= 2 * 10 for row, col, _, _ in cells)


DIAGONAL_SECTORS = {8: (-1, 0), 24: (0, 1), 40: (1, 0), 56: (0, -1)}
"""Sector -> the orthogonal cell its ray grazes first, on this platform."""


@pytest.mark.parametrize(("sector", "grazed"), DIAGONAL_SECTORS.items())
def test_which_corner_cell_a_diagonal_grazes_is_pinned(
    sector: int, grazed: tuple[int, int]
) -> None:
    """The ulp of sin against cos picks this, so pin it and hear about a change.

    There is no rule behind these four values and no tolerance in ``ray_cells``:
    at 45 degrees sin and cos differ by 1 to 3 ulp and whichever is smaller wins
    the step. A libm that rounded the other way would graze the mirror cell,
    which is legitimate -- measured, it moves 3,675,077 cube cells (4.5%) and the
    verdict by 0.009 points -- but it must arrive as a red test and not as a city
    that came out different when someone rebuilt it.
    """
    cells = ray_cells(sector * 360.0 / 64, 10.0, 1.0)
    assert cells[0][:2] == grazed
    assert cells[0][3] == pytest.approx(cells[0][2]), "the grazed cell has zero thickness"
    # Exactly one orthogonal per corner: the pair is (orthogonal, diagonal).
    assert abs(cells[1][0]) == 1 and abs(cells[1][1]) == 1


def test_only_the_diagonals_cross_corners_and_they_do_it_every_step() -> None:
    """354 of 354 steps in four sectors; zero in the other sixty.

    This is what makes the corner case worth a decision instead of a shrug: it
    is not a rare coincidence, it is the whole geometry of NE, SE, SW and NW.
    """
    ties = {}
    for sector in range(64):
        cells = ray_cells(sector * 360.0 / 64, 500.0, 1.0)
        ties[sector] = sum(
            1 for a, b in itertools.pairwise(cells) if b[2] == pytest.approx(a[2], rel=1e-9)
        )
    assert {s for s, n in ties.items() if n} == set(DIAGONAL_SECTORS)
    assert all(ties[s] == 354 for s in DIAGONAL_SECTORS)


def test_a_ray_shorter_than_half_a_cell_crosses_nothing() -> None:
    assert ray_cells(0.0, 0.4, 1.0) == []


def test_every_quadrant_moves_the_right_way() -> None:
    """Azimuth 0 = North, clockwise: the sign convention, pinned."""
    quadrants = {0.0: (-1, 0), 90.0: (0, 1), 180.0: (1, 0), 270.0: (0, -1)}
    for azimuth, (expected_row, expected_col) in quadrants.items():
        row, col, _, _ = ray_cells(azimuth, 10.0, 1.0)[0]
        assert (row, col) == (expected_row, expected_col)
    # And a bearing between two axes moves along both.
    rows = {row for row, _, _, _ in ray_cells(135.0, 10.0, 1.0)}
    cols = {col for _, col, _, _ in ray_cells(135.0, 10.0, 1.0)}
    assert max(rows) > 0 and max(cols) > 0  # south-east
