"""Shade engine: combine sun position and horizon into a verdict.

The core rule is one comparison -- a point is shaded when the sun sits below
that pixel's horizon toward the sun's azimuth::

    shade = sun.elevation < horizon(sun.azimuth)

Two refinements around it:

- **Day/night vs sun/shade**: elevation <= 0 means the sun is below the
  *astronomical* horizon (night) -- a different question from "is it below
  this pixel's *local* skyline" (shade).
- **Under a canopy the horizon lies**: the horizon is computed from street
  level, but a pixel whose landcover says "vegetation overhead" is shaded by
  that very canopy whenever the sun is up (opaque-canopy MVP assumption).

Shade *type* asks which obstacle blocks the sun. The reference answer
ray-marches from the observer toward the sun's azimuth until the first pixel
whose top edge reaches above the sun's elevation, then reads its landcover
class. Whether production uses this ray-march over COG windows or a
precomputed per-sector class raster is an open phase-2 decision.

Naming the winner is not the same as asking *what would still be shade
without it*: a wall and a crown often cover the same pixel and the argmax
picks one by centimetres. So a scene can carry a second horizon built over a
vegetation-free surface, and a pixel whose blocker is a crown but whose sky is
closed by a building anyway comes back as :attr:`ShadeType.BOTH`.
"""

import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta, tzinfo
from enum import IntEnum, StrEnum
from typing import Final

import numpy as np
import numpy.typing as npt

from shade_core.horizon import HorizonGrid
from shade_core.raycast import ray_cells
from shade_core.solar import SunPosition, sun_positions_for_day


class Landcover(IntEnum):
    """Raster classes derived from LiDAR point classification (stored uint8)."""

    GROUND = 0
    VEGETATION = 1
    BUILDING = 2


NO_BLOCKER: Final = 255
"""Sector-class value meaning open sky: nothing raises this sector's horizon."""


class ShadeState(StrEnum):
    SUN = "sun"
    SHADE = "shade"
    NIGHT = "night"


class ShadeType(StrEnum):
    BUILDING = "building"
    VEGETATION = "vegetation"
    BOTH = "both"
    """Vegetation blocks the sun here, and so would the skyline without it."""


_SHADE_TYPE_BY_LANDCOVER = {
    Landcover.BUILDING: ShadeType.BUILDING,
    Landcover.VEGETATION: ShadeType.VEGETATION,
}


@dataclass(frozen=True)
class ShadeScene:
    """A place the engine can answer questions about.

    All optional arrays share the horizon grid's georeference: ``landcover``
    holds :class:`Landcover` codes, ``canopy`` marks pixels lying *under*
    vegetation, ``sector_classes`` is the pipeline's per-sector blocker
    class cube (same shape as the horizon, ``NO_BLOCKER`` = open sky), and
    ``dsm``/``dtm`` feed the reference ray-march. Shade classification uses
    ``sector_classes`` when present, else the ray-march; with neither,
    queries still work but shade comes back untyped.

    ``horizon_noveg`` is the same skyline over a surface with vegetation
    lowered to the terrain. Without it nothing breaks and nothing is ever
    typed :attr:`ShadeType.BOTH`, which is what artifacts built before the
    second cube existed get.
    """

    horizon: HorizonGrid
    landcover: npt.NDArray[np.uint8] | None = None
    canopy: npt.NDArray[np.bool_] | None = None
    dsm: npt.NDArray[np.float64] | None = None
    dtm: npt.NDArray[np.float64] | None = None
    sector_classes: npt.NDArray[np.uint8] | None = None
    horizon_noveg: HorizonGrid | None = None
    observer_height_m: float = 1.6


@dataclass(frozen=True)
class ShadeResult:
    state: ShadeState
    shade_type: ShadeType | None = None


@dataclass(frozen=True)
class ShadeInterval:
    """Half-open [start, end) stretch of constant state during daylight."""

    start: datetime
    end: datetime
    state: ShadeState  # SUN or SHADE, never NIGHT
    shade_type: ShadeType | None


def is_shaded(scene: ShadeScene, x: float, y: float, sun: SunPosition) -> ShadeResult:
    """Shade verdict for a point (projected CRS meters) under a given sun."""
    if not sun.is_up:
        return ShadeResult(ShadeState.NIGHT)
    row, col = scene.horizon.rowcol(x, y)
    if scene.canopy is not None and bool(scene.canopy[row, col]):
        # Under a crown, but the wall across the street may be closing the sky
        # regardless: that is BOTH, not VEGETATION.
        under_canopy = (
            ShadeType.BOTH if holds_without_trees(scene, x, y, sun) else ShadeType.VEGETATION
        )
        return ShadeResult(ShadeState.SHADE, under_canopy)
    if sun.elevation_deg < scene.horizon.horizon_at(x, y, sun.azimuth_deg):
        return ShadeResult(ShadeState.SHADE, classify_shade(scene, x, y, sun))
    return ShadeResult(ShadeState.SUN)


def holds_without_trees(scene: ShadeScene, x: float, y: float, sun: SunPosition) -> bool:
    """Would the sun still be blocked here with every crown felled?

    False whenever the scene has no vegetation-free horizon: never claim a
    counterfactual the artifacts cannot support.
    """
    if scene.horizon_noveg is None:
        return False
    return sun.elevation_deg < scene.horizon_noveg.horizon_at(x, y, sun.azimuth_deg)


def classify_shade(scene: ShadeScene, x: float, y: float, sun: SunPosition) -> ShadeType | None:
    """What casts the shade here, and would it hold without the trees?

    Names the blocker (below), then promotes ``VEGETATION`` to
    :attr:`ShadeType.BOTH` when the vegetation-free horizon closes the sky at
    this azimuth too. ``BUILDING`` needs no such check: if a building won the
    argmax, the vegetation-free skyline reaches at least that angle by
    construction.
    """
    shade_type = _blocker_type(scene, x, y, sun)
    if shade_type is ShadeType.VEGETATION and holds_without_trees(scene, x, y, sun):
        return ShadeType.BOTH
    return shade_type


def _blocker_type(scene: ShadeScene, x: float, y: float, sun: SunPosition) -> ShadeType | None:
    """Which obstacle wins the sun's azimuth? Two strategies, picked by the scene.

    **Per-sector blocker classes** (the production artifact): the pipeline's
    horizon sweep already recorded which landcover class produced each
    sector's max angle, so the answer is one lookup at the *contributing
    sector* -- of the two sectors flanking the sun's azimuth, the one whose
    skyline drove the interpolated shade verdict.

    **Reference ray-march** (needs dsm + dtm + landcover): walk from the
    observer's eye (DTM + observer height) toward the sun's azimuth; the
    first pixel whose surface top subtends an angle >= the sun's elevation
    is the blocker. Near shade boundaries the exact ray can find nothing
    even though the interpolated horizon says shade (azimuth interpolation
    smears obstacle edges by up to half a sector, ~2.8 degrees at 64), so it
    falls back to re-marching along the contributing sector's azimuth.

    Returns None when the scene lacks the needed arrays, or when the blocker
    is bare ground / open sky / beyond the grid.
    """
    grid = scene.horizon
    if scene.sector_classes is not None:
        row, col = grid.rowcol(x, y)
        sector = _contributing_sector(grid, x, y, sun.azimuth_deg)
        value = int(scene.sector_classes[sector, row, col])
        if value == NO_BLOCKER:
            return None
        return _SHADE_TYPE_BY_LANDCOVER.get(Landcover(value))
    if scene.dsm is None or scene.dtm is None or scene.landcover is None:
        return None
    row, col = grid.rowcol(x, y)
    observer_z = float(scene.dtm[row, col]) + scene.observer_height_m

    blocker = _ray_march_blocker(scene, x, y, sun.azimuth_deg, sun.elevation_deg, observer_z)
    if blocker is not None:
        return blocker
    sector_width = 360.0 / grid.sectors
    contributing = _contributing_sector(grid, x, y, sun.azimuth_deg)
    return _ray_march_blocker(
        scene, x, y, contributing * sector_width, sun.elevation_deg, observer_z
    )


def _contributing_sector(grid: HorizonGrid, x: float, y: float, azimuth_deg: float) -> int:
    """Of the two sectors flanking ``azimuth_deg``, the one with the higher skyline."""
    profile = grid.profile_at(x, y)
    sector_width = 360.0 / grid.sectors
    lower = int((azimuth_deg % 360.0) / sector_width) % grid.sectors
    upper = (lower + 1) % grid.sectors
    return lower if profile[lower] >= profile[upper] else upper


def _ray_march_blocker(
    scene: ShadeScene,
    x: float,
    y: float,
    azimuth_deg: float,
    elevation_deg: float,
    observer_z: float,
) -> ShadeType | None:
    assert scene.dsm is not None and scene.landcover is not None
    grid = scene.horizon
    rows, cols = scene.dsm.shape
    row, col = grid.rowcol(x, y)
    # The cells the ray really crosses, each at the distance it is entered --
    # the same convention as the sweep and the oracle (ADR-027). Marching a
    # fixed step instead would both skip cells the ray goes through and put
    # them at the wrong distance. The radius is the grid's diagonal, so the
    # walk ends by leaving the block, exactly as the old loop did.
    reach = math.hypot(rows, cols) * grid.resolution_m
    for d_row, d_col, distance, _exit in ray_cells(azimuth_deg, reach, grid.resolution_m):
        r, c = row + d_row, col + d_col
        if not (0 <= r < rows and 0 <= c < cols):
            return None
        angle = math.degrees(math.atan2(float(scene.dsm[r, c]) - observer_z, distance))
        if angle >= elevation_deg:
            return _SHADE_TYPE_BY_LANDCOVER.get(Landcover(int(scene.landcover[r, c])))
    return None


def shade_timeline(
    scene: ShadeScene,
    x: float,
    y: float,
    lat: float,
    lon: float,
    day: date,
    tz: tzinfo | str,
    step_minutes: int = 5,
) -> list[ShadeInterval]:
    """Sun/shade intervals across one local calendar day, daylight only.

    Sweeps the day's sun positions every ``step_minutes`` and merges
    consecutive samples with the same (state, shade_type) into intervals.
    Consecutive intervals share their boundary exactly; boundary times are
    accurate to one step (bisection refinement is a future improvement if
    field validation asks for it). Night is not part of the result: the
    first interval starts at the first sample with the sun up.
    """
    samples = sun_positions_for_day(lat, lon, day, tz, step_minutes)
    intervals: list[ShadeInterval] = []
    current: tuple[datetime, ShadeState, ShadeType | None] | None = None

    def close(end: datetime) -> None:
        nonlocal current
        if current is not None:
            start, state, shade_type = current
            intervals.append(ShadeInterval(start, end, state, shade_type))
            current = None

    for when, sun in samples:
        result = is_shaded(scene, x, y, sun)
        if result.state is ShadeState.NIGHT:
            close(when)
            continue
        if current is not None and (current[1], current[2]) == (result.state, result.shade_type):
            continue
        close(when)
        current = (when, result.state, result.shade_type)
    close(samples[-1][0] + timedelta(minutes=step_minutes))
    return intervals
