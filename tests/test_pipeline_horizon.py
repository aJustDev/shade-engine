"""The production horizon sweep against the brute-force oracle from core."""

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


def test_geometric_mode_close_to_reference(cube_grid: HorizonGrid) -> None:
    """Sanity only: the fast far-field schedule stays near the oracle here.

    Quantile, not max: geometric distances round to cell offsets the exact
    schedule never visits, so a ray grazing a cube corner can legitimately
    hit a cell the oracle skipped (tens of degrees on isolated pixels). Same
    discretization family as the phase-1 corner traps; the bulk must agree.
    """
    dsm, dtm = synthetic.cube_scene()
    params = HorizonParams(max_distance_m=80.0, step_mode="geometric")
    angles, _, _ = compute_horizon_block(
        dsm, dtm, synthetic.cube_landcover(), 1.0, params, _full_window(dsm)
    )
    difference = np.abs(angles - cube_grid.angles_deg)
    assert np.quantile(difference, 0.999) <= 0.5
