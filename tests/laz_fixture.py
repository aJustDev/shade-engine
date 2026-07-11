"""Synthetic LAZ writers for pipeline tests; no binary fixtures in git.

Kept separate from tests/synthetic.py so core-only tests never import laspy.

LAS format notes encoded here on purpose: point formats 0-5 pack the
classification into 5 bits shared with flag bits, so class codes above 31
cannot exist there; format 6 (LAS 1.4) gives classification a full byte and
is what PNOA's second coverage actually ships. ``return_number`` is a 1-based
4-bit subfield -- never write 0. No CRS goes into the header: the pipeline
trusts the CRS declared in the city YAML (PNOA files already come in the
local UTM zone).
"""

from pathlib import Path

import laspy
import numpy as np
import numpy.typing as npt

import synthetic
from shade_pipeline.rasterize import LIDAR_CLASS_BUILDING, LIDAR_CLASS_GROUND


def write_laz(
    path: Path,
    x: npt.NDArray[np.float64],
    y: npt.NDArray[np.float64],
    z: npt.NDArray[np.float64],
    classification: npt.NDArray[np.uint8],
    return_number: npt.NDArray[np.uint8] | None = None,
) -> int:
    """Write parallel point arrays as a LAZ file; returns the point count."""
    header = laspy.LasHeader(version="1.4", point_format=6)
    header.scales = np.array([0.01, 0.01, 0.01])
    header.offsets = np.array([0.0, 0.0, 0.0])
    las = laspy.LasData(header)
    las.x = x
    las.y = y
    las.z = z
    las.classification = classification
    if return_number is None:
        return_number = np.ones(len(x), dtype=np.uint8)
    las.return_number = return_number
    las.number_of_returns = return_number
    las.write(path)
    return len(x)


def write_cube_laz(path: Path) -> int:
    """The cube scene as a point cloud: one first return per cell center.

    Roof points (class 6, z = 20) on the cube footprint and ground points
    (class 2, z = 0) everywhere else. There are no ground points *under* the
    cube -- exactly like a real flight -- so the DTM gets a 20x20 hole that
    exercises gap filling for real.
    """
    size = synthetic.SIZE
    row, col = np.mgrid[0:size, 0:size]
    x = (col + 0.5).astype(np.float64).ravel()
    y = (size - row - 0.5).astype(np.float64).ravel()
    in_cube = (
        (x >= synthetic.CUBE_X[0])
        & (x < synthetic.CUBE_X[1])
        & (y >= synthetic.CUBE_Y[0])
        & (y < synthetic.CUBE_Y[1])
    )
    z = np.where(in_cube, synthetic.CUBE_HEIGHT, 0.0)
    classification = np.where(in_cube, LIDAR_CLASS_BUILDING, LIDAR_CLASS_GROUND)
    return write_laz(path, x, y, z, classification.astype(np.uint8))
