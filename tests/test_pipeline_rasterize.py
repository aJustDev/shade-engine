"""Binning and gap-filling of LiDAR points into the base rasters."""

from pathlib import Path

import numpy as np
import pytest
from numpy.testing import assert_allclose, assert_array_equal

import laz_fixture
import synthetic
from shade_core.shade import Landcover
from shade_pipeline.rasterize import RasterStack, fill_dtm_gaps, rasterize_lidar

BBOX = (0.0, 0.0, 4.0, 4.0)  # 4x4 cells at 1 m; cell (0, 0) covers x in [0,1), y in [3,4)


def _rasterize(
    tmp_path: Path,
    x: list[float],
    y: list[float],
    z: list[float],
    classification: list[int],
    return_number: list[int] | None = None,
) -> RasterStack:
    path = tmp_path / "points.laz"
    laz_fixture.write_laz(
        path,
        np.array(x, dtype=np.float64),
        np.array(y, dtype=np.float64),
        np.array(z, dtype=np.float64),
        np.array(classification, dtype=np.uint8),
        None if return_number is None else np.array(return_number, dtype=np.uint8),
    )
    return rasterize_lidar([path], BBOX, 1.0)


def test_dsm_keeps_max_first_return(tmp_path: Path) -> None:
    stack = _rasterize(tmp_path, [0.5, 0.5], [3.5, 3.5], [1.0, 5.0], [2, 2])
    assert stack.dsm[0, 0] == 5.0


def test_later_returns_do_not_feed_dsm(tmp_path: Path) -> None:
    # A high first return in another cell plus a later return in cell (0, 0):
    # the later return must not set the DSM there.
    stack = _rasterize(tmp_path, [0.5, 2.5], [3.5, 3.5], [10.0, 0.0], [5, 2], return_number=[2, 1])
    assert stack.dsm[0, 0] == 0.0  # filled from the only ground point, not 10.0


def test_dtm_averages_ground_points(tmp_path: Path) -> None:
    stack = _rasterize(tmp_path, [0.5, 0.9], [3.5, 3.5], [1.0, 2.0], [2, 2])
    assert stack.dtm[0, 0] == pytest.approx(1.5)


def test_ground_later_returns_do_feed_dtm(tmp_path: Path) -> None:
    # Under canopy the ground echo is a later return; class 2 counts anyway.
    stack = _rasterize(tmp_path, [0.5, 0.5], [3.5, 3.5], [8.0, 1.0], [4, 2], return_number=[1, 2])
    assert stack.dtm[0, 0] == pytest.approx(1.0)
    assert stack.dsm[0, 0] == 8.0


def test_landcover_is_class_of_dsm_point(tmp_path: Path) -> None:
    stack = _rasterize(tmp_path, [0.5, 0.5, 2.5], [3.5, 3.5, 1.5], [5.0, 3.0, 0.0], [4, 6, 2])
    assert stack.landcover[0, 0] == Landcover.VEGETATION  # tree top above the roof point


def test_landcover_tie_prefers_building(tmp_path: Path) -> None:
    stack = _rasterize(tmp_path, [0.5, 0.5, 2.5], [3.5, 3.5, 1.5], [5.0, 5.0, 0.0], [4, 6, 2])
    assert stack.landcover[0, 0] == Landcover.BUILDING


def test_points_outside_bbox_are_dropped(tmp_path: Path) -> None:
    stack = _rasterize(tmp_path, [0.5, 10.5], [3.5, 3.5], [0.0, 99.0], [2, 6])
    assert float(stack.dsm.max()) == 0.0


def test_dsm_hole_takes_filled_dtm(tmp_path: Path) -> None:
    # Only a later-return ground point: no first return anywhere, DSM = DTM.
    stack = _rasterize(tmp_path, [0.5], [3.5], [3.0], [2], return_number=[2])
    assert stack.dsm[0, 0] == 3.0
    assert stack.landcover[0, 0] == Landcover.GROUND


def test_fill_dtm_gaps_interior_hole() -> None:
    dtm = np.zeros((5, 5), dtype=np.float32)
    dtm[2, 2] = np.nan
    filled = fill_dtm_gaps(dtm)
    assert filled[2, 2] == pytest.approx(0.0)
    assert not np.isnan(filled).any()


def test_fill_dtm_gaps_raises_when_unfillable() -> None:
    with pytest.raises(ValueError, match="no ground point"):
        fill_dtm_gaps(np.full((5, 5), np.nan, dtype=np.float32))


def test_cube_scene_roundtrip(tmp_path: Path) -> None:
    """The golden fixture as a LAZ reproduces the synthetic arrays exactly."""
    path = tmp_path / "cube.laz"
    count = laz_fixture.write_cube_laz(path)
    stack = rasterize_lidar([path], (0.0, 0.0, 120.0, 120.0), synthetic.RESOLUTION_M)

    dsm, dtm = synthetic.cube_scene()
    assert stack.point_counts == {"cube.laz": count}
    assert_array_equal(stack.dsm, dsm.astype(np.float32))
    # The 20x20 DTM hole under the cube fills to 0 from the flat surroundings.
    assert_allclose(stack.dtm, dtm.astype(np.float32), atol=1e-6)
    assert_array_equal(stack.landcover, synthetic.cube_landcover())
    assert stack.transform.c == 0.0
    assert stack.transform.f == 120.0
