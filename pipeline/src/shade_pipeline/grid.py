"""Grid arithmetic shared by every pipeline stage.

Single source of truth for how a city bbox (projected CRS, meters) maps to
raster shape, affine transform and horizon padding. Keeping it in one place
means the rasterizer, the horizon sweep and the exporter can never disagree
about which pixel a coordinate falls in.
"""

import math

from affine import Affine
from rasterio.transform import from_origin

from shade_core.config import Bbox


def buffer_pixels(max_distance_m: float, resolution_m: float) -> int:
    """Padding (pixels) so every horizon sample lands inside the raster.

    The sweep samples distances up to ``max_distance + resolution/4`` (the
    arange stop is exclusive), whose rounded offsets never exceed
    ``ceil(max_distance / resolution)`` -- round-half-even of ``x + 1/4``
    cannot exceed ``ceil(x)``. No extra ``+1`` is needed (or wanted: the
    coverage check demands data for the whole padded bbox).
    """
    return math.ceil(max_distance_m / resolution_m)


def padded_bbox(bbox: Bbox, resolution_m: float, buffer_px: int) -> Bbox:
    """Expand a bbox by a whole number of pixels on every side."""
    pad = buffer_px * resolution_m
    min_x, min_y, max_x, max_y = bbox
    return (min_x - pad, min_y - pad, max_x + pad, max_y + pad)


def grid_shape(bbox: Bbox, resolution_m: float) -> tuple[int, int]:
    """(rows, cols) covering the bbox, rounding partial cells outward."""
    min_x, min_y, max_x, max_y = bbox
    rows = math.ceil((max_y - min_y) / resolution_m)
    cols = math.ceil((max_x - min_x) / resolution_m)
    return rows, cols


def transform_from_bbox(bbox: Bbox, resolution_m: float) -> Affine:
    """North-up affine transform anchored at the bbox top-left corner.

    Matches core's ``HorizonGrid`` convention: origin at (x_min, y_max),
    rows growing southward (negative y pixel size), columns eastward.
    """
    min_x, _, _, max_y = bbox
    transform: Affine = from_origin(min_x, max_y, resolution_m, resolution_m)
    return transform
