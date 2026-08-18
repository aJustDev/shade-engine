"""Two sanity checks before the S5 gate figure is written down.

1. Is montilla-test's canopy typical? 9.455% of pixels under crown is the whole
   basis of the gate; the other built cities say whether that is a quirk of a
   1.28 km2 crop.
2. That "80.4% of the city changes skyline when crowns fall" needs a
   distribution, not a boolean. If most of it is a fraction of a degree in some
   far sector, the number means something very different from what it sounds
   like.
"""

from pathlib import Path

import numpy as np
import rasterio

print("canopy share per built city:")
for directory in sorted(Path("data/cities").glob("*/v1")):
    canopy_path = directory / "canopy.tif"
    if not canopy_path.exists():
        continue
    with rasterio.open(canopy_path) as src:
        canopy = src.read(1) != 0
    print(f"  {directory.parent.name:>16}  {canopy.shape}  {100.0 * canopy.mean():6.3f}%")

BASE = Path("data/bench/montilla-test-far500/v1")
scale = 90.0 / 255.0
worst = None
per_pixel_max = None
raised_cells = 0
total_cells = 0
hist = np.zeros(256, dtype=np.int64)
with (
    rasterio.open(BASE / "horizon.tif") as opaque,
    rasterio.open(BASE / "horizon_noveg.tif") as felled,
):
    for band in range(1, opaque.count + 1):
        delta = opaque.read([band])[0].astype(np.int16) - felled.read([band])[0].astype(np.int16)
        total_cells += delta.size
        raised_cells += int((delta > 0).sum())
        hist += np.bincount(delta.clip(0, 255).ravel(), minlength=256)
        per_pixel_max = delta if per_pixel_max is None else np.maximum(per_pixel_max, delta)

print(f"\ncube cells the crowns raise: {raised_cells:,} of {total_cells:,} "
      f"({100.0 * raised_cells / total_cells:.3f}%)")
for threshold_deg in (0.353, 1.0, 5.0, 20.0, 45.0):
    q = int(np.ceil(threshold_deg / scale))
    over = int(hist[q:].sum())
    print(f"  raised by more than {threshold_deg:5.2f} deg: {over:>12,} "
          f"({100.0 * over / total_cells:.3f}% of the cube)")

print("\nper pixel, the biggest drop across its 64 sectors:")
for threshold_deg in (0.353, 1.0, 5.0, 20.0, 45.0):
    over = int((per_pixel_max * scale > threshold_deg).sum())
    print(f"  more than {threshold_deg:5.2f} deg in at least one sector: {over:>9,} "
          f"({100.0 * over / per_pixel_max.size:.3f}% of the city)")
