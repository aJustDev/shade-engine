"""The arbiter against geometry that can be worked out on paper."""

import math

import numpy as np
import pytest

import synthetic
from shade_core.solar import SunPosition
from shade_pipeline.arbiter import shade_bracket

# The arbiter reads a padded stack, exactly like a sweep tile: inner sits 50 px
# inside a 160 px square, which covers every ray of the 40 m used below.
PAD = 50
INNER = (PAD, PAD + 60, PAD, PAD + 60)
WALL_ROW_INNER = 30


def _flat_with_wall(height: float = 10.0) -> tuple[np.ndarray, np.ndarray]:
    """Flat ground with one east-west wall, so shade length is analytic."""
    size = 2 * PAD + 60
    dsm = np.zeros((size, size), dtype=np.float32)
    dtm = np.zeros((size, size), dtype=np.float32)
    dsm[PAD + WALL_ROW_INNER, :] = height
    return dsm, dtm


def test_shadow_reaches_the_length_geometry_predicts() -> None:
    """A wall of height h with the sun at elevation e shades (h - eye) / tan(e).

    The sun due south means the shadow falls north, which is toward lower row
    indices. `most` and `least` must bracket that length within the one pixel
    the cell's occupancy can move it.
    """
    height, elevation = 10.0, 45.0
    dsm, dtm = _flat_with_wall(height=height)
    sun = SunPosition(azimuth_deg=180.0, elevation_deg=elevation)
    most, least = shade_bracket(dsm, dtm, INNER, sun, max_distance_m=40.0)

    expected = (height - 1.6) / math.tan(math.radians(elevation))
    column = 30
    shaded_rows = np.flatnonzero(most[:, column])
    # The sun due south puts the shadow north of the wall, toward lower rows.
    reach = WALL_ROW_INNER - shaded_rows.min()
    assert reach == pytest.approx(expected, abs=1.5)
    assert least[:, column].sum() <= most[:, column].sum()


def test_the_bracket_only_disagrees_at_the_shadow_edge() -> None:
    """Entry and exit differ by one cell of reach, never more."""
    dsm, dtm = _flat_with_wall()
    sun = SunPosition(azimuth_deg=180.0, elevation_deg=30.0)
    most, least = shade_bracket(dsm, dtm, INNER, sun, max_distance_m=40.0)
    disagreement = most & ~least
    assert (least & ~most).sum() == 0, "least shade can never exceed most"
    for column in range(60):
        assert disagreement[:, column].sum() <= 1


def test_open_ground_is_never_shaded() -> None:
    size = 2 * PAD + 60
    dsm = np.zeros((size, size), dtype=np.float32)
    dtm = np.zeros((size, size), dtype=np.float32)
    sun = SunPosition(azimuth_deg=137.0, elevation_deg=25.0)
    most, least = shade_bracket(dsm, dtm, INNER, sun, max_distance_m=40.0)
    assert not most.any() and not least.any()


def test_night_is_refused() -> None:
    dsm, dtm = _flat_with_wall()
    with pytest.raises(ValueError, match="below the horizon"):
        shade_bracket(dsm, dtm, INNER, SunPosition(azimuth_deg=0.0, elevation_deg=-5.0))


def test_the_cube_fixture_agrees_with_the_engine_over_open_sky() -> None:
    """On the synthetic cube the arbiter and the sweep must tell one story.

    Not equality: the cube goes through sectors, interpolation and
    quantization, and the arbiter through none of them. But the shaded area at
    a given instant has to land inside the bracket, which is the property the
    whole convention rests on.
    """
    from shade_pipeline.horizon import HorizonParams, compute_horizon_tiled

    dsm, dtm = synthetic.cube_scene()
    landcover = synthetic.cube_landcover()
    params = HorizonParams(max_distance_m=25.0)
    result = compute_horizon_tiled(dsm, dtm, landcover, 1.0, params)

    sun = SunPosition(azimuth_deg=180.0, elevation_deg=40.0)
    inner = (30, 90, 30, 90)
    most, least = shade_bracket(
        dsm.astype(np.float32), dtm.astype(np.float32), inner, sun, max_distance_m=25.0
    )
    scale = 90.0 / 255.0
    position = (sun.azimuth_deg % 360.0) / (360.0 / params.sectors)
    lower = int(position) % params.sectors
    horizon = result.angles_q[lower].astype(np.float32) * scale
    engine = sun.elevation_deg < horizon[30:90, 30:90]

    assert least.sum() <= engine.sum() <= most.sum(), (
        f"engine claims {engine.sum()} shaded pixels, bracket is [{least.sum()}, {most.sum()}]"
    )
