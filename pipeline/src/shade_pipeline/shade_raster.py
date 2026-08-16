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

The blocker class alone cannot split shade into layers a viewer can toggle:
it names whichever obstacle won the argmax, so a pixel covered by a wall *and*
a crown lands in one bucket by centimetres, and hiding the trees erases shade
that the wall casts anyway. The vegetation-free horizon answers the actual
question -- would this be shade with no trees -- and splits cast shade into
"holds without trees" and "only the trees hold it". ``STATE_SHADE_BOTH`` keeps
the first group's crowns visible to consumers that care about comfort (the
route graph) without merging the two questions.

Parity with the point engine is bit-exact away from float boundaries and is
enforced by tests: the interpolation runs in float32 exactly like
``HorizonGrid.horizon_at``, the final comparison promotes to float64 (core
wraps the interpolated value in ``float()``), and the contributing-sector
tie-break compares raw uint8 bands (the dequantization scale is positive
and monotonic, so order and ties survive quantization).
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Final

import numpy as np
import numpy.typing as npt
import rasterio
from scipy import ndimage

from shade_core.artifacts import (
    BLOCKER_CLASS_FILENAME,
    CANOPY_FILENAME,
    HORIZON_FILENAME,
    HORIZON_NOVEG_FILENAME,
    LANDCOVER_FILENAME,
    load_coverage,
)
from shade_core.shade import NO_BLOCKER, Landcover
from shade_core.solar import SunPosition

STATE_SUN: Final = 0
STATE_SHADE_BUILDING: Final = 1
STATE_SHADE_VEGETATION: Final = 2
"""Shaded, and only the trees hold it: fell them and the sun reaches here."""
STATE_SHADE_OTHER: Final = 3
"""Shaded, but the blocker is bare ground or open sky (interpolation edge)."""
STATE_SHADE_BOTH: Final = 4
"""Shaded by a crown, and by the skyline that would remain without it.

Never reaches a tile: ``tiles.py`` folds it into ``STATE_SHADE_BUILDING`` when
writing the building set, so the PNG palette keeps its five entries.
"""
STATE_OUTSIDE: Final = 255
"""Nodata for pixels outside the city raster; only appears after warping."""


def signed_distance(mask: npt.NDArray[np.bool_]) -> npt.NDArray[np.float32]:
    """Distance inside minus distance outside, in pixels; zero on the boundary.

    A categorical mask has no continuous field to resample, so one is
    manufactured. Interpolating *this* and thresholding at zero puts the
    outline at a sub-pixel position and rounds convex corners with a radius of
    about one source pixel, instead of stepping in whole metres.

    Honest about what each layer buys. For a **crown** it is an accuracy gain:
    a crown is round, and a staircase crown is the less true of the two. For a
    **roof** it is neither better nor worse -- the 1 m staircase is not the
    real corner either, only a discretization of it -- so the choice is which
    approximation to show, and a curve at least does not claim to be a
    measurement. See ``shade-docs: learning/borde-subpixel.md``.
    """
    # One transform at a time, each demoted to float32 before the next runs:
    # scipy answers in float64, and holding two of those over a city-sized
    # raster is four times the memory of the result for no extra precision
    # (these are pixel counts, exact well past any distance we care about).
    distance: npt.NDArray[np.float32] = ndimage.distance_transform_edt(mask).astype(np.float32)
    distance -= ndimage.distance_transform_edt(~mask).astype(np.float32)
    return distance


@dataclass(frozen=True)
class ShadeFields:
    """One instant's ingredients at the artifacts' own resolution.

    The point of handing these out instead of a finished state raster is
    *where the threshold happens*. Comparing at 1 m and upsampling the verdict
    gives a staircase of whole metres; carrying the continuous fields to the
    tile grid and comparing there puts the boundary where it belongs, because
    the zero crossing of a bilinear field inside a cell is a curve. Same
    arithmetic, later decision.

    ``margin`` is horizon angle minus sun elevation: positive means shade. It
    is the field whose sign the whole overlay is made of.
    """

    margin: npt.NDArray[np.float32]
    margin_noveg: npt.NDArray[np.float32]
    blocker: npt.NDArray[np.uint8]
    roof_distance: npt.NDArray[np.float32]
    canopy_distance: npt.NDArray[np.float32]
    outside: npt.NDArray[np.bool_] | None


def compose_state(
    *,
    shaded: npt.NDArray[np.bool_],
    holds: npt.NDArray[np.bool_],
    blocker: npt.NDArray[np.uint8],
    under_canopy: npt.NDArray[np.bool_],
    roof: npt.NDArray[np.bool_] | None = None,
    outside: npt.NDArray[np.bool_] | None = None,
) -> npt.NDArray[np.uint8]:
    """Shade state from already-thresholded masks, at whatever resolution.

    The single place the state logic lives, so the 1 m path and the tile path
    cannot drift: :func:`compute_state_raster` calls it with the masks it
    computed at 1 m, and ``tiles.py`` calls it per 256 px window with the same
    masks thresholded on the tile grid.

    ``roof`` and ``outside`` are presentation cuts and default to absent:
    nobody stands on a roof and the overlay is street-level shade, but the
    point engine has no such notion.
    """
    state = np.zeros(shaded.shape, dtype=np.uint8)  # STATE_SUN
    state[shaded & holds & (blocker == Landcover.BUILDING)] = STATE_SHADE_BUILDING
    state[shaded & holds & (blocker == Landcover.VEGETATION)] = STATE_SHADE_BOTH
    state[shaded & holds & ((blocker == Landcover.GROUND) | (blocker == NO_BLOCKER))] = (
        STATE_SHADE_OTHER
    )
    state[shaded & ~holds] = STATE_SHADE_VEGETATION
    # Canopy override last: is_shaded checks the canopy *before* the horizon,
    # and an unconditional overwrite here yields the same result.
    state[under_canopy & ~holds] = STATE_SHADE_VEGETATION
    state[under_canopy & holds] = STATE_SHADE_BOTH
    if roof is not None:
        state[roof] = STATE_OUTSIDE
    if outside is not None:
        state[outside] = STATE_OUTSIDE
    return state


def read_shade_fields(artifact_dir: str | Path, sun: SunPosition) -> ShadeFields:
    """Every continuous field one instant needs, at the artifacts' resolution.

    Reads the same two flanking horizon bands as :func:`compute_state_raster`
    and interpolates them in azimuth identically; what it does *not* do is
    compare them against the sun. That comparison is the caller's, and doing
    it later is what buys the sub-pixel boundary.
    """
    directory = Path(artifact_dir)
    if not sun.is_up:
        raise ValueError(
            f"sun elevation {sun.elevation_deg:.2f} deg is below the horizon; "
            "night has no shade raster"
        )
    with rasterio.open(directory / HORIZON_FILENAME) as src:
        sectors = src.count
        angle_max_deg = float(src.tags()["angle_max_deg"])
        position = (sun.azimuth_deg % 360.0) / (360.0 / sectors)
        lower = int(position) % sectors
        upper = (lower + 1) % sectors
        fraction = position - int(position)
        lower_q = src.read([lower + 1])[0]
        upper_q = src.read([upper + 1])[0]

    scale = angle_max_deg / 255.0
    elevation = np.float32(sun.elevation_deg)
    horizon = (1.0 - fraction) * (lower_q.astype(np.float32) * np.float32(scale)) + fraction * (
        upper_q.astype(np.float32) * np.float32(scale)
    )
    margin = (horizon - elevation).astype(np.float32)
    del horizon

    with rasterio.open(directory / BLOCKER_CLASS_FILENAME) as src:
        blocker_lower = src.read([lower + 1])[0]
        blocker_upper = src.read([upper + 1])[0]
    blocker = np.where(lower_q >= upper_q, blocker_lower, blocker_upper)
    del lower_q, upper_q, blocker_lower, blocker_upper

    noveg_path = directory / HORIZON_NOVEG_FILENAME
    if noveg_path.exists():
        with rasterio.open(noveg_path) as src:
            noveg_lower_q = src.read([lower + 1])[0]
            noveg_upper_q = src.read([upper + 1])[0]
        horizon_noveg = (1.0 - fraction) * (
            noveg_lower_q.astype(np.float32) * np.float32(scale)
        ) + fraction * (noveg_upper_q.astype(np.float32) * np.float32(scale))
        margin_noveg = (horizon_noveg - elevation).astype(np.float32)
        del noveg_lower_q, noveg_upper_q, horizon_noveg
    else:
        # Artifacts predating ADR-017 encoded exactly this: without the second
        # cube, "would it hold with the trees felled" can only be answered by
        # the blocker class. A tiny positive margin reproduces the boolean.
        holds = (margin > 0.0) & (blocker != Landcover.VEGETATION)
        margin_noveg = np.where(holds, np.float32(1.0), np.float32(-1.0)).astype(np.float32)

    canopy_path = directory / CANOPY_FILENAME
    if not canopy_path.exists():
        raise FileNotFoundError(
            f"{canopy_path} missing; artifacts predate the canopy mask -- "
            "run `shade-engine canopy <city>` to derive it"
        )
    with rasterio.open(canopy_path) as src:
        canopy = src.read()[0] != 0
    with rasterio.open(directory / LANDCOVER_FILENAME) as src:
        roof = src.read()[0] == Landcover.BUILDING

    covered = load_coverage(directory)
    return ShadeFields(
        margin=margin,
        margin_noveg=margin_noveg,
        blocker=blocker,
        roof_distance=signed_distance(roof),
        canopy_distance=signed_distance(canopy),
        outside=None if covered is None else ~covered,
    )


def compute_state_raster(artifact_dir: str | Path, sun: SunPosition) -> npt.NDArray[np.uint8]:
    """Shade state code per pixel of a city's artifacts under a given sun.

    Mirrors :func:`shade_core.shade.is_shaded` decision by decision: canopy
    overrides everything (a pixel under the canopy mask is shaded by
    vegetation whenever the sun is up), then the horizon comparison, then the
    blocker classification at the contributing sector, then the promotion to
    ``STATE_SHADE_BOTH`` where the vegetation-free horizon closes the sky too.
    Night has no raster: callers must not ask (raises ``ValueError``), since
    every pixel would be NIGHT.

    Artifacts without ``horizon_noveg.tif`` fall back to the pre-decomposition
    states, which is exactly what they encoded.
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

    # Would the sun still be blocked with the trees felled? A building blocker
    # answers itself (the vegetation-free skyline reaches at least its angle),
    # so `holds` only ever differs from `shaded` on vegetation blockers -- and
    # without the cube the old artifacts' implicit answer was "no".
    noveg_path = directory / HORIZON_NOVEG_FILENAME
    if noveg_path.exists():
        with rasterio.open(noveg_path) as src:
            noveg_lower_q = src.read([lower + 1])[0]
            noveg_upper_q = src.read([upper + 1])[0]
        horizon_noveg = (1.0 - fraction) * (
            noveg_lower_q.astype(np.float32) * np.float32(scale)
        ) + fraction * (noveg_upper_q.astype(np.float32) * np.float32(scale))
        holds = np.float64(sun.elevation_deg) < horizon_noveg
    else:
        holds = shaded & (blocker != Landcover.VEGETATION)

    # read()[0] instead of read(1): rasterio's single-band path reshapes in
    # place, which numpy 2.5 deprecates.
    canopy_path = directory / CANOPY_FILENAME
    if not canopy_path.exists():
        raise FileNotFoundError(
            f"{canopy_path} missing; artifacts predate the canopy mask -- "
            "run `shade-engine canopy <city>` to derive it"
        )
    with rasterio.open(canopy_path) as src:
        canopy = src.read()[0]
    return compose_state(shaded=shaded, holds=holds, blocker=blocker, under_canopy=canopy != 0)
