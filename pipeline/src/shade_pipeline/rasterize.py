"""Rasterize LiDAR point clouds into the DSM / DTM / landcover stack.

A point cloud becomes a raster by *binning*: each point falls in exactly one
cell (floor of its offset from the grid origin) and each cell aggregates the
points it received. Per cell:

- **DSM**: highest z among *first returns* -- the surface a sun ray meets
  first (canopy tops, roofs).
- **DTM**: mean z of ground-classified points (ASPRS class 2), whatever their
  return number (under vegetation the ground echo is usually a later return).
- **landcover**: the class of the point that set the cell's DSM, so the
  horizon sweep can report *what* blocks the sun there.

Cells without ground points -- building footprints, water -- are DTM holes
filled by inverse-distance interpolation from surrounding ground pixels
(rasterio's ``fillnodata``). Cells without any first return copy the filled
DTM, and the DSM is floored at the DTM to guard against noise points below
the terrain. Noise *above* the terrain has no such floor, so noise and
overlap classes (7, 18, 12) and withheld-flagged points are dropped before
binning; see the chunk loop for the details.
"""

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

import laspy
import numpy as np
import numpy.typing as npt
import rasterio.fill
from affine import Affine

from shade_core.config import Bbox
from shade_core.shade import Landcover
from shade_pipeline.grid import grid_shape, transform_from_bbox
from shade_pipeline.progress import format_duration

# ASPRS point classes as delivered by PNOA flights.
LIDAR_CLASS_GROUND = 2
LIDAR_CLASSES_VEGETATION = (3, 4, 5)
LIDAR_CLASS_BUILDING = 6
LIDAR_CLASS_LOW_NOISE = 7
LIDAR_CLASS_OVERLAP = 12
LIDAR_CLASS_HIGH_NOISE = 18
DROPPED_CLASSES = (LIDAR_CLASS_LOW_NOISE, LIDAR_CLASS_OVERLAP, LIDAR_CLASS_HIGH_NOISE)


@dataclass(frozen=True)
class RasterStack:
    """The three per-city base rasters plus their shared georeference."""

    dsm: npt.NDArray[np.float32]
    dtm: npt.NDArray[np.float32]
    landcover: npt.NDArray[np.uint8]
    transform: Affine
    point_counts: dict[str, int]


def rasterize_lidar(
    files: Sequence[Path],
    bbox: Bbox,
    resolution_m: float,
    *,
    chunk_size: int = 2_000_000,
    progress: Callable[[str], None] | None = None,
) -> RasterStack:
    """Bin LAZ/LAS files into DSM, DTM and landcover over ``bbox``.

    ``bbox`` is the (already padded) target extent in the same projected CRS
    the files are delivered in; points outside it are dropped. Files are read
    in chunks so arbitrarily large point clouds fit in memory. Accumulation
    uses ``np.maximum.at`` / ``np.add.at``: unbuffered (correct with repeated
    indices) and simple; switch to a lexsort + reduceat scheme only if real
    PNOA-scale runs prove too slow.
    """
    rows, cols = grid_shape(bbox, resolution_m)
    min_x, _, _, max_y = bbox
    n = rows * cols

    dsm_max = np.full(n, -np.inf)
    building_max = np.full(n, -np.inf)
    vegetation_max = np.full(n, -np.inf)
    dtm_sum = np.zeros(n)
    dtm_count = np.zeros(n, dtype=np.int64)
    point_counts: dict[str, int] = {}

    binning_start = time.monotonic()
    for file_index, path in enumerate(files, start=1):
        if progress is not None:
            if file_index == 1:
                progress(f"binning [1/{len(files)}] {path.name}")
            else:
                average = (time.monotonic() - binning_start) / (file_index - 1)
                eta = average * (len(files) - file_index + 1)
                progress(
                    f"binning [{file_index}/{len(files)}] {path.name} "
                    f"(avg {format_duration(average)}/file, eta {format_duration(eta)})"
                )
        total = 0
        with laspy.open(path) as reader:
            for points in reader.chunk_iterator(chunk_size):
                x = np.asarray(points.x)
                y = np.asarray(points.y)
                z = np.asarray(points.z)
                classification = np.asarray(points.classification)
                first = np.asarray(points.return_number) == 1
                total += len(x)

                # Noise (7/18) and overlap (12, or its LAS 1.4 flag) never bin:
                # the DSM is a max, so one stray return 50 m above a street
                # would cast a phantom obstacle over every horizon profile
                # within max_distance. Withheld points are excluded by spec.
                # The synthetic flag stays: it marks valid points produced by
                # another technique (hydro-flattened water is class 2 +
                # synthetic; dropping it would hole the DTM across the river).
                keep = ~(
                    np.isin(classification, DROPPED_CLASSES)
                    | np.asarray(points.withheld, dtype=bool)
                    | np.asarray(points.overlap, dtype=bool)
                )

                col = np.floor((x - min_x) / resolution_m).astype(np.int64)
                row = np.floor((max_y - y) / resolution_m).astype(np.int64)
                inside = keep & (row >= 0) & (row < rows) & (col >= 0) & (col < cols)
                idx = (row * cols + col)[inside]
                z = z[inside]
                classification = classification[inside]
                first = first[inside]

                np.maximum.at(dsm_max, idx[first], z[first])
                building = first & (classification == LIDAR_CLASS_BUILDING)
                np.maximum.at(building_max, idx[building], z[building])
                vegetation = first & np.isin(classification, LIDAR_CLASSES_VEGETATION)
                np.maximum.at(vegetation_max, idx[vegetation], z[vegetation])
                ground = classification == LIDAR_CLASS_GROUND
                np.add.at(dtm_sum, idx[ground], z[ground])
                np.add.at(dtm_count, idx[ground], 1)
        point_counts[path.name] = total

    dtm = np.full(n, np.nan)
    has_ground = dtm_count > 0
    dtm[has_ground] = dtm_sum[has_ground] / dtm_count[has_ground]
    dtm_filled = fill_dtm_gaps(dtm.reshape(rows, cols).astype(np.float32))

    # Landcover = class of the point that set the cell's DSM; building wins
    # exact ties. Cells with no first return at all stay GROUND.
    has_surface = np.isfinite(dsm_max)
    landcover = np.full(n, Landcover.GROUND, dtype=np.uint8)
    landcover[has_surface & (vegetation_max >= dsm_max)] = Landcover.VEGETATION
    landcover[has_surface & (building_max >= dsm_max)] = Landcover.BUILDING

    dsm = dsm_max.reshape(rows, cols).astype(np.float32)
    surface = has_surface.reshape(rows, cols)
    dsm[~surface] = dtm_filled[~surface]
    dsm = np.maximum(dsm, dtm_filled)

    return RasterStack(
        dsm=dsm,
        dtm=dtm_filled,
        landcover=landcover.reshape(rows, cols),
        transform=transform_from_bbox(bbox, resolution_m),
        point_counts=point_counts,
    )


def fill_dtm_gaps(
    dtm: npt.NDArray[np.float32], *, max_search_distance_px: float = 100.0
) -> npt.NDArray[np.float32]:
    """Fill NaN holes by inverse-distance interpolation from valid pixels.

    Wraps ``rasterio.fill.fillnodata`` (GDALFillNodata): pixels where the mask
    is False are interpolated from surrounding valid ones, searching up to
    ``max_search_distance_px`` *pixels* away. Holes wider than that would
    survive as NaN and poison every observer height downstream, so any
    remainder raises instead of shipping a broken DTM.
    """
    valid = ~np.isnan(dtm)
    if valid.all():
        return dtm
    filled: npt.NDArray[np.float32] = rasterio.fill.fillnodata(
        dtm.copy(),
        mask=valid,
        max_search_distance=max_search_distance_px,
        smoothing_iterations=0,
    )
    remaining = int(np.isnan(filled).sum())
    if remaining:
        raise ValueError(
            f"DTM has {remaining} cells with no ground point within "
            f"{max_search_distance_px} px; widen max_search_distance_px or check the input"
        )
    return filled
