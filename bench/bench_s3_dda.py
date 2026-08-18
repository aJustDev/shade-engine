"""S3, step 4: an arbiter from outside the family.

compute_horizon_reference, _ray_march_blocker and the exact-azimuth ray all
sample by rounding a nominal schedule, so all three inherit whatever the
distance convention gets wrong. None of them can referee this.

What can: exact ray traversal (Amanatides-Woo DDA) over the DSM read as what
it physically is -- a field of 1x1 m columns. For a ray leaving the observer
at azimuth A and elevation E, the column at cell c blocks the sun iff the ray
is below its top anywhere inside the cell's footprint, and since the ray only
climbs, that happens first at the cell's *entry* distance. So the exact
critical distance per cell is t_in, which the DDA gives in closed form.

Step 4a, here: pure geometry, no data. For every sector, compare the distance
each convention assigns to a cell against the true entry distance of the ray
that crosses it, and check which cells each method looks at in the first place.
"""

import json
import math
from pathlib import Path

import numpy as np

from shade_pipeline.horizon import HorizonParams, sector_offsets

PARAMS = HorizonParams(sectors=64, max_distance_m=500.0)
RES = 1.0


def dda_cells(azimuth_deg: float, max_distance_m: float, res: float):
    """Cells a ray crosses, with the distance at which it enters each.

    Amanatides & Woo (1987). The observer sits at the centre of cell (0, 0);
    the ray leaves in (east, north) and we walk cell boundaries in x and y,
    always stepping whichever comes first.
    """
    azimuth = math.radians(azimuth_deg)
    east, north = math.sin(azimuth), math.cos(azimuth)
    d_col, d_row = east, -north  # array axes: rows grow south

    cells = []
    col = row = 0

    # Distance along the ray to the next boundary in each axis, and the
    # distance between consecutive boundaries.
    def first_and_delta(direction: float) -> tuple[float, float, int]:
        if direction == 0.0:
            return math.inf, math.inf, 0
        step = 1 if direction > 0 else -1
        # from the cell centre, half a cell to the first boundary
        return (0.5 * res) / abs(direction), res / abs(direction), step

    t_x, dt_x, step_col = first_and_delta(d_col)
    t_y, dt_y, step_row = first_and_delta(d_row)
    t = 0.0
    while t < max_distance_m:
        if t_x < t_y:
            t = t_x
            t_x += dt_x
            col += step_col
        else:
            t = t_y
            t_y += dt_y
            row += step_row
        if t >= max_distance_m:
            break
        cells.append((row, col, t))
    return cells


report = {"sectors": {}}
gap_conv = {"nominal": [], "centre": [], "edge": []}
missing_total = extra_total = crossed_total = 0

for k in range(PARAMS.sectors):
    azimuth = k * 360.0 / PARAMS.sectors
    crossed = dda_cells(azimuth, PARAMS.max_distance_m, RES)
    entry = {(r, c): t for r, c, t in crossed}
    sampled = sector_offsets(k, PARAMS, RES)

    sampled_cells = {(r, c) for r, c, _ in sampled}
    crossed_cells = set(entry)
    missing = crossed_cells - sampled_cells  # the ray goes through, nobody looks
    extra = sampled_cells - crossed_cells  # looked at, the ray never enters
    missing_total += len(missing)
    extra_total += len(extra)
    crossed_total += len(crossed_cells)

    for d_row, d_col, nominal in sampled:
        true_entry = entry.get((d_row, d_col))
        if true_entry is None:
            continue
        centre = math.hypot(d_row, d_col) * RES
        gap_conv["nominal"].append(nominal - true_entry)
        gap_conv["centre"].append(centre - true_entry)
        gap_conv["edge"].append(centre - RES / 2.0 - true_entry)

print(f"cells the ray really crosses, per sector: {crossed_total / PARAMS.sectors:.1f}")
print(f"cells the sweep samples, per sector:      {len(sampled):.1f} (last sector)")
print(
    f"  crossed but never sampled: {missing_total / PARAMS.sectors:.1f} per sector "
    f"({100.0 * missing_total / crossed_total:.1f}%)"
)
print(f"  sampled though never crossed: {extra_total / PARAMS.sectors:.1f} per sector")

print("\nerror of each convention against the true entry distance (metres):")
print(f"  {'convention':>10}  {'mean':>9}  {'p50':>9}  {'p90':>9}  {'max':>9}  {'|mean|':>9}")
for name, values in gap_conv.items():
    v = np.array(values)
    print(
        f"  {name:>10}  {v.mean():+9.4f}  {np.median(v):+9.4f}  "
        f"{np.quantile(v, 0.9):+9.4f}  {v.max():+9.4f}  {np.abs(v).mean():9.4f}"
    )
    report["sectors"][name] = {
        "mean_m": float(v.mean()),
        "median_m": float(np.median(v)),
        "p90_m": float(np.quantile(v, 0.9)),
        "max_m": float(v.max()),
        "mean_abs_m": float(np.abs(v).mean()),
    }

report["crossed_per_sector"] = crossed_total / PARAMS.sectors
report["missing_pct"] = 100.0 * missing_total / crossed_total
Path("data/bench/s3-dda-geometry.json").write_text(json.dumps(report, indent=2))
print("\nwrote data/bench/s3-dda-geometry.json")
