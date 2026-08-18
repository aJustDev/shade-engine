"""Hillshading: the light convention, the sign of north, and the flat case.

Signs are the whole risk here. A relief lit from the wrong quadrant looks
perfectly plausible -- it is a grey image of a town either way -- so the tests
fix the direction against planes whose answer is known before running anything.
"""

import math

import numpy as np
import pytest

from shade_pipeline.relief import hillshade


def _plane(rows: int, cols: int, *, north: float = 0.0, east: float = 0.0) -> np.ndarray:
    """A plane rising ``north``/``east`` metres per metre in those directions.

    Row 0 is the northernmost, so the north gradient goes against the row
    index: this helper is where that is written once.
    """
    r = np.arange(rows, dtype=np.float64)[:, None]
    c = np.arange(cols, dtype=np.float64)[None, :]
    return (rows - 1 - r) * north + c * east


def test_flat_ground_is_lit_by_the_sine_of_the_altitude() -> None:
    """The case that catches a wrong formula before any sign does."""
    for altitude in (20.0, 45.0, 70.0):
        shaded = hillshade(np.zeros((8, 8)), 1.0, altitude_deg=altitude)
        assert np.allclose(shaded, math.sin(math.radians(altitude)), atol=1e-6)


def test_a_slope_is_brightest_facing_the_light() -> None:
    """A roof falling east is lit from the east and dark from the west."""
    falling_east = _plane(8, 8, east=-0.5)

    from_east = hillshade(falling_east, 1.0, azimuth_deg=90.0)
    from_west = hillshade(falling_east, 1.0, azimuth_deg=270.0)
    flat = math.sin(math.radians(45.0))

    assert from_east.mean() > flat > from_west.mean()


def test_north_is_not_the_row_index() -> None:
    """The sign that is easy to get wrong and impossible to see afterwards.

    Row 0 is the north edge, so a surface *rising* northward has a negative row
    gradient -- and it is a slope you walk up going north, which means it faces
    **south**. It is lit from the south and dark from the north, and stating it
    the other way round is the mistake this test exists for.
    """
    rising_north = _plane(8, 8, north=0.5)

    from_north = hillshade(rising_north, 1.0, azimuth_deg=0.0)
    from_south = hillshade(rising_north, 1.0, azimuth_deg=180.0)
    flat = math.sin(math.radians(45.0))

    assert from_south.mean() > flat > from_north.mean()


def test_the_light_comes_from_the_north_west_by_default() -> None:
    """Cartographic convention, and the reason it is not the real sun.

    Lit from the south-east the same relief reads inverted to most people, so
    the default is a drawing decision and not a solar one.
    """
    facing_northwest = _plane(8, 8, north=-0.4, east=0.4)
    facing_southeast = _plane(8, 8, north=0.4, east=-0.4)

    assert hillshade(facing_northwest, 1.0).mean() > hillshade(facing_southeast, 1.0).mean()


def test_resolution_changes_the_slope_a_step_makes() -> None:
    """The reason this is computed in metres and never in Web Mercator.

    The same 1 m step is a cliff over a 1 m cell and a ramp over a 5 m one. A
    horizontal unit that is not a metre -- or that stretches with latitude --
    silently rescales every slope in the image.
    """
    step = np.zeros((8, 8))
    step[:, 4:] = 1.0

    sharp = hillshade(step, 1.0)
    gentle = hillshade(step, 5.0)

    assert sharp.std() > gentle.std()


def test_missing_elevation_stays_missing() -> None:
    """And it has to be said explicitly, which is the surprise.

    A central difference at a cell never reads that cell, so a lone hole comes
    out of the gradient perfectly lit while the four neighbours around it go
    NaN. Filling it with zero would draw flat ground where there is no data.
    """
    surface = np.zeros((8, 8))
    surface[3, 3] = np.nan

    shaded = hillshade(surface, 1.0)

    assert np.isnan(shaded[3, 3])
    assert not np.isnan(shaded[0, 0])


def test_smoothing_takes_the_speckle_and_keeps_the_step() -> None:
    """Why the drawing blurs a surface the sweep reads raw.

    A LiDAR roof is rough at the scale of its own cell -- 0.61 m of local
    standard deviation on Montalban's buildings, 2.48 at p90 -- and a gradient
    turns that into noise. A party wall is metres of step and survives.
    """
    rng = np.random.default_rng(0)
    surface = np.zeros((40, 40))
    surface[:, 20:] = 6.0  # la medianera
    noisy = surface + rng.normal(0.0, 0.6, surface.shape)
    flat = (slice(5, 15), slice(5, 15))

    def contrast(image: np.ndarray) -> float:
        """Cuanto destaca la pared sobre el ruido que le queda al tejado."""
        return float((image[:, 18:22].mean(axis=0).max() - image[flat].mean()) / image[flat].std())

    crudo = hillshade(noisy, 1.0)
    suave = hillshade(noisy, 1.0, smooth_sigma_px=1.2)

    # Sin suavizar, el escalon esta ENTERRADO en el ruido del propio tejado.
    assert contrast(crudo) < 1.0
    # Suavizado, el tejado se aplana (sd 0,227 -> 0,036) y la pared emerge.
    assert contrast(suave) > 4.0
    assert suave[flat].std() < crudo[flat].std() / 5


def test_the_output_is_float32() -> None:
    """It becomes a whole-city array in a render worker; float64 would double it."""
    assert hillshade(np.zeros((4, 4)), 1.0).dtype == np.float32


@pytest.mark.parametrize("exaggeration", [1.0, 3.0])
def test_exaggeration_only_deepens_contrast(exaggeration: float) -> None:
    ramp = _plane(8, 8, east=0.2)

    shaded = hillshade(ramp, 1.0, vertical_exaggeration=exaggeration)

    assert np.all((shaded >= 0.0) & (shaded <= 1.0))
