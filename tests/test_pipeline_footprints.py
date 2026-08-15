"""The OSM footprint corrective: roofs recovered, patio trees left alone."""

import numpy as np
from shapely.geometry import box

from shade_core.shade import Landcover
from shade_pipeline.footprints import apply_footprint_override, footprint_ids
from shade_pipeline.grid import transform_from_bbox

BBOX = (0.0, 0.0, 20.0, 20.0)  # 20x20 cells at 1 m
TRANSFORM = transform_from_bbox(BBOX, 1.0)
SHAPE = (20, 20)


def _block() -> tuple[np.ndarray, np.ndarray]:
    """A 12 m building filling x,y in [4, 16), with a 4x4 courtyard in the middle.

    Returns (landcover, chm). Everything starts correctly labelled; each test
    then plants the misclassification it cares about.
    """
    landcover = np.zeros(SHAPE, dtype=np.uint8)
    chm = np.zeros(SHAPE, dtype=np.float32)
    landcover[4:16, 4:16] = Landcover.BUILDING
    chm[4:16, 4:16] = 12.0
    landcover[8:12, 8:12] = Landcover.GROUND  # the courtyard floor
    chm[8:12, 8:12] = 0.0
    return landcover, chm


def _ids() -> np.ndarray:
    return footprint_ids([box(4.0, 4.0, 16.0, 16.0)], TRANSFORM, SHAPE)


def test_roof_height_vegetation_inside_a_footprint_becomes_building() -> None:
    landcover, chm = _block()
    landcover[5:7, 5:7] = Landcover.VEGETATION  # stray class-5 returns on the tiles
    chm[5:7, 5:7] = 12.4

    flipped = apply_footprint_override(landcover, chm, _ids())

    assert flipped == 4
    assert (landcover[5:7, 5:7] == Landcover.BUILDING).all()


def test_a_courtyard_tree_below_the_eaves_survives() -> None:
    landcover, chm = _block()
    landcover[9:11, 9:11] = Landcover.VEGETATION
    chm[9:11, 9:11] = 6.0  # an orange tree in the patio, well under the roof

    flipped = apply_footprint_override(landcover, chm, _ids())

    assert flipped == 0
    assert (landcover[9:11, 9:11] == Landcover.VEGETATION).all()


def test_vegetation_outside_any_footprint_is_untouched() -> None:
    landcover, chm = _block()
    landcover[1:3, 1:3] = Landcover.VEGETATION  # street tree, taller than the block
    chm[1:3, 1:3] = 14.0

    assert apply_footprint_override(landcover, chm, _ids()) == 0
    assert (landcover[1:3, 1:3] == Landcover.VEGETATION).all()


def test_footprint_without_a_roof_reference_is_skipped() -> None:
    """No building cell inside means no roof height to compare against."""
    landcover = np.zeros(SHAPE, dtype=np.uint8)
    chm = np.zeros(SHAPE, dtype=np.float32)
    landcover[4:16, 4:16] = Landcover.VEGETATION
    chm[4:16, 4:16] = 12.0

    assert apply_footprint_override(landcover, chm, _ids()) == 0
    assert (landcover[4:16, 4:16] == Landcover.VEGETATION).all()


def test_each_footprint_uses_its_own_roof() -> None:
    """A tall block next to a low one: one reference for the city would misjudge both."""
    landcover = np.zeros(SHAPE, dtype=np.uint8)
    chm = np.zeros(SHAPE, dtype=np.float32)
    landcover[2:8, 2:8] = Landcover.BUILDING
    chm[2:8, 2:8] = 20.0
    landcover[12:18, 12:18] = Landcover.BUILDING
    chm[12:18, 12:18] = 6.0
    # 7 m of vegetation: above the low block's roof, far below the tall one's.
    landcover[3:5, 3:5] = Landcover.VEGETATION
    chm[3:5, 3:5] = 7.0
    landcover[13:15, 13:15] = Landcover.VEGETATION
    chm[13:15, 13:15] = 7.0

    ids = footprint_ids([box(2.0, 12.0, 8.0, 18.0), box(12.0, 2.0, 18.0, 8.0)], TRANSFORM, SHAPE)
    apply_footprint_override(landcover, chm, ids)

    assert (landcover[3:5, 3:5] == Landcover.VEGETATION).all()
    assert (landcover[13:15, 13:15] == Landcover.BUILDING).all()


def test_no_footprints_is_a_no_op() -> None:
    landcover, chm = _block()
    landcover[5:7, 5:7] = Landcover.VEGETATION
    chm[5:7, 5:7] = 12.4
    before = landcover.copy()

    assert apply_footprint_override(landcover, chm, footprint_ids([], TRANSFORM, SHAPE)) == 0
    assert (landcover == before).all()
