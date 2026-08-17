"""Horizon grid: each pixel's skyline, quantized into azimuth sectors.

The horizon map is the engine's central trick. Precomputing shadow rasters
per date and hour explodes combinatorially; instead, each pixel stores its
*angular fingerprint*: for N azimuth sectors (band k points toward azimuth
``k * 360 / N``, 0 = North, clockwise -- same convention as
:class:`shade_core.solar.SunPosition`), the elevation angle of the highest
obstacle visible in that direction. Any shade query then reduces to
``sun.elevation < horizon(sun.azimuth)``: one precomputation per city,
millisecond queries, valid for any instant.

Angles are measured from an observer standing at street level -- terrain
elevation (DTM) plus ~1.6 m of person -- against obstacles taken from the
surface model (DSM). Computing from the DSM itself would place the observer
on top of tree canopies and roofs, reporting sun where the street below is
shaded.

Sampling choices (deliberate, see shade-docs: learning/horizon-algorithm.md):

- **Across azimuth**: circular linear interpolation between the two adjacent
  sectors (wrapping 360 -> 0). Nearest-sector would err by up to half a
  sector (~2.8 degrees at 64 sectors), which near a building translates into
  meters of shade-boundary error.
- **Across space**: nearest pixel, never bilinear. Horizon profiles are
  discontinuous at building walls; averaging a roof profile with a street
  profile yields angles that describe neither place.
"""

import math
from dataclasses import dataclass

import numpy as np
import numpy.typing as npt

from shade_core.raycast import ray_cells


@dataclass(frozen=True)
class HorizonGrid:
    """Multiband horizon raster held in memory.

    ``angles_deg[k, row, col]`` is the horizon elevation angle (degrees, >= 0)
    of pixel (row, col) toward azimuth ``k * 360 / sectors``. ``origin`` is
    the (x_min, y_max) corner -- the top-left of the array -- in projected CRS
    meters; rows grow southward, columns eastward.
    """

    angles_deg: npt.NDArray[np.float32]  # shape (sectors, rows, cols)
    resolution_m: float
    origin: tuple[float, float]

    def __post_init__(self) -> None:
        if self.angles_deg.ndim != 3:
            raise ValueError("angles_deg must have shape (sectors, rows, cols)")

    @property
    def sectors(self) -> int:
        return int(self.angles_deg.shape[0])

    def rowcol(self, x: float, y: float) -> tuple[int, int]:
        """Nearest pixel for a point in projected CRS meters.

        ``floor``, not ``int``: truncation rounds toward zero, so a point up
        to one pixel west or north of the grid would land on pixel 0 and be
        answered instead of rejected. ``SceneReader`` floors too, and the two
        "nearest pixel" routes have to agree.
        """
        x_min, y_max = self.origin
        col = math.floor((x - x_min) / self.resolution_m)
        row = math.floor((y_max - y) / self.resolution_m)
        _, rows, cols = self.angles_deg.shape
        if not (0 <= row < rows and 0 <= col < cols):
            raise ValueError(f"point ({x}, {y}) is outside the horizon grid")
        return row, col

    def profile_at(self, x: float, y: float) -> npt.NDArray[np.float32]:
        """Full horizon profile (one angle per sector) at the nearest pixel."""
        row, col = self.rowcol(x, y)
        return self.angles_deg[:, row, col]

    def horizon_at(self, x: float, y: float, azimuth_deg: float) -> float:
        """Horizon angle toward an azimuth, linearly interpolated in azimuth."""
        profile = self.profile_at(x, y)
        position = (azimuth_deg % 360.0) / (360.0 / self.sectors)
        lower = int(position) % self.sectors
        upper = (lower + 1) % self.sectors
        fraction = position - int(position)
        return float((1.0 - fraction) * profile[lower] + fraction * profile[upper])


def compute_horizon_reference(
    dsm: npt.NDArray[np.floating],
    dtm: npt.NDArray[np.floating],
    resolution_m: float,
    *,
    sectors: int = 64,
    max_distance_m: float = 100.0,
    observer_height_m: float = 1.6,
    origin: tuple[float, float] | None = None,
) -> HorizonGrid:
    """Brute-force reference horizon: obviously correct, deliberately slow.

    For every pixel and sector, **walk the cells the line of sight crosses**
    (:func:`shade_core.raycast.ray_cells`) up to ``max_distance_m`` and keep
    ``max(atan2(obstacle_z - observer_z, entry_distance))``. A DSM cell is a
    1x1 m column, so it starts blocking at the distance the ray enters it; see
    [[ADR-027]] and shade-docs: learning/recorrido-de-rayo.md. The observer
    stands at DTM + ``observer_height_m``; angles are floored at 0 (the
    astronomical horizon). ``max_distance_m`` bounds both cost and the lowest
    resolvable horizon angle: obstacles further away can only cast very
    low-sun shadows.

    Intended for small synthetic arrays in tests. The pipeline's vectorized,
    tiled implementation must reproduce these values on the same fixtures --
    this function is its oracle, and it can only be one while both walk the
    ray the same way.
    """
    if dsm.shape != dtm.shape:
        raise ValueError("dsm and dtm must have the same shape")
    rows, cols = dsm.shape
    if origin is None:
        origin = (0.0, rows * resolution_m)

    observer_z = dtm.astype(np.float64) + observer_height_m
    surface_z = dsm.astype(np.float64)
    row_index = np.arange(rows)[:, None]
    col_index = np.arange(cols)[None, :]

    angles = np.zeros((sectors, rows, cols), dtype=np.float32)
    for k in range(sectors):
        azimuth_deg = k * 360.0 / sectors
        best = np.full((rows, cols), -np.inf)
        for d_row, d_col, distance, _exit in ray_cells(azimuth_deg, max_distance_m, resolution_m):
            source_row = row_index + d_row
            source_col = col_index + d_col
            inside = (
                (source_row >= 0) & (source_row < rows) & (source_col >= 0) & (source_col < cols)
            )
            obstacle_z = surface_z[
                np.clip(source_row, 0, rows - 1), np.clip(source_col, 0, cols - 1)
            ]
            angle = np.degrees(np.arctan2(obstacle_z - observer_z, distance))
            best = np.maximum(best, np.where(inside, angle, -np.inf))
        angles[k] = np.maximum(best, 0.0)

    return HorizonGrid(angles_deg=angles, resolution_m=resolution_m, origin=origin)
