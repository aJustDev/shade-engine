"""Synthetic scenes with hand-checkable geometry for golden tests.

World frame: resolution 1 m, origin (0, SIZE) -> x east in [0, SIZE),
y north in (0, SIZE]. Cell (row, col) covers x in [col, col+1),
y in [SIZE-row-1, SIZE-row).
"""

import numpy as np
import numpy.typing as npt

from shade_core.shade import Landcover

SIZE = 120
RESOLUTION_M = 1.0

# Where the built-city fixture places the scene's (0, 0) corner in EPSG:25830
# (UTM 30N, meters). Chosen near real Cordoba (~37.87 N, 4.80 W) so the API's
# lat/lon queries and the phase-1 golden sun positions both apply. Coordinates
# around 4e6 also expose georef sign/precision bugs that a (0, 0) origin masks.
UTM_ORIGIN = (341000.0, 4192000.0)

# Cube building: 20 m tall, footprint x in [50, 70), y in [30, 50).
CUBE_HEIGHT = 20.0
CUBE_X = (50.0, 70.0)
CUBE_Y = (30.0, 50.0)
CUBE_NORTH_WALL_Y = CUBE_Y[1]
CUBE_CENTER_X = (CUBE_X[0] + CUBE_X[1]) / 2

# Query x for shade/timeline tests: offset from the cube's symmetry axis so no
# sun ray ever grazes a cube corner exactly (measure-zero contact where
# reference sampling and ray-marching legitimately disagree). Real-world query
# points are floats from GPS; only synthetic round numbers hit corners.
QUERY_X = CUBE_CENTER_X + 3.0


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


def cube_landcover() -> npt.NDArray[np.uint8]:
    """Landcover for the cube scene: BUILDING on the footprint, GROUND elsewhere."""
    landcover = np.zeros((SIZE, SIZE), dtype=np.float64)
    _fill(landcover, CUBE_X, CUBE_Y, float(Landcover.BUILDING))
    return landcover.astype(np.uint8)


# Tree: 8 m tall canopy, footprint x in [26, 34), y in [26, 34), on a smaller
# grid so its horizon stays cheap to compute.
TREE_SIZE = 60
CANOPY_HEIGHT = 8.0
CANOPY_X = (26.0, 34.0)
CANOPY_Y = (26.0, 34.0)
CANOPY_NORTH_Y = CANOPY_Y[1]
CANOPY_CENTER = (30.0, 30.0)


def tree_scene() -> tuple[
    npt.NDArray[np.float64],
    npt.NDArray[np.float64],
    npt.NDArray[np.uint8],
    npt.NDArray[np.bool_],
]:
    """(dsm, dtm, landcover, canopy): one tree canopy floating over flat ground."""
    dsm, dtm = flat_terrain(TREE_SIZE)
    _fill(dsm, CANOPY_X, CANOPY_Y, CANOPY_HEIGHT)
    landcover = np.zeros((TREE_SIZE, TREE_SIZE), dtype=np.float64)
    _fill(landcover, CANOPY_X, CANOPY_Y, float(Landcover.VEGETATION))
    return dsm, dtm, landcover.astype(np.uint8), landcover.astype(np.bool_)
