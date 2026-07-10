"""Synthetic scenes with hand-checkable geometry for golden tests.

World frame: resolution 1 m, origin (0, SIZE) -> x east in [0, SIZE),
y north in (0, SIZE]. Cell (row, col) covers x in [col, col+1),
y in [SIZE-row-1, SIZE-row).
"""

import numpy as np
import numpy.typing as npt

SIZE = 120
RESOLUTION_M = 1.0

# Cube building: 20 m tall, footprint x in [50, 70), y in [30, 50).
CUBE_HEIGHT = 20.0
CUBE_X = (50.0, 70.0)
CUBE_Y = (30.0, 50.0)
CUBE_NORTH_WALL_Y = CUBE_Y[1]
CUBE_CENTER_X = (CUBE_X[0] + CUBE_X[1]) / 2


def _fill(
    target: npt.NDArray[np.float64],
    x: tuple[float, float],
    y: tuple[float, float],
    value: float,
) -> None:
    rows, _ = target.shape
    row0 = int(rows - y[1])
    row1 = int(rows - y[0])
    target[row0:row1, int(x[0]) : int(x[1])] = value


def flat_terrain(size: int = SIZE) -> tuple[npt.NDArray[np.float64], npt.NDArray[np.float64]]:
    """(dsm, dtm) of perfectly flat ground at z=0."""
    return np.zeros((size, size)), np.zeros((size, size))


def cube_scene() -> tuple[npt.NDArray[np.float64], npt.NDArray[np.float64]]:
    """(dsm, dtm): the spec's golden fixture, a 20 m cube on flat ground."""
    dsm, dtm = flat_terrain()
    _fill(dsm, CUBE_X, CUBE_Y, CUBE_HEIGHT)
    return dsm, dtm
