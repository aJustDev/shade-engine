"""DSM decluttering: cables flattened, awnings dropped, the city left alone."""

import numpy as np
import numpy.typing as npt
from numpy.testing import assert_allclose

from shade_core.shade import Landcover
from shade_pipeline.declutter import declutter_dsm


def _ground(
    size: int = 40,
) -> tuple[npt.NDArray[np.float32], npt.NDArray[np.float32], npt.NDArray[np.uint8]]:
    """Flat terrain at 0 m: dsm == dtm, all GROUND, nothing standing anywhere."""
    return (
        np.zeros((size, size), dtype=np.float32),
        np.zeros((size, size), dtype=np.float32),
        np.full((size, size), Landcover.GROUND, dtype=np.uint8),
    )


def test_cable_over_a_square_is_flattened() -> None:
    """The Corredera case: 14 m of line at 10 m, over nothing."""
    dsm, dtm, landcover = _ground()
    dsm[20, 5:19] = 10.0

    result = declutter_dsm(dsm, dtm, landcover)

    assert result.linear_px == 14
    assert_allclose(dsm[20, 5:19], 0.0)


def test_a_building_keeps_every_metre_it_had() -> None:
    """The rule that makes this safe: wide structures survive the opening whole.

    Edges included -- a roof edge is exactly where a naive protrusion test
    would find a thin, long region and delete the outline of the building.
    """
    dsm, dtm, landcover = _ground()
    dsm[8:28, 8:28] = 15.0
    landcover[8:28, 8:28] = Landcover.BUILDING
    before = dsm.copy()

    result = declutter_dsm(dsm, dtm, landcover)

    assert result.total_px == 0
    assert_allclose(dsm, before)


def test_a_ridge_standing_on_a_roof_survives() -> None:
    """Thin, long and protruding, but not over open ground: it stays.

    A party wall proud of the roofs beside it is indistinguishable from a
    cable seen from above, and it is permanent fabric. What separates them is
    what they hang over, so that is what the rule asks. Measured on
    cordoba-test, this guard spares 18 regions of 407.
    """
    dsm, dtm, landcover = _ground()
    dsm[8:28, 8:28] = 15.0
    landcover[8:28, 8:28] = Landcover.BUILDING
    dsm[18, 10:26] = 19.0  # a wall running along the roof
    before = dsm.copy()

    result = declutter_dsm(dsm, dtm, landcover)

    assert result.linear_px == 0
    assert_allclose(dsm, before)


def test_cable_anchored_to_a_roof_loses_the_part_over_the_square() -> None:
    """Protrusion, not height, is what gets labelled -- and that is the point.

    The cable leaves the roof at roof height, so no height threshold could
    separate the two. The overhang is over paving, and that half goes.
    """
    dsm, dtm, landcover = _ground()
    dsm[8:28, 8:20] = 15.0
    landcover[8:28, 8:20] = Landcover.BUILDING
    dsm[15, 20:34] = 15.0  # a line at roof height, running out of the roof

    result = declutter_dsm(dsm, dtm, landcover)

    assert result.linear_px == 14
    assert_allclose(dsm[15, 20:34], 0.0)
    assert_allclose(dsm[8:28, 8:20], 15.0)


def test_a_narrow_tall_building_survives_because_osm_drew_it() -> None:
    """Two pixels wide, twenty long, 26 m over its own street: a cable's shape.

    Nothing in a surface model separates an exposed party wall from a cable
    strung at the same height. Somebody drawing the building does, and this is
    the guard with the most reach: the linear rule is the one that removes
    real height, up to 28 m of it on cordoba-test.
    """
    dsm, dtm, landcover = _ground()
    dsm[10:30, 20:22] = 26.0
    landcover[10:30, 20:22] = Landcover.BUILDING
    built = np.zeros(dsm.shape, dtype=bool)
    built[10:30, 20:22] = True
    before = dsm.copy()

    assert declutter_dsm(dsm.copy(), dtm, landcover).linear_px == 40  # without the guard

    result = declutter_dsm(dsm, dtm, landcover, built)

    assert result.linear_px == 0
    assert_allclose(dsm, before)


def test_awning_over_paving_drops_to_the_terrain() -> None:
    dsm, dtm, landcover = _ground()
    dsm[10:22, 10:25] = 2.6  # 12 x 15 plane, one metre flat
    landcover[10:22, 10:25] = Landcover.VEGETATION  # what PNOA calls it

    result = declutter_dsm(dsm, dtm, landcover)

    assert result.slab_px == 12 * 15
    assert_allclose(dsm, 0.0)


def test_a_shed_the_classifier_recognised_survives() -> None:
    """Same shape as an awning; the building label is trusted to protect.

    The classifier's vegetation label is the unreliable one -- that is why
    this module exists -- but a flat roof is what a geometric classifier is
    good at. So the label is allowed to spare and never to accuse.
    """
    dsm, dtm, landcover = _ground()
    dsm[10:22, 10:25] = 2.6
    landcover[10:22, 10:25] = Landcover.BUILDING
    before = dsm.copy()

    result = declutter_dsm(dsm, dtm, landcover)

    assert result.slab_px == 0
    assert_allclose(dsm, before)


def test_a_pruned_crown_at_awning_height_survives() -> None:
    """Same height, same footprint, but rough: the threshold that matters."""
    dsm, dtm, landcover = _ground()
    crown = 2.6 + np.random.default_rng(0).normal(0.0, 0.3, size=(12, 15)).astype(np.float32)
    dsm[10:22, 10:25] = crown
    landcover[10:22, 10:25] = Landcover.VEGETATION
    assert 0.25 < float(crown.std()) < 0.35

    result = declutter_dsm(dsm, dtm, landcover)

    assert result.slab_px == 0
    assert_allclose(dsm[10:22, 10:25], crown)


def test_a_flat_courtyard_inside_a_building_survives() -> None:
    """Flat and low, but ringed by roof rather than by paving: it stays.

    This is what ``SLAB_GROUND_FRACTION`` is for. Height and flatness alone
    cannot tell an awning in a square from a low structure enclosed by the
    block it belongs to; what is around it can.
    """
    dsm, dtm, landcover = _ground()
    dsm[8:30, 8:30] = 12.0  # the block
    landcover[8:30, 8:30] = Landcover.BUILDING
    dsm[14:24, 14:24] = 2.0  # a low, flat courtyard inside it
    landcover[14:24, 14:24] = Landcover.VEGETATION
    before = dsm.copy()

    result = declutter_dsm(dsm, dtm, landcover)

    assert result.slab_px == 0
    assert_allclose(dsm, before)


def test_nothing_to_do_on_bare_ground() -> None:
    dsm, dtm, landcover = _ground()

    result = declutter_dsm(dsm, dtm, landcover)

    assert result.total_px == 0
    assert_allclose(dsm, 0.0)
