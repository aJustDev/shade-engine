"""The production horizon sweep against the brute-force oracle from core."""

import numpy as np
from numpy.testing import assert_allclose, assert_array_equal

import synthetic
from shade_core.horizon import HorizonGrid
from shade_core.shade import Landcover, ShadeScene
from shade_pipeline.grid import buffer_pixels
from shade_pipeline.horizon import (
    NO_BLOCKER,
    HorizonParams,
    compute_horizon_block,
    compute_horizon_tiled,
    quantize_angles,
    sector_offsets,
)

CUBE_PARAMS = HorizonParams(max_distance_m=80.0)


def _full_window(dsm: np.ndarray) -> tuple[int, int, int, int]:
    rows, cols = dsm.shape
    return (0, rows, 0, cols)


def test_exact_block_matches_reference_cube(cube_grid: HorizonGrid) -> None:
    dsm, dtm = synthetic.cube_scene()
    angles, _ = compute_horizon_block(
        dsm, dtm, synthetic.cube_landcover(), 1.0, CUBE_PARAMS, _full_window(dsm)
    )
    assert_allclose(angles, cube_grid.angles_deg, atol=1e-4)


def test_exact_block_matches_reference_tree(tree_shade_scene: ShadeScene) -> None:
    dsm, dtm, landcover, _ = synthetic.tree_scene()
    params = HorizonParams(max_distance_m=40.0)
    angles, _ = compute_horizon_block(dsm, dtm, landcover, 1.0, params, _full_window(dsm))
    assert_allclose(angles, tree_shade_scene.horizon.angles_deg, atol=1e-4)


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
    angles, _ = compute_horizon_block(
        dsm, dtm, synthetic.cube_landcover(), 1.0, params, _full_window(dsm)
    )
    difference = np.abs(angles - cube_grid.angles_deg)
    assert np.quantile(difference, 0.999) <= 0.5
