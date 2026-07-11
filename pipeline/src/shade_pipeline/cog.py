"""Write rasters as Cloud Optimized GeoTIFFs (COG).

A COG is a plain GeoTIFF whose layout honors a contract: pixels stored in
independently-compressed internal tiles, reduced-resolution overviews for
visualization, and tile indexes at the front of the file. Reading one pixel
of one band then costs one tile's worth of IO -- the same over local disk or
HTTP range requests -- which is how the API will query city-sized artifacts
without ever loading them (see docs/learning/cog.md).

GDAL's COG driver is CreateCopy-only (it must know every tile before writing
the header), so the canonical path is: write a temporary tiled GTiff, then
copy it through the COG driver.
"""

from collections.abc import Mapping
from pathlib import Path

import numpy as np
import numpy.typing as npt
import rasterio
import rasterio.shutil
from affine import Affine


def write_cog(
    path: Path,
    data: npt.NDArray[np.float32] | npt.NDArray[np.uint8],
    transform: Affine,
    crs: str,
    *,
    tags: Mapping[str, str] | None = None,
) -> None:
    """Write a 2D (rows, cols) or 3D (bands, rows, cols) array as a COG.

    Band k+1 carries ``data[k]`` (rasterio bands are 1-based); for the
    horizon artifacts that means band 1 = sector 0 = North. Overviews are
    resampled with ``nearest``: every band here is categorical or quantized,
    and averaging would invent values that exist nowhere.
    """
    cube = data[np.newaxis] if data.ndim == 2 else data
    bands, rows, cols = cube.shape
    tmp = path.with_name(path.name + ".tmp.tif")
    try:
        with rasterio.open(
            tmp,
            "w",
            driver="GTiff",
            width=cols,
            height=rows,
            count=bands,
            dtype=cube.dtype.name,
            crs=crs,
            transform=transform,
            tiled=True,
            blockxsize=512,
            blockysize=512,
            compress="deflate",
        ) as dst:
            dst.write(cube)
            if tags:
                dst.update_tags(**tags)
        rasterio.shutil.copy(
            tmp,
            path,
            driver="COG",
            COMPRESS="DEFLATE",
            BLOCKSIZE="512",
            OVERVIEW_RESAMPLING="NEAREST",
        )
    finally:
        tmp.unlink(missing_ok=True)
