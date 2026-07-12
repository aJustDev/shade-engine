"""Whole-city shade state raster for one instant.

The API answers shade for one point; the visualization tiles need the same
verdict for every pixel of the city at once. This module vectorizes the
core rule (``shade = sun.elevation < horizon(sun.azimuth)``) over the full
raster instead of calling :func:`shade_core.shade.is_shaded` per pixel.

Two things make the whole-city pass cheap:

- **One sun for the whole city.** Sun azimuth/elevation vary by less than
  0.1 degrees across an 8 km bbox -- under half the horizon quantization
  step (90/255/2 = 0.176 degrees) -- so a single :class:`SunPosition`
  computed at the bbox center is exact for our purposes.
- **Two bands, not sixty-four.** The sun sits between two adjacent azimuth
  sectors; only those two horizon (and blocker-class) bands are read.
  Dequantizing the full horizon cube to float32 would cost ~14 GB at city
  scale; two uint8 bands are ~110 MB.

Parity with the point engine is bit-exact away from float boundaries and is
enforced by tests: the interpolation runs in float32 exactly like
``HorizonGrid.horizon_at``, the final comparison promotes to float64 (core
wraps the interpolated value in ``float()``), and the contributing-sector
tie-break compares raw uint8 bands (the dequantization scale is positive
and monotonic, so order and ties survive quantization).
"""

from pathlib import Path
from typing import Final

import numpy as np
import numpy.typing as npt
import rasterio

from shade_core.artifacts import BLOCKER_CLASS_FILENAME, HORIZON_FILENAME, LANDCOVER_FILENAME
from shade_core.shade import NO_BLOCKER, Landcover
from shade_core.solar import SunPosition

STATE_SUN: Final = 0
STATE_SHADE_BUILDING: Final = 1
STATE_SHADE_VEGETATION: Final = 2
STATE_SHADE_OTHER: Final = 3
"""Shaded, but the blocker is bare ground or open sky (interpolation edge)."""
STATE_OUTSIDE: Final = 255
"""Nodata for pixels outside the city raster; only appears after warping."""


def compute_state_raster(artifact_dir: str | Path, sun: SunPosition) -> npt.NDArray[np.uint8]:
    """Shade state code per pixel of a city's artifacts under a given sun.

    Mirrors :func:`shade_core.shade.is_shaded` decision by decision: canopy
    overrides everything (a pixel under vegetation is vegetation-shaded
    whenever the sun is up), then the horizon comparison, then the blocker
    classification at the contributing sector. Night has no raster: callers
    must not ask (raises ``ValueError``), since every pixel would be NIGHT.
    """
    if not sun.is_up:
        raise ValueError(
            f"sun elevation {sun.elevation_deg:.2f} deg is below the horizon; "
            "night has no shade raster"
        )
    directory = Path(artifact_dir)

    with rasterio.open(directory / HORIZON_FILENAME) as src:
        sectors = src.count
        angle_max_deg = float(src.tags()["angle_max_deg"])
        # Same sector arithmetic as HorizonGrid.horizon_at: the sun's azimuth
        # falls between sectors `lower` and `upper` (wrapping 360 -> 0).
        position = (sun.azimuth_deg % 360.0) / (360.0 / sectors)
        lower = int(position) % sectors
        upper = (lower + 1) % sectors
        fraction = position - int(position)
        # List indexes (3D result, first band taken) instead of an int index:
        # rasterio's single-band path sets the shape in place, which numpy
        # 2.5 deprecates. Same workaround as shade_core.artifacts.
        lower_q = src.read([lower + 1])[0]
        upper_q = src.read([upper + 1])[0]

    # Dequantize and interpolate in float32, matching core's scalar path
    # (python-float scalars stay "weak" under NEP 50, so the ops run in
    # float32). The comparison then promotes to float64 via a *strong*
    # np.float64 scalar: core compares against float(interpolated), and a
    # weak python float here would silently demote the sun's elevation to
    # float32, flipping verdicts on boundary pixels.
    scale = angle_max_deg / 255.0
    horizon = (1.0 - fraction) * (lower_q.astype(np.float32) * np.float32(scale)) + fraction * (
        upper_q.astype(np.float32) * np.float32(scale)
    )
    shaded = np.float64(sun.elevation_deg) < horizon

    with rasterio.open(directory / BLOCKER_CLASS_FILENAME) as src:
        blocker_lower = src.read([lower + 1])[0]
        blocker_upper = src.read([upper + 1])[0]
    # Contributing sector, vectorized: of the two flanking sectors, the one
    # with the higher skyline (ties go to lower, core's `>=`). Comparing the
    # raw uint8 bands is equivalent to comparing the dequantized floats.
    blocker = np.where(lower_q >= upper_q, blocker_lower, blocker_upper)

    state = np.zeros(horizon.shape, dtype=np.uint8)  # STATE_SUN
    state[shaded & (blocker == Landcover.BUILDING)] = STATE_SHADE_BUILDING
    state[shaded & (blocker == Landcover.VEGETATION)] = STATE_SHADE_VEGETATION
    state[shaded & ((blocker == Landcover.GROUND) | (blocker == NO_BLOCKER))] = STATE_SHADE_OTHER

    # Canopy override, applied last: is_shaded checks canopy *before* the
    # horizon, and an unconditional overwrite here yields the same result.
    # read()[0] instead of read(1): rasterio's single-band path reshapes in
    # place, which numpy 2.5 deprecates.
    with rasterio.open(directory / LANDCOVER_FILENAME) as src:
        landcover = src.read()[0]
    state[landcover == Landcover.VEGETATION] = STATE_SHADE_VEGETATION
    return state
