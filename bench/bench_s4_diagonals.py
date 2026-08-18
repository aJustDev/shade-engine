"""S4, question 0: what the corner tie in the diagonal sectors actually decides.

In sectors 8, 24, 40 and 56 (45, 135, 225, 315 deg) every step of the ray
clips a cell corner, so ``ray_cells`` emits two cells at the same entry
distance -- one of them with zero thickness. Which of the two comes out first
is decided by the last bit of sin vs cos, and there is no tolerance in
``ray_cells``: measured on this platform they differ by 1 to 3 ulp in all four.

Two things could depend on that order:

- the horizon **angle**: it should not, since both cells are visited and their
  distances differ by ~1e-16 m;
- the blocker **class**: it can, because ``>`` with ascending distances gives
  ties to whichever cell was emitted first.

This measures both by sweeping the four diagonal sectors twice, swapping the
order inside every tied pair the second time. Anything that changes is
something a libm update could change too.
"""

import json
import math
from pathlib import Path

import numpy as np

from shade_core.raycast import ray_cells
from shade_core.shade import NO_BLOCKER, Landcover
from shade_pipeline.horizon import ANGLE_MAX_DEG, height_datum

DIAGONALS = (8, 24, 40, 56)
SECTORS = 64
MAX_DISTANCE_M = 500.0
OBSERVER_H = 1.6

data = np.load("data/bench/montilla-test-padded.npz")
DSM, DTM, LANDCOVER = data["dsm"], data["dtm"], data["landcover"]
INNER = tuple(int(v) for v in data["inner"])
RES = float(data["resolution_m"][0])
ROW0, ROW1, COL0, COL1 = INNER
HEIGHT, WIDTH = ROW1 - ROW0, COL1 - COL0
ROWS, COLS = DSM.shape

DATUM = np.float32(height_datum(DTM))
OBSERVER = (DTM[ROW0:ROW1, COL0:COL1] - DATUM) + np.float32(OBSERVER_H)
SURFACE = DSM - DATUM


def swap_ties(cells: list[tuple[int, int, float, float]]) -> list[tuple[int, int, float, float]]:
    """Same cells, with every pair sharing an entry distance emitted the other way."""
    out = list(cells)
    i = 0
    while i + 1 < len(out):
        if math.isclose(out[i][2], out[i + 1][2], rel_tol=1e-9):
            out[i], out[i + 1] = out[i + 1], out[i]
            i += 2
        else:
            i += 1
    return out


def sweep_sector(cells: list[tuple[int, int, float, float]]) -> tuple[np.ndarray, np.ndarray]:
    """One sector's (angle, blocker class), the same accumulator the sweep uses."""
    best_slope = np.full((HEIGHT, WIDTH), -np.inf, dtype=np.float32)
    best_class = np.full((HEIGHT, WIDTH), NO_BLOCKER, dtype=np.uint8)
    for d_row, d_col, entry, _ in cells:
        i_lo = max(0, -(ROW0 + d_row))
        i_hi = min(HEIGHT, ROWS - ROW0 - d_row)
        j_lo = max(0, -(COL0 + d_col))
        j_hi = min(WIDTH, COLS - COL0 - d_col)
        if i_lo >= i_hi or j_lo >= j_hi:
            continue
        sub = (slice(i_lo, i_hi), slice(j_lo, j_hi))
        src = (
            slice(ROW0 + i_lo + d_row, ROW0 + i_hi + d_row),
            slice(COL0 + j_lo + d_col, COL0 + j_hi + d_col),
        )
        slope = (SURFACE[src] - OBSERVER[sub]) / np.float32(entry)
        improved = slope > best_slope[sub]
        np.copyto(best_slope[sub], slope, where=improved)
        np.copyto(best_class[sub], LANDCOVER[src], where=improved)
    angles = np.degrees(np.arctan(best_slope)).astype(np.float32)
    best_class[best_slope <= 0.0] = NO_BLOCKER
    return angles, best_class


report = {"sectors": {}, "ulp": {}}
total_angle_diff = total_class_diff = 0
scale = ANGLE_MAX_DEG / 255.0

for sector in DIAGONALS:
    azimuth = sector * 360.0 / SECTORS
    radians = math.radians(azimuth)
    cells = ray_cells(azimuth, MAX_DISTANCE_M, RES)
    ties = sum(
        1 for a, b in zip(cells, cells[1:], strict=False) if math.isclose(a[2], b[2], rel_tol=1e-9)
    )
    angles_a, class_a = sweep_sector(cells)
    angles_b, class_b = sweep_sector(swap_ties(cells))

    angle_diff = int((angles_a != angles_b).sum())
    quant_a = np.clip(np.rint(angles_a * 255.0 / ANGLE_MAX_DEG), 0, 255).astype(np.uint8)
    quant_b = np.clip(np.rint(angles_b * 255.0 / ANGLE_MAX_DEG), 0, 255).astype(np.uint8)
    quant_diff = int((quant_a != quant_b).sum())
    class_diff = int((class_a != class_b).sum())
    worst = float(np.abs(angles_a - angles_b).max()) if angle_diff else 0.0
    total_angle_diff += quant_diff
    total_class_diff += class_diff

    report["sectors"][sector] = {
        "azimuth_deg": azimuth,
        "cells": len(cells),
        "tied_pairs": ties,
        "sin": math.sin(radians),
        "cos": math.cos(radians),
        "first_emitted": list(cells[0][:2]),
        "angle_float_differing": angle_diff,
        "angle_quantized_differing": quant_diff,
        "worst_angle_deg": worst,
        "class_differing": class_diff,
        "class_pct": 100.0 * class_diff / class_a.size,
    }
    print(
        f"sector {sector:2d} ({azimuth:3.0f} deg): {len(cells):3d} cells, {ties:3d} tied pairs, "
        f"first ({cells[0][0]:+d},{cells[0][1]:+d}) | "
        f"angle float {angle_diff:,} quantized {quant_diff:,} (worst {worst:.2e} deg) | "
        f"class {class_diff:,} of {class_a.size:,} ({100.0 * class_diff / class_a.size:.4f}%)",
        flush=True,
    )

# How far apart the two are, in ulp of the boundary distance: that is the whole
# margin the emission order rests on.
for sector in DIAGONALS:
    azimuth = math.radians(sector * 360.0 / SECTORS)
    si, co = abs(math.sin(azimuth)), abs(math.cos(azimuth))
    first_col, first_row = (0.5 * RES) / si, (0.5 * RES) / co
    gap = abs(first_col - first_row)
    report["ulp"][sector] = {
        "gap_m": gap,
        "ulp": gap / math.ulp(first_col) if gap else 0.0,
    }
    print(f"  sector {sector:2d}: boundary gap {gap:.3e} m = {gap / math.ulp(first_col):.1f} ulp")

report["totals"] = {
    "quantized_angle_differing": total_angle_diff,
    "class_differing": total_class_diff,
    "cells_per_sector_plane": int(HEIGHT * WIDTH),
    "class_pct_of_four_sectors": 100.0 * total_class_diff / (4 * HEIGHT * WIDTH),
}
print(
    f"\nover the four diagonal sectors: {total_angle_diff:,} quantized angles and "
    f"{total_class_diff:,} classes move if the tie order flips "
    f"({100.0 * total_class_diff / (4 * HEIGHT * WIDTH):.4f}% of those four planes)"
)
Path("data/bench/s4-diagonals.json").write_text(json.dumps(report, indent=2))
print("wrote data/bench/s4-diagonals.json")
