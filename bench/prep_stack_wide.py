"""The same stack as prep_stack.py, but padded as far as the LiDAR reaches.

S4 measured the radius downward (500 -> 250 -> 125) and found its bill is paid
with the sun low. The other half was never measured with a temporal metric: the
"500 vs 2000 m changes nothing" in muestreo-del-horizonte is an *instantaneous*
figure, and this session just showed that an instantaneous figure says nothing
about interval boundaries -- cutting to 250 m moved shade_timeline edges by up
to half an hour while leaving the instant verdict identical above 10 degrees.

2000 m is not reachable here: montilla-test would need 6x6 LAZ tiles and the
cache holds 4x3 (x 353-356, y 4160-4162). Around the bbox that leaves 1046 m
west, 1465 m east, 986 m south and 1154 m north, so **986 m is the largest
uniform pad the data supports** and this builds 950 to stay inside it.

Doubling the radius is not quadrupling it, but it answers the qualitative
question S7 needs before it designs its walk: does the far field move sunset
boundaries at all?
"""

import sys
import time
from pathlib import Path

import numpy as np

from shade_core.config import load_city
from shade_pipeline.declutter import declutter_dsm
from shade_pipeline.grid import buffer_pixels, grid_shape, padded_bbox
from shade_pipeline.rasterize import rasterize_lidar

OUT = Path(sys.argv[1] if len(sys.argv) > 1 else "data/bench/montilla-test-wide.npz")
WIDE_DISTANCE_M = 950.0
CITY = Path("cities/montilla-test.yaml")
LIDAR = sorted(Path("data/lidar/montilla").glob("*.laz"))

config = load_city(CITY)
res = config.resolution_m
pad = buffer_pixels(WIDE_DISTANCE_M, res)
padded = padded_bbox(config.bbox, res, pad)
rows, cols = grid_shape(config.bbox, res)
print(f"city {rows}x{cols}, pad {pad}, padded {grid_shape(padded, res)}", flush=True)

start = time.monotonic()
stack = rasterize_lidar(LIDAR, padded, res, progress=lambda m: print(" ", m, flush=True))
print(
    f"binning {time.monotonic() - start:.1f}s, {sum(stack.point_counts.values()):,} pts", flush=True
)

# How much of the padded window the LiDAR actually covers: a nodata ring would
# quietly mean "no obstacles out there", which is not the same as measuring.
covered = float(np.isfinite(stack.dsm).mean())
print(f"padded window covered by data: {100.0 * covered:.2f}%")

cleaned = declutter_dsm(stack.dsm, stack.dtm, stack.landcover, None)
print(f"declutter: {cleaned.linear_px:,} linear px, {cleaned.slab_px:,} slab px", flush=True)

np.savez(
    OUT,
    dsm=stack.dsm,
    dtm=stack.dtm,
    landcover=stack.landcover,
    inner=np.array([pad, pad + rows, pad, pad + cols]),
    resolution_m=np.array([res]),
)
print(f"wrote {OUT} ({OUT.stat().st_size / 1e6:.0f} MB)")

import rasterio  # noqa: E402

with rasterio.open("data/cities/montilla-test/v1/dsm.tif") as src:
    published = src.read(1)
crop = stack.dsm[pad : pad + rows, pad : pad + cols]
print(f"dsm crop == published dsm.tif: {np.array_equal(published, crop)}")
