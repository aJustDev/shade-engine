"""Daily timeline invariants over the cube scene.

At 10 m north of the cube on the winter solstice the geometry forces exactly
three daylight intervals: sun rises at azimuth ~120 (the cube only blocks
azimuths ~135-225 from this point), the long midday stretch is building
shade (shadow ~33.6 m > 10 m), and the evening frees the point again.
"""

from datetime import date, datetime, timedelta
from itertools import pairwise
from zoneinfo import ZoneInfo

import synthetic
from shade_core.shade import ShadeScene, ShadeState, ShadeType, shade_timeline
from shade_core.solar import sun_position

CORDOBA_LAT = 37.88
CORDOBA_LON = -4.78
TZ = "Europe/Madrid"

WINTER = date(2026, 12, 21)
SUMMER = date(2026, 6, 21)
NEAR = (synthetic.QUERY_X, synthetic.CUBE_NORTH_WALL_Y + 10)


def test_intervals_are_contiguous_and_daylight_only(cube_shade_scene: ShadeScene) -> None:
    for day in (WINTER, SUMMER):
        intervals = shade_timeline(cube_shade_scene, *NEAR, CORDOBA_LAT, CORDOBA_LON, day, TZ)
        assert intervals
        for left, right in pairwise(intervals):
            assert left.end == right.start
        assert all(i.state is not ShadeState.NIGHT for i in intervals)
        assert all(i.start < i.end for i in intervals)


def test_timeline_starts_at_sunrise_and_ends_at_sunset(cube_shade_scene: ShadeScene) -> None:
    intervals = shade_timeline(cube_shade_scene, *NEAR, CORDOBA_LAT, CORDOBA_LON, WINTER, TZ)
    first, last = intervals[0], intervals[-1]
    assert sun_position(CORDOBA_LAT, CORDOBA_LON, first.start).is_up
    assert not sun_position(CORDOBA_LAT, CORDOBA_LON, first.start - timedelta(minutes=5)).is_up
    assert not sun_position(CORDOBA_LAT, CORDOBA_LON, last.end).is_up


def test_winter_day_is_sun_shade_sun(cube_shade_scene: ShadeScene) -> None:
    intervals = shade_timeline(cube_shade_scene, *NEAR, CORDOBA_LAT, CORDOBA_LON, WINTER, TZ)
    assert [i.state for i in intervals] == [ShadeState.SUN, ShadeState.SHADE, ShadeState.SUN]
    shade = intervals[1]
    assert shade.shade_type is ShadeType.BUILDING
    # The building shade must cover solar noon (~13:20 CET) and last hours.
    solar_noon = datetime(2026, 12, 21, 13, 20, tzinfo=ZoneInfo(TZ))
    assert shade.start <= solar_noon < shade.end
    assert (shade.end - shade.start) > timedelta(hours=3)


def test_summer_noon_is_sunny_in_timeline(cube_shade_scene: ShadeScene) -> None:
    intervals = shade_timeline(cube_shade_scene, *NEAR, CORDOBA_LAT, CORDOBA_LON, SUMMER, TZ)
    # Solar noon falls ~14:20 CEST; the interval containing it must be SUN.
    solar_noon = datetime(2026, 6, 21, 14, 20, tzinfo=ZoneInfo(TZ))
    containing = next(i for i in intervals if i.start <= solar_noon < i.end)
    assert containing.state is ShadeState.SUN
