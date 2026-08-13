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

    The write is verified: the finished COG is read back and compared band
    by band against ``data``, raising on any mismatch. Multi-hour builds
    cannot afford to assume the storage stack persisted every page.
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
            # Band interleave: each internal tile compresses one band, so
            # reading 2 of the horizon cube's 64 bands decompresses 2, not 64
            # (pixel interleave packs all bands into every tile). It also
            # keeps the band-by-band write loop below strictly sequential.
            interleave="band",
            # Classic TIFF caps at 4 GB of 32-bit offsets; IF_SAFER switches
            # to BigTIFF whenever the projected size could cross it.
            bigtiff="IF_SAFER",
        ) as dst:
            # One band at a time: a single write(cube) would materialize the
            # whole array, defeating memmapped horizon cubes at city scale.
            for band in range(bands):
                dst.write(cube[band], band + 1)
            if tags:
                dst.update_tags(**tags)
        rasterio.shutil.copy(
            tmp,
            path,
            driver="COG",
            COMPRESS="DEFLATE",
            BLOCKSIZE="512",
            OVERVIEW_RESAMPLING="NEAREST",
            INTERLEAVE="BAND",
            BIGTIFF="IF_SAFER",
        )
        # Trust nothing that took hours to compute: read the finished COG
        # back and require exact equality with the source, band by band. The
        # Cordoba build once lost the tail bands of the horizon cube to a
        # silent I/O failure and shipped artifacts that looked valid; this
        # readback is the contract that what was computed is what shipped.
        with rasterio.open(path) as src:
            for band in range(bands):
                if not np.array_equal(src.read([band + 1])[0], cube[band]):
                    raise ValueError(
                        f"{path.name}: band {band + 1} readback mismatch after COG write"
                    )
    finally:
        tmp.unlink(missing_ok=True)
