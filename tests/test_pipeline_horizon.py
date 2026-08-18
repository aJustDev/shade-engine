"""The production horizon sweep against the brute-force oracle from core."""

import math
import os
from pathlib import Path

import numpy as np
import pytest
from numpy.testing import assert_allclose, assert_array_equal

import synthetic
from shade_core.horizon import HorizonGrid
from shade_core.shade import Landcover, ShadeScene
from shade_pipeline import horizon as horizon_module
from shade_pipeline.budget import MemoryBudgetError
from shade_pipeline.grid import buffer_pixels
from shade_pipeline.horizon import (
    NO_BLOCKER,
    HorizonParams,
    compute_horizon_block,
    compute_horizon_tiled,
    quantize_angles,
    quantized_horizon_block,
    sector_offsets,
    tile_jobs,
)

CUBE_PARAMS = HorizonParams(max_distance_m=80.0)


def _full_window(dsm: np.ndarray) -> tuple[int, int, int, int]:
    rows, cols = dsm.shape
    return (0, rows, 0, cols)


def test_exact_block_matches_reference_cube(cube_grid: HorizonGrid) -> None:
    dsm, dtm = synthetic.cube_scene()
    angles, _, _ = compute_horizon_block(
        dsm, dtm, synthetic.cube_landcover(), 1.0, CUBE_PARAMS, _full_window(dsm)
    )
    assert_allclose(angles, cube_grid.angles_deg, atol=1e-4)


def test_exact_block_matches_reference_tree(tree_shade_scene: ShadeScene) -> None:
    dsm, dtm, landcover, _ = synthetic.tree_scene()
    params = HorizonParams(max_distance_m=40.0)
    angles, _, _ = compute_horizon_block(dsm, dtm, landcover, 1.0, params, _full_window(dsm))
    assert_allclose(angles, tree_shade_scene.horizon.angles_deg, atol=1e-4)


def _wall_behind_tree() -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Flat 40x40 with a 10 m wall and, 6 m in front of it, a 12 m crown.

    Looking north from row 19 the crown wins the sector by ~29 degrees, so the
    blocker class reads VEGETATION -- and felling it still leaves the wall.
    That gap between "who won" and "what would remain" is the whole reason the
    second cube exists.
    """
    size = 40
    dsm = np.zeros((size, size))
    dtm = np.zeros((size, size))
    landcover = np.zeros((size, size), dtype=np.uint8)

    def fill(y0: int, y1: int, target: np.ndarray, value: float) -> None:
        target[size - y1 : size - y0, :] = value

    fill(30, 32, dsm, 10.0)
    fill(30, 32, landcover, Landcover.BUILDING)
    fill(24, 26, dsm, 12.0)
    fill(24, 26, landcover, Landcover.VEGETATION)
    return dsm, dtm, landcover


def test_noveg_horizon_keeps_the_wall_behind_the_tree() -> None:
    dsm, dtm, landcover = _wall_behind_tree()
    params = HorizonParams(max_distance_m=20.0)
    angles, blocker, noveg = compute_horizon_block(
        dsm, dtm, landcover, 1.0, params, _full_window(dsm)
    )
    north, row, col = 0, 19, 20
    assert blocker[north, row, col] == Landcover.VEGETATION
    # Distances are the half-pixel schedule deduped to the *smallest* distance
    # that lands on each cell, so the cell 4 rows north is sampled at 3.5 m and
    # the one 10 rows north at 9.5. Eye at 1.6 m: crown 12, wall 10.
    assert angles[north, row, col] == pytest.approx(np.degrees(np.arctan2(10.4, 3.5)), abs=1e-3)
    assert noveg[north, row, col] == pytest.approx(np.degrees(np.arctan2(8.4, 9.5)), abs=1e-3)


def test_noveg_horizon_never_rises_above_the_full_one() -> None:
    """Felling trees can only open the sky. True pixel by pixel, sector by sector."""
    dsm, dtm, landcover = _wall_behind_tree()
    result = compute_horizon_tiled(dsm, dtm, landcover, 1.0, HorizonParams(max_distance_m=20.0))
    assert (result.angles_noveg_q <= result.angles_q).all()


def test_noveg_horizon_equals_the_full_one_without_vegetation(cube_grid: HorizonGrid) -> None:
    """No crowns, nothing to fell: the two cubes describe the same skyline.

    Within one quantum, because the two reach it by different arithmetic (one
    arctan2 per sample against one arctan of an accumulated tangent).
    """
    dsm, dtm = synthetic.cube_scene()
    result = compute_horizon_tiled(dsm, dtm, synthetic.cube_landcover(), 1.0, CUBE_PARAMS)
    difference = result.angles_q.astype(np.int16) - result.angles_noveg_q.astype(np.int16)
    assert np.abs(difference).max() <= 1


def test_lifting_the_whole_city_changes_nothing() -> None:
    """Raise every height by 400 m and the cubes come out bit for bit the same.

    The sweep only ever uses *differences* of height, so the elevation above the
    ellipsoid must not enter the result -- and in float32 it does unless the
    heights are made relative first: at 367 m the ulp is 3.05e-05 m, so a
    constant like the observer's 1.6 m rounds the same way on every pixel of a
    city and quietly shifts the whole skyline (see shade-docs:
    learning/precision-de-alturas.md). 400 m is a whole number of datum steps,
    which is what makes the equality exact rather than approximate.

    The equality is demanded on the *angles*, not on their quantization: this
    fixture's angles sit far from a quantization boundary, so the cubes survive
    an error the floats do not. Measured with ``height_datum_m=0.0`` forced,
    which is the deliberate error this pins: the angles differ by up to
    1.14e-05 deg while the quantized cubes stay identical. On real data at
    367 m that same defect moves 6,219 cells and 26 verdicts.
    """
    dsm, dtm = synthetic.cube_scene()
    landcover = synthetic.cube_landcover()
    inner = _full_window(dsm)
    ground = compute_horizon_block(
        dsm, dtm, landcover, 1.0, CUBE_PARAMS, inner, horizon_module.height_datum(dtm)
    )
    lifted = compute_horizon_block(
        dsm + 400.0,
        dtm + 400.0,
        landcover,
        1.0,
        CUBE_PARAMS,
        inner,
        horizon_module.height_datum(dtm + 400.0),
    )
    for mine, theirs in zip(lifted, ground, strict=True):
        assert_array_equal(mine, theirs)

    # And the driver derives that datum by itself, which is the only way a
    # production sweep gets one.
    result = compute_horizon_tiled(dsm + 400.0, dtm + 400.0, landcover, 1.0, CUBE_PARAMS)
    assert result.height_datum_m == 400.0
    assert_array_equal(result.angles_q, quantize_angles(lifted[0]))


def test_height_datum_is_the_median_to_the_nearest_hundred() -> None:
    """Nearest, not truncated: truncating leaves the heights in a high binade."""
    assert horizon_module.height_datum(np.full((4, 4), 361.1)) == 400.0
    assert horizon_module.height_datum(np.full((4, 4), 289.8)) == 300.0
    assert horizon_module.height_datum(np.zeros((4, 4))) == 0.0


def test_quantized_block_equals_quantizing_the_float_block() -> None:
    """The memory-lean path and the oracle's peer are the same numbers.

    ``quantized_horizon_block`` exists only to avoid holding float32 cubes;
    if it ever computed anything different the parallel sweep would be
    silently wrong, so the two are pinned together here.
    """
    dsm, dtm, landcover = _wall_behind_tree()
    params = HorizonParams(max_distance_m=20.0)
    inner = _full_window(dsm)
    angles, blocker, noveg = compute_horizon_block(dsm, dtm, landcover, 1.0, params, inner)
    angles_q, blocker_q, noveg_q = quantized_horizon_block(dsm, dtm, landcover, 1.0, params, inner)
    assert_array_equal(angles_q, quantize_angles(angles))
    assert_array_equal(blocker_q, blocker)
    assert_array_equal(noveg_q, quantize_angles(noveg))


def test_tile_jobs_partition_the_inner_window() -> None:
    """Every inner pixel is swept exactly once, ragged last row/column included."""
    inner = (10, 130, 20, 100)
    jobs = tile_jobs(inner, 48)
    covered = np.zeros((inner[1] - inner[0], inner[3] - inner[2]), dtype=np.int32)
    for t0, t1, u0, u1 in jobs:
        covered[t0 - inner[0] : t1 - inner[0], u0 - inner[2] : u1 - inner[2]] += 1
    assert (covered == 1).all()
    assert len(jobs) == 3 * 2  # 120 = 48 + 48 + 24 rows, 80 = 48 + 32 cols


@pytest.mark.parametrize("scratch", [False, True])
def test_parallel_sweep_matches_serial(scratch: bool, tmp_path: Path) -> None:
    """The exit criterion: workers change the schedule, never a single value.

    Run with a scratch dir too, because memmapped cubes are what production
    writes into and the parallel path files tiles into them out of order.
    """
    dsm, dtm = synthetic.cube_scene()
    landcover = synthetic.cube_landcover()
    params = HorizonParams(max_distance_m=20.0, tile_size=48)
    serial = compute_horizon_tiled(dsm, dtm, landcover, 1.0, params)
    parallel = compute_horizon_tiled(
        dsm,
        dtm,
        landcover,
        1.0,
        HorizonParams(max_distance_m=20.0, tile_size=48, workers=2),
        scratch_dir=tmp_path if scratch else None,
    )
    assert_array_equal(np.asarray(parallel.angles_q), serial.angles_q)
    assert_array_equal(np.asarray(parallel.blocker_class), serial.blocker_class)
    assert_array_equal(np.asarray(parallel.angles_noveg_q), serial.angles_noveg_q)


def _suicidal_block(*args: object, **kwargs: object) -> None:
    """Stand-in for the sweep that kills its own process, like the OOM killer."""
    os._exit(1)


def test_dead_worker_fails_the_sweep(monkeypatch: pytest.MonkeyPatch) -> None:
    """A worker that dies ends the build; it never degrades to serial in silence.

    Patching what the tile *calls* rather than ``_sweep_tile`` itself: that one
    is pickled by qualified name and has to stay the real function. ``fork``
    hands the patched module straight to the children.
    """
    monkeypatch.setattr(horizon_module, "quantized_horizon_block", _suicidal_block)
    dsm, dtm = synthetic.cube_scene()
    params = HorizonParams(max_distance_m=20.0, tile_size=48, workers=2)
    with pytest.raises(RuntimeError, match="worker died"):
        compute_horizon_tiled(dsm, dtm, synthetic.cube_landcover(), 1.0, params)


def test_sweep_refuses_workers_that_do_not_fit(monkeypatch: pytest.MonkeyPatch) -> None:
    """The guardrail fires before the pool exists, naming what would fit."""
    monkeypatch.setattr(horizon_module, "check_worker_budget", _raise_budget)
    dsm, dtm = synthetic.cube_scene()
    params = HorizonParams(max_distance_m=20.0, tile_size=48, workers=4)
    with pytest.raises(MemoryBudgetError, match="--workers 1"):
        compute_horizon_tiled(dsm, dtm, synthetic.cube_landcover(), 1.0, params)


def _raise_budget(*args: object, **kwargs: object) -> None:
    raise MemoryBudgetError("needs more than there is; use --workers 1 or fewer")


def test_tiled_quantized_equals_quantized_reference(cube_grid: HorizonGrid) -> None:
    """Ragged tiles (120 = 48 + 48 + 24) must not change a single value."""
    dsm, dtm = synthetic.cube_scene()
    params = HorizonParams(max_distance_m=80.0, tile_size=48)
    result = compute_horizon_tiled(dsm, dtm, synthetic.cube_landcover(), 1.0, params)
    assert_array_equal(result.angles_q, quantize_angles(cube_grid.angles_deg))


def _western_half(shape: tuple[int, ...]) -> np.ndarray:
    """A computation area covering the western half of the scene."""
    rows, cols = shape
    mask = np.zeros((rows, cols), dtype=bool)
    mask[:, : cols // 2] = True
    return mask


def test_masked_sweep_does_not_depend_on_the_tile_size() -> None:
    """The invariant the area mask could have broken, and the reason it masks late.

    A tile the area only grazes is swept whole and masked afterwards, so the
    tile size decides how much work is skipped and never what is written. The
    three sizes here put the area's edge in a different place each time: 24
    splits it on a tile boundary and leaves whole tiles outside, 120 makes the
    entire scene one partially covered tile.
    """
    dsm, dtm = synthetic.cube_scene()
    landcover = synthetic.cube_landcover()
    coverage = _western_half(dsm.shape)
    results = [
        compute_horizon_tiled(
            dsm,
            dtm,
            landcover,
            1.0,
            HorizonParams(max_distance_m=20.0, tile_size=size),
            coverage=coverage,
        )
        for size in (24, 48, 120)
    ]
    for other in results[1:]:
        assert_array_equal(other.angles_q, results[0].angles_q)
        assert_array_equal(other.blocker_class, results[0].blocker_class)
        assert_array_equal(other.angles_noveg_q, results[0].angles_noveg_q)


def test_uncovered_pixels_are_open_sky_with_no_blocker() -> None:
    """Outside the area: angle 0 with NO_BLOCKER, in both cubes.

    Not a claim that the sky is open -- ``coverage.tif`` is what says there is
    no data -- but the only pair that satisfies the artifact invariants:
    ``verify`` treats an angle above 0 with no blocker as a hard failure, and
    a zero angle with a real class as evidence of a cube that lost data.
    """
    dsm, dtm = synthetic.cube_scene()
    coverage = _western_half(dsm.shape)
    result = compute_horizon_tiled(
        dsm,
        dtm,
        synthetic.cube_landcover(),
        1.0,
        HorizonParams(max_distance_m=20.0, tile_size=48),
        coverage=coverage,
    )
    outside = (slice(None), slice(None), slice(dsm.shape[1] // 2, None))
    assert_array_equal(result.angles_q[outside], 0)
    assert_array_equal(result.angles_noveg_q[outside], 0)
    assert_array_equal(result.blocker_class[outside], NO_BLOCKER)
    # ...and the covered half is bit for bit what an unmasked sweep produced.
    full = compute_horizon_tiled(
        dsm,
        dtm,
        synthetic.cube_landcover(),
        1.0,
        HorizonParams(max_distance_m=20.0, tile_size=48),
    )
    inside = (slice(None), slice(None), slice(None, dsm.shape[1] // 2))
    assert_array_equal(result.angles_q[inside], full.angles_q[inside])
    assert_array_equal(result.blocker_class[inside], full.blocker_class[inside])
    assert_array_equal(result.angles_noveg_q[inside], full.angles_noveg_q[inside])


def test_masked_sweep_survives_the_workers(tmp_path: Path) -> None:
    """Skipping tiles changes what is submitted; it must not change a value."""
    dsm, dtm = synthetic.cube_scene()
    landcover = synthetic.cube_landcover()
    coverage = _western_half(dsm.shape)
    serial = compute_horizon_tiled(
        dsm,
        dtm,
        landcover,
        1.0,
        HorizonParams(max_distance_m=20.0, tile_size=24),
        coverage=coverage,
    )
    parallel = compute_horizon_tiled(
        dsm,
        dtm,
        landcover,
        1.0,
        HorizonParams(max_distance_m=20.0, tile_size=24, workers=2),
        coverage=coverage,
        scratch_dir=tmp_path,
    )
    assert_array_equal(np.asarray(parallel.angles_q), serial.angles_q)
    assert_array_equal(np.asarray(parallel.blocker_class), serial.blocker_class)
    assert_array_equal(np.asarray(parallel.angles_noveg_q), serial.angles_noveg_q)


def test_the_sweep_reports_the_tiles_it_skips() -> None:
    """The saving has to be visible: it is the only reason the area exists."""
    dsm, dtm = synthetic.cube_scene()
    lines: list[str] = []
    compute_horizon_tiled(
        dsm,
        dtm,
        synthetic.cube_landcover(),
        1.0,
        HorizonParams(max_distance_m=20.0, tile_size=24),
        coverage=_western_half(dsm.shape),
        progress=lines.append,
    )
    assert "sweeping 15 tiles serially (10 outside the area)" in lines[0]


def test_an_area_that_misses_the_bbox_is_refused() -> None:
    dsm, dtm = synthetic.cube_scene()
    with pytest.raises(ValueError, match="covers no pixel"):
        compute_horizon_tiled(
            dsm,
            dtm,
            synthetic.cube_landcover(),
            1.0,
            HorizonParams(max_distance_m=20.0, tile_size=48),
            coverage=np.zeros(dsm.shape, dtype=bool),
        )


def test_a_mask_of_the_wrong_shape_is_refused() -> None:
    """The mask covers the inner window, not the padded raster; mixing them is a bug."""
    dsm, dtm = synthetic.cube_scene()
    with pytest.raises(ValueError, match="coverage mask is"):
        compute_horizon_tiled(
            dsm,
            dtm,
            synthetic.cube_landcover(),
            1.0,
            HorizonParams(max_distance_m=20.0, tile_size=48),
            (20, 100, 20, 100),
            coverage=np.ones(dsm.shape, dtype=bool),
        )


def test_inner_window_equals_reference_crop(cube_grid: HorizonGrid) -> None:
    """Sweeping only an inner window reproduces the reference crop (padding path)."""
    dsm, dtm = synthetic.cube_scene()
    params = HorizonParams(max_distance_m=20.0, tile_size=48)
    inner = (20, 100, 20, 100)
    result = compute_horizon_tiled(dsm, dtm, synthetic.cube_landcover(), 1.0, params, inner)
    from shade_core.horizon import compute_horizon_reference

    reference = compute_horizon_reference(dsm, dtm, 1.0, max_distance_m=20.0)
    assert_array_equal(result.angles_q, quantize_angles(reference.angles_deg[:, 20:100, 20:100]))


def test_tiled_scratch_dir_matches_in_memory(tmp_path: Path) -> None:
    """Memmapped output cubes are a storage detail: values stay bit-identical."""
    dsm, dtm = synthetic.cube_scene()
    in_memory = compute_horizon_tiled(dsm, dtm, synthetic.cube_landcover(), 1.0, CUBE_PARAMS)
    mapped = compute_horizon_tiled(
        dsm, dtm, synthetic.cube_landcover(), 1.0, CUBE_PARAMS, scratch_dir=tmp_path
    )
    assert_array_equal(np.asarray(mapped.angles_q), in_memory.angles_q)
    assert_array_equal(np.asarray(mapped.blocker_class), in_memory.blocker_class)


def test_blocker_class_cube() -> None:
    dsm, dtm = synthetic.cube_scene()
    result = compute_horizon_tiled(dsm, dtm, synthetic.cube_landcover(), 1.0, CUBE_PARAMS)
    row, col = 60, int(synthetic.QUERY_X)  # 10 m north of the cube wall
    south = CUBE_PARAMS.sectors // 2
    assert result.blocker_class[south, row, col] == Landcover.BUILDING
    assert result.blocker_class[0, row, col] == NO_BLOCKER


def test_flat_terrain_is_all_sky() -> None:
    dsm, dtm = synthetic.flat_terrain(20)
    landcover = np.zeros((20, 20), dtype=np.uint8)
    params = HorizonParams(max_distance_m=10.0)
    result = compute_horizon_tiled(dsm, dtm, landcover, 1.0, params)
    assert not result.angles_q.any()
    assert (result.blocker_class == NO_BLOCKER).all()


def test_sector_offsets_unique_bounded_ascending() -> None:
    for sector in (0, 7, 16, 33, 63):
        offsets = sector_offsets(sector, CUBE_PARAMS, 1.0)
        cells = [(dr, dc) for dr, dc, _ in offsets]
        distances = [d for _, _, d in offsets]
        bound = buffer_pixels(CUBE_PARAMS.max_distance_m, 1.0)
        assert len(set(cells)) == len(cells)
        assert (0, 0) not in cells
        assert all(abs(dr) <= bound and abs(dc) <= bound for dr, dc in cells)
        assert distances == sorted(distances)


def test_quantization_roundtrip() -> None:
    # The 1e-4 slack absorbs float32 rounding (~1 ulp of 90) on top of the
    # theoretical half-step bound; it is negligible vs the 0.353 deg step.
    angles = np.linspace(0.0, 90.0, 1001, dtype=np.float32).reshape(1, 7, 143)
    dequantized = quantize_angles(angles).astype(np.float32) * (90.0 / 255.0)
    assert np.abs(dequantized - angles).max() <= 90.0 / 255.0 / 2.0 + 1e-4


def test_a_shorter_radius_cannot_cost_more_than_the_geometry_allows() -> None:
    """The bound behind [[ADR-028]], made executable.

    An obstacle of height h left outside radius R can raise the horizon by at
    most ``atan(h / R)``, because that is the steepest line from an eye to
    anything at least R away. So shortening the radius has an error ceiling that
    can be written down before running anything, and it can only bite with the
    sun below that angle.

    This is what makes the radius the safe speed lever and thinning the step an
    unsafe one: a schedule that skips cells has no such ceiling, its error being
    set by wherever the tall thin things happen to fall relative to it. Measured
    on montilla-test, 500 -> 250 m: worst drop 6.353 deg against a bound of
    20.139.
    """
    dsm, dtm = synthetic.cube_scene()
    landcover = synthetic.cube_landcover()
    window = _full_window(dsm)
    short_radius = 30.0

    far, _, _ = compute_horizon_block(
        dsm, dtm, landcover, 1.0, HorizonParams(max_distance_m=80.0), window
    )
    near, _, _ = compute_horizon_block(
        dsm, dtm, landcover, 1.0, HorizonParams(max_distance_m=short_radius), window
    )

    # The tallest thing any observer here can see above its own eye.
    tallest = float(dsm.max() - (dtm.min() + 1.6))
    bound_deg = math.degrees(math.atan(tallest / short_radius))
    lost = far - near

    assert lost.min() >= -1e-4, "a shorter radius can only lower the horizon"
    assert lost.max() <= bound_deg, f"lost {lost.max():.3f} deg, bound is {bound_deg:.3f}"
    assert lost.max() > 0.0, "this fixture must lose something, or the test proves nothing"
