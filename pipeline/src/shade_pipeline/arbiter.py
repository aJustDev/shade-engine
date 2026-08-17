"""Judging the engine from outside its own conventions.

Everything else that checks the sweep shares its sampling: the oracle in
``shade_core.horizon``, the ray-march in ``shade_core.shade`` and the sweep
itself all walk the ray the same way, so none of them can tell whether that way
is right. That is how the distance convention survived from phase 2 to
[[ADR-027]] with nobody noticing (shade-docs: learning/muestreo-del-horizonte.md).

This module answers the question the cubes answer -- is the sun blocked at this
pixel? -- without a horizon, without sectors, without quantization and without a
per-cell distance rule. Just the ray, the cells it crosses, and the heights
there. It is slower per instant than reading a cube and that is fine: it is the
referee, not the product.

**It reports a bracket, not a number.** A DSM cell is a column of unknown
occupancy, so two readings bound the truth:

- ``most``: the column blocks from the distance the ray *enters* the cell.
  Solid 1x1 m block -- the most shade the data can justify, and what the sweep
  computes since ADR-027.
- ``least``: it blocks only if the ray is still below the top when it *leaves*.
  The obstacle would have to sit at the far edge -- the least shade defensible.

A crown that transmits, a pierced parapet or a wall crossing a cell diagonally
all live between the two. Measured on montilla-test over the 83 ladder
instants, the bracket is 2.85 points of shade wide (50.62% to 53.47%), and that
width is the price of the solid-column assumption -- the number field validation
gets to falsify, instead of a single figure that hides it.
"""

import math

import numpy as np
import numpy.typing as npt

from shade_core.raycast import ray_cells
from shade_core.solar import SunPosition

__all__ = ["shade_bracket"]

Window = tuple[int, int, int, int]


def shade_bracket(
    dsm: npt.NDArray[np.floating],
    dtm: npt.NDArray[np.floating],
    inner: Window,
    sun: SunPosition,
    *,
    resolution_m: float = 1.0,
    max_distance_m: float = 500.0,
    observer_height_m: float = 1.6,
) -> tuple[npt.NDArray[np.bool_], npt.NDArray[np.bool_]]:
    """(most, least) shade over ``inner``, by exact traversal at the sun's azimuth.

    ``dsm`` and ``dtm`` must be padded around ``inner`` by at least
    ``max_distance_m`` worth of pixels, exactly like the sweep's tiles: cells
    whose offset falls outside the arrays are skipped, so a short pad silently
    under-reports shade.

    Returns two boolean rasters of ``inner``'s shape. They differ only where a
    cell's occupancy decides the verdict, which is precisely where the
    solid-column reading is doing the work.

    Note what this does *not* model: the opaque-canopy rule of [[ADR-002]],
    which puts every pixel under a crown in shade whenever the sun is up. No ray
    trace reproduces that -- it is a product decision, not geometry -- so on a
    city with trees the engine will always claim more shade than this function
    (measured on montilla-test: 88% of the excess sits under canopy, +0.07
    points elsewhere). Compare over open sky, or compare knowing this.
    """
    if not sun.is_up:
        raise ValueError(
            f"sun elevation {sun.elevation_deg:.2f} deg is below the horizon; "
            "night has no shade bracket"
        )
    row0, row1, col0, col1 = inner
    rows, cols = row1 - row0, col1 - col0
    height, width = dsm.shape
    observer = dtm[row0:row1, col0:col1].astype(np.float32) + np.float32(observer_height_m)
    tangent = math.tan(math.radians(sun.elevation_deg))

    most = np.zeros((rows, cols), dtype=bool)
    least = np.zeros((rows, cols), dtype=bool)
    for d_row, d_col, entry, exit_m in ray_cells(sun.azimuth_deg, max_distance_m, resolution_m):
        top, left = row0 + d_row, col0 + d_col
        if top < 0 or left < 0 or top + rows > height or left + cols > width:
            continue  # the ray left the padded stack
        surface = dsm[top : top + rows, left : left + cols]
        np.logical_or(most, surface > observer + np.float32(tangent * entry), out=most)
        np.logical_or(least, surface > observer + np.float32(tangent * exit_m), out=least)
    return most, least
