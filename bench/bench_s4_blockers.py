"""S4, step 3: of the cells geometric skips, how many are the ONLY blocker.

The question the auditor put, and the one that actually decides the mode. A
horizon error of 16 degrees means nothing on its own: if the cell that was
skipped is the third-highest thing in its sector, dropping it costs zero,
because something nearer already won the argmax. It only costs when the skipped
cell is the one holding the sector up -- and only then in the azimuths and hours
the sun really occupies.

So this counts, per sector and per pixel:

- ``sole``: the winning cell of the exact sweep is one geometric drops, AND
  nothing geometric keeps comes within reach of it. The horizon collapses.
- ``covered``: the winner is dropped but a kept cell lands close behind, so the
  loss is small.
- ``kept``: the winner survives the thinning; geometric costs nothing here.

Reported both in cells of the cube and, for the sole ones, in how far the
horizon falls -- because that is what turns into a verdict at some hour.

**Retired with what it measured.** ``step_mode="geometric"`` left the code in
40fe8fc, so this script no longer runs against the current engine. It is kept
as the record of how ADR-028's figures were obtained, not as something to
re-run: that would mean reverting the commit that retired the mode.
"""

import json
from collections import Counter
from pathlib import Path

import numpy as np

from shade_core.raycast import ray_cells
from shade_pipeline.horizon import ANGLE_MAX_DEG, HorizonParams, height_datum, sector_offsets

SECTORS = 64
MAX_DISTANCE_M = 500.0
OBSERVER_H = 1.6
SAMPLE_SECTORS = tuple(range(0, 64, 4))
"""Every fourth sector: 16 of 64, enough for a distribution and a quarter of the cost."""

data = np.load("data/bench/montilla-test-padded.npz")
DSM, DTM = data["dsm"], data["dtm"]
INNER = tuple(int(v) for v in data["inner"])
RES = float(data["resolution_m"][0])
ROW0, ROW1, COL0, COL1 = INNER
HEIGHT, WIDTH = ROW1 - ROW0, COL1 - COL0
ROWS, COLS = DSM.shape

DATUM = np.float32(height_datum(DTM))
OBSERVER = (DTM[ROW0:ROW1, COL0:COL1] - DATUM) + np.float32(OBSERVER_H)
SURFACE = DSM - DATUM

EXACT = HorizonParams(sectors=SECTORS, max_distance_m=MAX_DISTANCE_M, step_mode="exact")
GEOMETRIC = HorizonParams(sectors=SECTORS, max_distance_m=MAX_DISTANCE_M, step_mode="geometric")


def sweep(cells: list[tuple[int, int, float]]) -> np.ndarray:
    """Best slope per pixel over the given (d_row, d_col, distance) list."""
    best = np.full((HEIGHT, WIDTH), -np.inf, dtype=np.float32)
    for d_row, d_col, distance in cells:
        i_lo, i_hi = max(0, -(ROW0 + d_row)), min(HEIGHT, ROWS - ROW0 - d_row)
        j_lo, j_hi = max(0, -(COL0 + d_col)), min(WIDTH, COLS - COL0 - d_col)
        if i_lo >= i_hi or j_lo >= j_hi:
            continue
        sub = (slice(i_lo, i_hi), slice(j_lo, j_hi))
        src = (
            slice(ROW0 + i_lo + d_row, ROW0 + i_hi + d_row),
            slice(COL0 + j_lo + d_col, COL0 + j_hi + d_col),
        )
        np.maximum(best[sub], (SURFACE[src] - OBSERVER[sub]) / np.float32(distance), out=best[sub])
    return best


report = {"sectors": {}}
buckets: Counter[str] = Counter()
falls: list[np.ndarray] = []
total_cells = 0

for sector in SAMPLE_SECTORS:
    exact_cells = sector_offsets(sector, EXACT, RES)
    geometric_cells = sector_offsets(sector, GEOMETRIC, RES)
    kept = {(r, c) for r, c, _ in geometric_cells}
    dropped = [cell for cell in exact_cells if (cell[0], cell[1]) not in kept]

    exact_best = sweep(exact_cells)
    geometric_best = sweep(geometric_cells)
    # The cells geometric drops, on their own: what the thinning throws away.
    dropped_best = sweep(dropped) if dropped else np.full_like(exact_best, -np.inf)

    exact_deg = np.degrees(np.arctan(exact_best))
    geometric_deg = np.degrees(np.arctan(geometric_best))
    dropped_deg = np.degrees(np.arctan(dropped_best))
    fall = exact_deg - geometric_deg

    # A dropped cell "wins" where the exact horizon comes from the dropped set.
    won_by_dropped = dropped_deg >= exact_deg - 1e-6
    sole = won_by_dropped & (fall > 1.0)
    covered = won_by_dropped & ~sole
    buckets["kept"] += int((~won_by_dropped).sum())
    buckets["covered"] += int(covered.sum())
    buckets["sole"] += int(sole.sum())
    total_cells += exact_deg.size
    if sole.any():
        falls.append(fall[sole])

    report["sectors"][sector] = {
        "azimuth_deg": sector * 360.0 / SECTORS,
        "exact_cells": len(exact_cells),
        "geometric_cells": len(geometric_cells),
        "dropped_cells": len(dropped),
        "won_by_dropped": int(won_by_dropped.sum()),
        "sole": int(sole.sum()),
        "sole_pct": 100.0 * float(sole.mean()),
        "mean_fall_deg": float(fall.mean()),
        "p99_fall_deg": float(np.percentile(fall, 99)),
        "max_fall_deg": float(fall.max()),
    }
    print(
        f"sector {sector:2d} ({sector * 360 / SECTORS:5.1f} deg): "
        f"{len(exact_cells):3d} -> {len(geometric_cells):3d} cells, {len(dropped):3d} dropped | "
        f"sole blocker at {int(sole.sum()):7,} px ({100.0 * float(sole.mean()):.3f}%) | "
        f"fall mean {fall.mean():+.4f} p99 {np.percentile(fall, 99):.3f} max {fall.max():.2f} deg",
        flush=True,
    )

all_falls = np.concatenate(falls) if falls else np.array([0.0])
scale = ANGLE_MAX_DEG / 255.0
report["totals"] = {
    "sampled_sectors": len(SAMPLE_SECTORS),
    "cells": total_cells,
    "kept": buckets["kept"],
    "covered": buckets["covered"],
    "sole": buckets["sole"],
    "sole_pct": 100.0 * buckets["sole"] / total_cells,
    "sole_fall_median_deg": float(np.median(all_falls)),
    "sole_fall_p90_deg": float(np.percentile(all_falls, 90)),
    "sole_fall_max_deg": float(all_falls.max()),
    "sole_over_5_deg": int((all_falls > 5.0).sum()),
    "sole_over_20_deg": int((all_falls > 20.0).sum()),
}
print(f"\nover {len(SAMPLE_SECTORS)} sampled sectors, {total_cells:,} cells:")
print(
    f"  winner survives the thinning     {buckets['kept']:12,}  ({100.0 * buckets['kept'] / total_cells:6.3f}%)"
)
print(
    f"  winner dropped but covered       {buckets['covered']:12,}  ({100.0 * buckets['covered'] / total_cells:6.3f}%)"
)
print(
    f"  winner dropped and SOLE blocker  {buckets['sole']:12,}  ({100.0 * buckets['sole'] / total_cells:6.3f}%)"
)
print(
    f"\n  where it is the sole blocker the horizon falls: median {np.median(all_falls):.2f}, "
    f"p90 {np.percentile(all_falls, 90):.2f}, max {all_falls.max():.2f} deg; "
    f"{int((all_falls > 5.0).sum()):,} over 5 deg, {int((all_falls > 20.0).sum()):,} over 20"
)
Path("data/bench/s4-blockers.json").write_text(json.dumps(report, indent=2))
print("\nwrote data/bench/s4-blockers.json")
