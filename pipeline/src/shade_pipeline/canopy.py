"""Derive the canopy mask artifact: vegetation actually overhead.

The raw rule ``landcover == VEGETATION`` over-shades: PNOA classifies low,
medium and high vegetation alike (ASPRS classes 3/4/5), so lawns, hedges and
flowerbeds count as "canopy" and paint permanent vegetation shade. The mask
keeps only vegetation tall enough to stand under::

    canopy = (landcover == VEGETATION) & (dsm - dtm >= CANOPY_MIN_HEIGHT_M)

``dsm - dtm`` is the CHM (canopy height model): the height of whatever sits
above the ground (see shade-docs: learning/dsm-dtm-chm.md). A sieve filter then
drops connected regions smaller than ``CANOPY_SIEVE_PX`` pixels -- urban
LiDAR classification speckle (stray vegetation returns on facades, balconies,
street furniture). Sieve replaces small regions with their largest neighbor,
so it also FILLS sub-threshold holes inside large crowns; that bias is
accepted (an 8 m2 gap inside a crown is shaded anyway) and pinned by tests.
See shade-docs: learning/canopy-sieve.md.

Size is not enough, though, and the Plaza de la Corredera proved it: a square
with no tree in it, 20% of its paving labelled vegetation, and four blobs
large enough to survive the sieve -- two flat slabs at 2.6 m (awnings, sd of
the CHM 0.06 and 0.07 m) and a 14 m straight line at 10 m (a cable the
classifier read as "high vegetation"). So the mask also looks at the *shape*
of each surviving region:

- **Roughness.** A crown is a rough surface: leaves, gaps, branch structure,
  all sampled by a 5 pt/m2 LiDAR. An awning is a plane. Regions flatter than
  :data:`CANOPY_ROUGHNESS_MIN_M` are not crowns.
- **Linearity.** A cable is one or two pixels wide and runs for tens of
  metres. Nothing that grows does that.

The roughness threshold is the delicate one, and it was measured rather than
guessed. A pruned Cordoba orange tree -- the most characteristic street tree
in the city -- has sd(CHM) between 0.26 and 0.52 m: nearly as flat as an
awning. The obvious threshold of 0.4-0.5 would have felled the Patio de los
Naranjos. See ``shade-docs: learning/clasificacion-lidar-pnoa.md``.

Deliberately with no height condition: the Corredera slabs sit at 2.6 m, so a
rule that only inspected tall regions would let exactly the artefacts through.

Crowns keep casting shade regardless of this mask: the horizon sweep reads
the DSM, which is untouched by anything in this module. The mask only answers
"is there canopy overhead at this pixel", the question
:func:`shade_core.shade.is_shaded` short-circuits on before consulting the
horizon.
"""

from pathlib import Path
from typing import Final

import numpy as np
import numpy.typing as npt
import rasterio
from rasterio.features import sieve
from scipy import ndimage

from shade_core.artifacts import CANOPY_FILENAME, DSM_FILENAME, DTM_FILENAME, LANDCOVER_FILENAME
from shade_core.shade import Landcover
from shade_pipeline.cog import write_cog
from shade_pipeline.declutter import STRUCTURE_8, linear_labels

CANOPY_MIN_HEIGHT_M: Final = 2.5
"""Vegetation below this CHM is not canopy: you stand next to it, not under it."""

CANOPY_SIEVE_PX: Final = 8
"""Minimum connected-region size in pixels (8 m2 at 1 m/px)."""

CANOPY_ROUGHNESS_MIN_M: Final = 0.20
"""Least standard deviation of the CHM a region needs to pass as a crown.

Measured against Cordoba's municipal tree inventory: at 0.20 the filter drops
1.9% of the regions with a catalogued tree under them and 0.07% of their
area, while removing 11.9% of the regions with no tree anywhere near. Raising
it to the 0.4-0.5 that looks right by eye would delete the city's own orange
trees, whose crowns measure 0.26-0.52.
"""

CANOPY_LINEAR_WIDTH_PX: Final = 2
"""Pixels per unit of length below which a region reads as a line, not a blob.

Compared against the region's longest bounding-box side rather than against a
bounding box in isolation, so a diagonal cable -- whose box is as tall as it
is wide -- is caught by the same rule as a horizontal one.
"""

CANOPY_LINEAR_MIN_PX: Final = 8
"""Length, in pixels, above which a thin region is a cable and not a branch."""

CANOPY_TAGS: Final = {
    "min_height_m": str(CANOPY_MIN_HEIGHT_M),
    "sieve_px": str(CANOPY_SIEVE_PX),
    "roughness_min_m": str(CANOPY_ROUGHNESS_MIN_M),
    "linear_width_px": str(CANOPY_LINEAR_WIDTH_PX),
    "linear_min_px": str(CANOPY_LINEAR_MIN_PX),
}
"""COG tags recording every threshold the mask was built with.

A mask is a verdict, not a measurement: without the thresholds beside it,
nobody looking at a finished artifact can tell a build that filtered from one
that did not.
"""


def canopy_mask(
    dsm: npt.NDArray[np.float32],
    dtm: npt.NDArray[np.float32],
    landcover: npt.NDArray[np.uint8],
) -> npt.NDArray[np.uint8]:
    """0/1 mask of pixels under vegetation at least ``CANOPY_MIN_HEIGHT_M`` tall.

    ``sieve`` needs an integer dtype (bool raises) and defaults to
    4-connectivity; 8 keeps diagonally-touching crown pixels as one region.
    Shape filtering runs after it, on the regions that survived: the sieve is
    also what closes the sub-threshold holes, so filtering first would judge
    the shape of a crown full of pinholes.
    """
    chm = dsm - dtm
    raw = (landcover == Landcover.VEGETATION) & (chm >= CANOPY_MIN_HEIGHT_M)
    sieved: npt.NDArray[np.uint8] = sieve(
        raw.astype(np.uint8), size=CANOPY_SIEVE_PX, connectivity=8
    )
    return drop_non_crowns(sieved, chm)


def drop_non_crowns(
    mask: npt.NDArray[np.uint8], chm: npt.NDArray[np.floating]
) -> npt.NDArray[np.uint8]:
    """Clear the regions of ``mask`` that are too flat or too thin to be crowns.

    8-connectivity, matching the sieve: a crown sampled through foliage is
    full of diagonal contacts, and 4-connectivity would shatter one tree into
    a dozen regions and judge each fragment's shape on its own.
    """
    labels, count = ndimage.label(mask, structure=STRUCTURE_8)
    if count == 0:
        return mask
    roughness = np.zeros(count + 1, dtype=np.float64)
    roughness[1:] = ndimage.standard_deviation(chm, labels=labels, index=np.arange(1, count + 1))
    # Index 0 is the background: never flat, never linear, never dropped.
    drop = linear_labels(
        labels, count, width_px=CANOPY_LINEAR_WIDTH_PX, min_length_px=CANOPY_LINEAR_MIN_PX
    )
    drop[1:] |= roughness[1:] < CANOPY_ROUGHNESS_MIN_M
    kept: npt.NDArray[np.uint8] = np.where(drop[labels], np.uint8(0), mask)
    return kept


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
    tags = dict(CANOPY_TAGS)
    if city_id is not None:
        tags["city_id"] = city_id
    path = directory / CANOPY_FILENAME
    write_cog(path, mask, transform, crs, tags=tags)
    return path, int(mask.sum()), int(mask.size)
