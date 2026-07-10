"""Solar position: where is the sun, as seen from a point on Earth, at an instant?

The sun's position is described by two angles, the same two angles the horizon
raster stores, which is what makes the shade query a plain comparison:

- **Azimuth**: horizontal angle, degrees. Convention here (and in pvlib):
  0 = North, clockwise, so 90 = East, 180 = South, 270 = West. Beware: some
  solar-engineering texts use 0 = South.
- **Elevation**: vertical angle above the horizon, degrees (zenith = 90 -
  elevation). We use the *apparent* elevation, which includes atmospheric
  refraction: the atmosphere bends light so the sun appears ~0.5 degrees
  higher when it sits at the horizon. For shade at dawn/dusk, the apparent
  sun is the one you actually see.

Two more concepts explain the numbers these functions return:

- **Declination**: the sun's angle relative to Earth's equator, oscillating
  between +23.44 (June solstice) and -23.44 (December). Back-of-envelope
  check: solar-noon elevation = 90 - latitude + declination. For Cordoba
  (37.88 N) that gives ~75.6 in June, ~28.7 in December.
- **Equation of time**: Earth's orbit is elliptical and its axis tilted, so
  true solar noon drifts up to +-16 minutes across the year relative to mean
  clock time. On top of that, timezone != solar time: in Cordoba solar noon
  falls around 14:20 CEST. Never assume "noon = 12:00"; ask the ephemeris.

Implementation: pvlib's NREL SPA algorithm (`get_solarposition`), accurate to
fractions of a degree, vectorized over pandas DatetimeIndex.
"""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, tzinfo
from zoneinfo import ZoneInfo

import pandas as pd
from pvlib import solarposition


@dataclass(frozen=True)
class SunPosition:
    """Sun position in the local sky. Azimuth 0 = North, clockwise; degrees."""

    azimuth_deg: float
    elevation_deg: float  # apparent elevation (refraction-corrected)

    @property
    def is_up(self) -> bool:
        """True if the (apparent) sun is above the astronomical horizon."""
        return self.elevation_deg > 0.0


def sun_position(lat: float, lon: float, when: datetime) -> SunPosition:
    """Sun position at one instant. ``when`` must be timezone-aware.

    Naive datetimes are rejected on purpose: resolving "no offset means the
    city's timezone" is an API-layer rule, not a solar-geometry one.
    """
    if when.tzinfo is None:
        raise ValueError("naive datetime: a timezone is required to locate the sun")
    frame = solarposition.get_solarposition(pd.DatetimeIndex([when]), lat, lon)
    row = frame.iloc[0]
    return SunPosition(
        azimuth_deg=float(row["azimuth"]),
        elevation_deg=float(row["apparent_elevation"]),
    )


def sun_positions_for_day(
    lat: float,
    lon: float,
    day: date,
    tz: tzinfo | str,
    step_minutes: int = 5,
) -> list[tuple[datetime, SunPosition]]:
    """Sun positions across one local calendar day, every ``step_minutes``.

    One vectorized SPA call for the whole day (~288 samples at 5 min), which
    is what the daily shade timeline sweeps against. Samples run from local
    00:00 (inclusive) to the next midnight (exclusive).
    """
    zone = ZoneInfo(tz) if isinstance(tz, str) else tz
    start = datetime.combine(day, time.min, tzinfo=zone)
    times = pd.date_range(
        start,
        start + timedelta(days=1),
        freq=f"{step_minutes}min",
        inclusive="left",
    )
    frame = solarposition.get_solarposition(times, lat, lon)
    return [
        (
            stamp.to_pydatetime(),
            SunPosition(azimuth_deg=float(az), elevation_deg=float(el)),
        )
        for stamp, az, el in zip(
            frame.index, frame["azimuth"], frame["apparent_elevation"], strict=True
        )
    ]
