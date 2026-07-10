"""The spec's golden tests: hand-checkable shade around a 20 m cube and a tree.

Oracle independent of the horizon machinery: the shadow of a wall of height h
reaches (h - 1.6) / tan(elevation) meters from it, using pvlib's elevation.
At Cordoba's winter-solstice noon (~28.7 deg) the cube shades ~33.6 m; at the
summer noon (~75.6 deg) only ~4.7 m. Points at 10 m and 60 m north of the
wall therefore flip verdicts between seasons in a way we can predict exactly.
"""

import math
from datetime import date, datetime
from zoneinfo import ZoneInfo

import synthetic
from shade_core.shade import ShadeScene, ShadeState, ShadeType, is_shaded
from shade_core.solar import SunPosition, sun_positions_for_day

CORDOBA_LAT = 37.88
CORDOBA_LON = -4.78
MADRID = ZoneInfo("Europe/Madrid")

WINTER = date(2026, 12, 21)
SUMMER = date(2026, 6, 21)

NEAR = (synthetic.QUERY_X, synthetic.CUBE_NORTH_WALL_Y + 10)
FAR = (synthetic.QUERY_X, synthetic.CUBE_NORTH_WALL_Y + 60)


def solar_noon(day: date) -> SunPosition:
    samples = sun_positions_for_day(CORDOBA_LAT, CORDOBA_LON, day, MADRID, step_minutes=1)
    return max(samples, key=lambda item: item[1].elevation_deg)[1]


def shadow_reach(height: float, sun: SunPosition) -> float:
    return (height - 1.6) / math.tan(math.radians(sun.elevation_deg))


def test_winter_noon_shades_the_near_point(cube_shade_scene: ShadeScene) -> None:
    sun = solar_noon(WINTER)
    assert shadow_reach(synthetic.CUBE_HEIGHT, sun) > 10  # oracle: ~33.6 m
    result = is_shaded(cube_shade_scene, *NEAR, sun)
    assert result.state is ShadeState.SHADE
    assert result.shade_type is ShadeType.BUILDING


def test_summer_noon_frees_the_near_point(cube_shade_scene: ShadeScene) -> None:
    sun = solar_noon(SUMMER)
    assert shadow_reach(synthetic.CUBE_HEIGHT, sun) < 10  # oracle: ~4.7 m
    assert is_shaded(cube_shade_scene, *NEAR, sun).state is ShadeState.SUN


def test_far_point_is_sunny_at_both_noons(cube_shade_scene: ShadeScene) -> None:
    for day in (WINTER, SUMMER):
        sun = solar_noon(day)
        assert shadow_reach(synthetic.CUBE_HEIGHT, sun) < 60
        assert is_shaded(cube_shade_scene, *FAR, sun).state is ShadeState.SUN


def test_night_wins_over_everything(cube_shade_scene: ShadeScene) -> None:
    down = SunPosition(azimuth_deg=10.0, elevation_deg=-5.0)
    assert is_shaded(cube_shade_scene, *NEAR, down).state is ShadeState.NIGHT


def test_under_canopy_is_vegetation_shade_all_day(tree_shade_scene: ShadeScene) -> None:
    for hour in (9, 14, 19):
        when = datetime(2026, 6, 21, hour, 0, tzinfo=MADRID)
        samples = sun_positions_for_day(CORDOBA_LAT, CORDOBA_LON, SUMMER, MADRID, 60)
        sun = next(s for t, s in samples if t == when)
        assert sun.is_up
        result = is_shaded(tree_shade_scene, *synthetic.CANOPY_CENTER, sun)
        assert result.state is ShadeState.SHADE
        assert result.shade_type is ShadeType.VEGETATION


def test_tree_shadow_is_classified_vegetation(tree_shade_scene: ShadeScene) -> None:
    # 6 m north of the canopy edge: horizon toward it is atan(6.4/6) ~ 46.8,
    # above the winter noon sun (~28.7) and below the summer one (~75.6).
    point = (synthetic.CANOPY_CENTER[0], synthetic.CANOPY_NORTH_Y + 6)
    winter = is_shaded(tree_shade_scene, *point, solar_noon(WINTER))
    assert winter.state is ShadeState.SHADE
    assert winter.shade_type is ShadeType.VEGETATION
    assert is_shaded(tree_shade_scene, *point, solar_noon(SUMMER)).state is ShadeState.SUN


def test_bare_horizon_gives_untyped_shade(cube_shade_scene: ShadeScene) -> None:
    bare = ShadeScene(horizon=cube_shade_scene.horizon)
    result = is_shaded(bare, *NEAR, solar_noon(WINTER))
    assert result.state is ShadeState.SHADE
    assert result.shade_type is None
