"""Production horizon sweep: tiled, vectorized, and true to the oracle.

This reimplements ``shade_core.horizon.compute_horizon_reference`` for real
city rasters. In ``exact`` step mode it reproduces the oracle's sampling
bit for bit -- same half-pixel distances, same ``round()`` offsets, same
float64 math -- restructured in two ways that do not change results:

- **Offset dedup**: consecutive half-pixel distances often snap to the same
  (d_row, d_col) cell; only the smallest distance per cell is kept. Safe
  proof: for a fixed target pixel and offset, ``dz = obstacle_z - observer_z``
  is constant and ``atan2(dz, d)`` is non-increasing in ``d`` when
  ``dz >= 0``, so the smallest distance attains that offset's maximum; when
  ``dz < 0`` every sample is negative and the final floor at 0 absorbs it.
- **Tiling with buffer**: the target area is swept in tiles, each reading a
  window padded by ``buffer_pixels`` -- at least the largest possible offset
  -- so a sample is inside the padded window iff it is inside the dataset.
  Per-pixel sample sets are therefore identical regardless of tile bounds.

The sweep also records *what* blocks each sector: whenever a sample raises a
pixel's max angle, its landcover class is kept. Strict ``>`` with ascending
distances means the nearest blocker wins ties, matching core's ray-march
intuition. Sectors whose final angle is 0 (open sky) get ``NO_BLOCKER``.

``geometric`` step mode grows the distance step multiplicatively in the far
field (constant relative error, far fewer samples) as a future knob for
city-scale runs; it can skip thin distant obstacles and is never validated
against the oracle at tight tolerance.
"""

import math
from dataclasses import dataclass
from typing import Final, Literal

import numpy as np
import numpy.typing as npt

from shade_pipeline.grid import buffer_pixels

NO_BLOCKER: Final = 255
ANGLE_MAX_DEG: Final = 90.0


@dataclass(frozen=True)
class HorizonParams:
    """Knobs of the horizon sweep; defaults match the spec."""

    sectors: int = 64
    max_distance_m: float = 500.0
    observer_height_m: float = 1.6
    tile_size: int = 512
    step_mode: Literal["exact", "geometric"] = "exact"
    geometric_growth: float = 1.02


@dataclass(frozen=True)
class HorizonResult:
    """Quantized horizon angles and blocker classes, shape (sectors, rows, cols)."""

    angles_q: npt.NDArray[np.uint8]
    blocker_class: npt.NDArray[np.uint8]


def sector_offsets(
    sector: int, params: HorizonParams, resolution_m: float
) -> list[tuple[int, int, float]]:
    """Deduped (d_row, d_col, distance) samples for one sector, ascending distance.

    Uses the exact expressions of the oracle (``np.arange`` half-pixel
    distances, builtin ``round`` -- half-to-even, never ``int(x + 0.5)``) so
    exact mode stays bit-identical to it.
    """
    azimuth = math.radians(sector * 360.0 / params.sectors)
    east, north = math.sin(azimuth), math.cos(azimuth)
    step = resolution_m / 2.0
    if params.step_mode == "exact":
        distances = np.arange(step, params.max_distance_m + step / 2.0, step)
    else:
        grown: list[float] = []
        distance = step
        while distance <= params.max_distance_m:
            grown.append(distance)
            distance = max(distance + step, distance * params.geometric_growth)
        distances = np.array(grown)

    kept: dict[tuple[int, int], float] = {}
    for distance in distances:
        d_col = round(distance * east / resolution_m)
        d_row = -round(distance * north / resolution_m)  # y up = row index down
        if (d_row == 0 and d_col == 0) or (d_row, d_col) in kept:
            continue
        kept[(d_row, d_col)] = float(distance)
    return [(d_row, d_col, d) for (d_row, d_col), d in kept.items()]


def compute_horizon_block(
    dsm: npt.NDArray[np.floating],
    dtm: npt.NDArray[np.floating],
    landcover: npt.NDArray[np.uint8],
    resolution_m: float,
    params: HorizonParams,
    inner: tuple[int, int, int, int],
) -> tuple[npt.NDArray[np.float32], npt.NDArray[np.uint8]]:
    """Sweep the ``inner`` (row0, row1, col0, col1) window against the full arrays.

    Returns pre-quantization float32 angles and uint8 blocker classes for the
    window. Samples come from anywhere in the given arrays; the caller is
    responsible for passing enough surrounding context (see the tiled driver).
    """
    row0, row1, col0, col1 = inner
    rows, cols = dsm.shape
    height, width = row1 - row0, col1 - col0
    observer_z = dtm[row0:row1, col0:col1].astype(np.float64) + params.observer_height_m
    surface_z = dsm.astype(np.float64)

    angles = np.empty((params.sectors, height, width), dtype=np.float32)
    blocker = np.empty((params.sectors, height, width), dtype=np.uint8)
    for k in range(params.sectors):
        best = np.full((height, width), -np.inf)
        best_class = np.full((height, width), NO_BLOCKER, dtype=np.uint8)
        for d_row, d_col, distance in sector_offsets(k, params, resolution_m):
            # Target pixel (i, j) samples array cell (row0 + i + d_row, ...);
            # clamp to the sub-rectangle of targets whose sample is in range.
            i_lo = max(0, -(row0 + d_row))
            i_hi = min(height, rows - row0 - d_row)
            j_lo = max(0, -(col0 + d_col))
            j_hi = min(width, cols - col0 - d_col)
            if i_lo >= i_hi or j_lo >= j_hi:
                continue
            src = (
                slice(row0 + i_lo + d_row, row0 + i_hi + d_row),
                slice(col0 + j_lo + d_col, col0 + j_hi + d_col),
            )
            sub = (slice(i_lo, i_hi), slice(j_lo, j_hi))
            angle = np.degrees(np.arctan2(surface_z[src] - observer_z[sub], distance))
            improved = angle > best[sub]
            best[sub] = np.where(improved, angle, best[sub])
            best_class[sub] = np.where(improved, landcover[src], best_class[sub])
        angles[k] = np.maximum(best, 0.0).astype(np.float32)
        best_class[best <= 0.0] = NO_BLOCKER
        blocker[k] = best_class
    return angles, blocker


def compute_horizon_tiled(
    dsm: npt.NDArray[np.floating],
    dtm: npt.NDArray[np.floating],
    landcover: npt.NDArray[np.uint8],
    resolution_m: float,
    params: HorizonParams,
    inner: tuple[int, int, int, int] | None = None,
) -> HorizonResult:
    """Sweep the ``inner`` window (default: everything) tile by tile.

    Each tile reads a window padded by ``buffer_pixels`` on every side
    (clipped at dataset edges), so results are independent of ``tile_size``;
    memory per tile stays bounded while cost stays proportional to inner
    pixels (buffer pixels are read, never swept).
    """
    rows, cols = dsm.shape
    if inner is None:
        inner = (0, rows, 0, cols)
    row0, row1, col0, col1 = inner
    pad = buffer_pixels(params.max_distance_m, resolution_m)

    angles_q = np.empty((params.sectors, row1 - row0, col1 - col0), dtype=np.uint8)
    blocker = np.empty_like(angles_q)
    for t0 in range(row0, row1, params.tile_size):
        t1 = min(t0 + params.tile_size, row1)
        for u0 in range(col0, col1, params.tile_size):
            u1 = min(u0 + params.tile_size, col1)
            p0, p1 = max(0, t0 - pad), min(rows, t1 + pad)
            q0, q1 = max(0, u0 - pad), min(cols, u1 + pad)
            tile_angles, tile_blocker = compute_horizon_block(
                dsm[p0:p1, q0:q1],
                dtm[p0:p1, q0:q1],
                landcover[p0:p1, q0:q1],
                resolution_m,
                params,
                (t0 - p0, t1 - p0, u0 - q0, u1 - q0),
            )
            out = (slice(None), slice(t0 - row0, t1 - row0), slice(u0 - col0, u1 - col0))
            angles_q[out] = quantize_angles(tile_angles)
            blocker[out] = tile_blocker
    return HorizonResult(angles_q=angles_q, blocker_class=blocker)


def quantize_angles(angles_deg: npt.NDArray[np.float32]) -> npt.NDArray[np.uint8]:
    """Quantize [0, 90] degrees to uint8: step 90/255 ~= 0.353 deg.

    Round-to-nearest keeps the error unbiased and below ~0.18 deg -- far under
    the sweep's own half-pixel discretization. Dequantize on read with
    ``q * (ANGLE_MAX_DEG / 255)``; readers take the scale from the artifact's
    ``angle_max_deg`` tag so files stay self-describing.
    """
    scaled = np.round(angles_deg * (255.0 / ANGLE_MAX_DEG))
    return np.clip(scaled, 0.0, 255.0).astype(np.uint8)
