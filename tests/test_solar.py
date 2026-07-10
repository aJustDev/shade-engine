"""Solar position against external references.

The oracle is independent of pvlib: solar-noon elevation = 90 - latitude +
declination, with declination = +23.44 (June solstice), -23.44 (December
solstice), ~0 (equinox). Cross-checked with the NOAA Solar Calculator
(https://gml.noaa.gov/grad/solcalc/) for Cordoba.
"""

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from shade_core.solar import sun_position, sun_positions_for_day

CORDOBA_LAT = 37.88
CORDOBA_LON = -4.78
MADRID = ZoneInfo("Europe/Madrid")


def noon_sample(day: date) -> tuple[datetime, float, float]:
    """(time, azimuth, elevation) of the highest 1-minute sample of the day."""
    samples = sun_positions_for_day(CORDOBA_LAT, CORDOBA_LON, day, MADRID, step_minutes=1)
    when, sun = max(samples, key=lambda item: item[1].elevation_deg)
    return when, sun.azimuth_deg, sun.elevation_deg


@pytest.mark.parametrize(
    ("day", "expected_elevation"),
    [
        (date(2026, 6, 21), 90 - CORDOBA_LAT + 23.44),  # ~75.6
        (date(2026, 12, 21), 90 - CORDOBA_LAT - 23.44),  # ~28.7
        (date(2026, 3, 20), 90 - CORDOBA_LAT),  # equinox, ~52.1
    ],
)
def test_solar_noon_elevation(day: date, expected_elevation: float) -> None:
    _, _, elevation = noon_sample(day)
    assert elevation == pytest.approx(expected_elevation, abs=0.5)


def test_sun_transits_due_south() -> None:
    _, azimuth, _ = noon_sample(date(2026, 6, 21))
    assert azimuth == pytest.approx(180.0, abs=2.0)


def test_solar_noon_is_not_clock_noon() -> None:
    # Europe/Madrid runs ahead of Cordoba's sun: solar noon ~14:20 CEST.
    when, _, _ = noon_sample(date(2026, 6, 21))
    assert when.hour == 14


def test_night_sun_is_down() -> None:
    sun = sun_position(CORDOBA_LAT, CORDOBA_LON, datetime(2026, 6, 21, 1, 0, tzinfo=MADRID))
    assert not sun.is_up


def test_naive_datetime_rejected() -> None:
    with pytest.raises(ValueError, match="naive"):
        sun_position(CORDOBA_LAT, CORDOBA_LON, datetime(2026, 6, 21, 12, 0))


def test_day_sweep_matches_point_query() -> None:
    samples = sun_positions_for_day(CORDOBA_LAT, CORDOBA_LON, date(2026, 6, 21), MADRID)
    when, sun = samples[len(samples) // 2]
    point = sun_position(CORDOBA_LAT, CORDOBA_LON, when)
    assert point.azimuth_deg == pytest.approx(sun.azimuth_deg, abs=1e-6)
    assert point.elevation_deg == pytest.approx(sun.elevation_deg, abs=1e-6)
