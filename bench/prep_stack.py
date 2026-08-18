"""Rebuild the padded raster stack that montilla-test's sweep actually saw.

rasterize_lidar over the padded bbox + declutter (the build log shows no
footprints and a declutter pass), cached to an npz so the S1..S4 benchmarks
all measure the same real data.
"""

import sys
import time
from pathlib import Path

import numpy as np

from shade_core.config import load_city
from shade_pipeline.declutter import declutter_dsm
from shade_pipeline.grid import buffer_pixels, grid_shape, padded_bbox
from shade_pipeline.rasterize import rasterize_lidar

OUT = Path(sys.argv[1])
CITY = Path("cities/montilla-test.yaml")
LIDAR = sorted(Path("data/lidar/montilla").glob("*.laz"))

config = load_city(CITY)
res = config.resolution_m
pad = buffer_pixels(config.horizon_max_distance_m, res)
padded = padded_bbox(config.bbox, res, pad)
rows, cols = grid_shape(config.bbox, res)
print(f"city {rows}x{cols}, pad {pad}, padded {grid_shape(padded, res)}", flush=True)

start = time.monotonic()
stack = rasterize_lidar(LIDAR, padded, res, progress=lambda m: print(" ", m, flush=True))
print(
    f"binning {time.monotonic() - start:.1f}s, {sum(stack.point_counts.values()):,} pts", flush=True
)

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

# Sanity: the crop must match the published artifact bit for bit.
import rasterio

with rasterio.open("data/cities/montilla-test/v1/dsm.tif") as src:
    published = src.read(1)
crop = stack.dsm[pad : pad + rows, pad : pad + cols]
same = np.array_equal(published, crop)
print(f"dsm crop == published dsm.tif: {same}")
if not same:
    diff = np.abs(published - crop)
    print(f"  max diff {diff.max():.6f}, cells differing {(diff > 0).sum():,}")
