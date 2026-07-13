"""Derive the canopy mask artifact: vegetation actually overhead.

The raw rule ``landcover == VEGETATION`` over-shades: PNOA classifies low,
medium and high vegetation alike (ASPRS classes 3/4/5), so lawns, hedges and
flowerbeds count as "canopy" and paint permanent vegetation shade. The mask
keeps only vegetation tall enough to stand under::

    canopy = (landcover == VEGETATION) & (dsm - dtm >= CANOPY_MIN_HEIGHT_M)

``dsm - dtm`` is the CHM (canopy height model): the height of whatever sits
above the ground (see docs/learning/dsm-dtm-chm.md). A sieve filter then
drops connected regions smaller than ``CANOPY_SIEVE_PX`` pixels -- urban
LiDAR classification speckle (stray vegetation returns on facades, balconies,
street furniture). Sieve replaces small regions with their largest neighbor,
so it also FILLS sub-threshold holes inside large crowns; that bias is
accepted (an 8 m2 gap inside a crown is shaded anyway) and pinned by tests.
See docs/learning/canopy-sieve.md.

Crowns keep casting shade regardless of this mask: the horizon sweep reads
the DSM, which is untouched. The mask only answers "is there canopy overhead
at this pixel", the question :func:`shade_core.shade.is_shaded` short-circuits
on before consulting the horizon.
"""

from pathlib import Path
from typing import Final

import numpy as np
import numpy.typing as npt
import rasterio
from rasterio.features import sieve

from shade_core.artifacts import CANOPY_FILENAME, DSM_FILENAME, DTM_FILENAME, LANDCOVER_FILENAME
from shade_core.shade import Landcover
from shade_pipeline.cog import write_cog

CANOPY_MIN_HEIGHT_M: Final = 2.5
"""Vegetation below this CHM is not canopy: you stand next to it, not under it."""

CANOPY_SIEVE_PX: Final = 8
"""Minimum connected-region size in pixels (8 m2 at 1 m/px)."""


def canopy_mask(
    dsm: npt.NDArray[np.float32],
    dtm: npt.NDArray[np.float32],
    landcover: npt.NDArray[np.uint8],
) -> npt.NDArray[np.uint8]:
    """0/1 mask of pixels under vegetation at least ``CANOPY_MIN_HEIGHT_M`` tall.

    ``sieve`` needs an integer dtype (bool raises) and defaults to
    4-connectivity; 8 keeps diagonally-touching crown pixels as one region.
    """
    raw = (landcover == Landcover.VEGETATION) & (dsm - dtm >= CANOPY_MIN_HEIGHT_M)
    sieved: npt.NDArray[np.uint8] = sieve(
        raw.astype(np.uint8), size=CANOPY_SIEVE_PX, connectivity=8
    )
    return sieved


def derive_canopy(artifact_dir: str | Path) -> tuple[Path, int, int]:
    """Compute and write ``canopy.tif`` for an existing artifact directory.

    Backfills artifacts built before the mask existed (``build`` writes it
    since then) without re-running the horizon sweep. Returns the written
    path plus (canopy pixels, total pixels) for reporting.
    """
    directory = Path(artifact_dir)
    with rasterio.open(directory / DSM_FILENAME) as src:
        dsm = src.read()[0]
        georef = (src.transform, src.crs, src.shape)
        transform, crs = src.transform, src.crs.to_string()
    with rasterio.open(directory / DTM_FILENAME) as src:
        if (src.transform, src.crs, src.shape) != georef:
            raise ValueError(
                f"{directory / DTM_FILENAME}: georeference does not match "
                f"{DSM_FILENAME}; mixed artifact versions?"
            )
        dtm = src.read()[0]
    with rasterio.open(directory / LANDCOVER_FILENAME) as src:
        if (src.transform, src.crs, src.shape) != georef:
            raise ValueError(
                f"{directory / LANDCOVER_FILENAME}: georeference does not match "
                f"{DSM_FILENAME}; mixed artifact versions?"
            )
        landcover = src.read()[0].astype(np.uint8)
        city_id = src.tags().get("city_id")

    mask = canopy_mask(dsm, dtm, landcover)
    tags = {"min_height_m": str(CANOPY_MIN_HEIGHT_M), "sieve_px": str(CANOPY_SIEVE_PX)}
    if city_id is not None:
        tags["city_id"] = city_id
    path = directory / CANOPY_FILENAME
    write_cog(path, mask, transform, crs, tags=tags)
    return path, int(mask.sum()), int(mask.size)
