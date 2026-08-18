"""S3, step 1: the geometric bias of the distance convention, exactly.

No sweep and no data: just the 34,528 offsets the sweep really uses, and what
distance each of the three candidate conventions assigns to the same cell.

  nominal (today)  the ray parameter at which the sample first rounded into
                   the cell -- always shorter than the cell it landed on
  centre           hypot(dr, dc) * res, the cell as a point at its centre
  near edge        hypot(dr, dc) * res - 0.5, the cell as a 1x1 m column that
                   blocks from its nearest face
"""

import json
import math
from pathlib import Path

import numpy as np

from shade_pipeline.horizon import HorizonParams, sector_offsets

PARAMS = HorizonParams(sectors=64, max_distance_m=500.0)
RES = 1.0

rows = []
for k in range(PARAMS.sectors):
    for d_row, d_col, nominal in sector_offsets(k, PARAMS, RES):
        centre = math.hypot(d_row, d_col) * RES
        rows.append((k, d_row, d_col, nominal, centre, centre - RES / 2.0))

data = np.array([(r[3], r[4], r[5]) for r in rows])
nominal, centre, near_edge = data[:, 0], data[:, 1], data[:, 2]
gap = centre - nominal

print(
    f"offsets: {len(rows):,} over {PARAMS.sectors} sectors ({len(rows) / PARAMS.sectors:.1f}/sector)"
)
print("\ndistance the sweep uses vs distance to the cell centre (metres):")
print(f"  nominal < centre in {100.0 * (gap > 0).mean():.2f}% of offsets")
print(
    f"  gap: mean {gap.mean():+.4f}, p50 {np.median(gap):+.4f}, "
    f"p90 {np.quantile(gap, 0.9):+.4f}, max {gap.max():+.4f}"
)
print(f"  as a fraction of a pixel: mean {gap.mean() / RES:+.3f}, max {gap.max() / RES:+.3f}")

# What that gap is worth in degrees depends on the obstacle's height. Rather
# than assume one, sweep the range a city actually spans: an eye at 1.6 m and
# obstacles from a kerb to a tower.
print("\nangular bias by obstacle height above the eye (deg), over all offsets:")
print(f"  {'dz (m)':>8}  {'mean':>8}  {'p90':>8}  {'max':>8}")
per_height = {}
for dz in (1.0, 3.0, 10.0, 20.0, 40.0):
    bias = np.degrees(np.arctan(dz / nominal)) - np.degrees(np.arctan(dz / centre))
    per_height[dz] = {
        "mean": float(bias.mean()),
        "p90": float(np.quantile(bias, 0.9)),
        "max": float(bias.max()),
    }
    print(f"  {dz:8.1f}  {bias.mean():+8.4f}  {np.quantile(bias, 0.9):+8.4f}  {bias.max():+8.4f}")

# The near-edge convention against the centre: a flat half pixel, so its bias
# is the opposite sign and does not decay with distance the same way.
print("\nnear edge vs centre, same reading:")
for dz in (3.0, 10.0):
    bias = np.degrees(np.arctan(dz / near_edge)) - np.degrees(np.arctan(dz / centre))
    print(
        f"  dz={dz:4.1f} m: mean {bias.mean():+.4f}, p90 {np.quantile(bias, 0.9):+.4f}, "
        f"max {bias.max():+.4f}"
    )

# Where the bias lives: near samples or far ones?
print("\nwhere the nominal-vs-centre gap concentrates:")
for lo, hi in ((0, 5), (5, 25), (25, 100), (100, 500)):
    mask = (centre >= lo) & (centre < hi)
    if mask.any():
        print(
            f"  {lo:3d}-{hi:3d} m: {mask.sum():6,} offsets, gap mean {gap[mask].mean():+.4f} m, "
            f"max {gap[mask].max():+.4f}"
        )

Path("data/bench/s3-offsets.json").write_text(
    json.dumps(
        {
            "offsets": len(rows),
            "gap_mean_m": float(gap.mean()),
            "gap_p90_m": float(np.quantile(gap, 0.9)),
            "gap_max_m": float(gap.max()),
            "fraction_nominal_shorter": float((gap > 0).mean()),
            "angular_bias_by_height_deg": per_height,
        },
        indent=2,
    )
)
print("\nwrote data/bench/s3-offsets.json")
