"""S4, question 0b: which cell the corner tie SELECTS, not just which comes first.

The swap experiment only reordered the pair. The sharper question: at an exact
corner crossing the ray touches four cells at once -- the current one, the two
orthogonal neighbours and the diagonal -- and the DDA emits only ONE of the two
orthogonals, whichever the last bit of sin/cos happens to favour. Measured:
sector 8 emits the north neighbour, sector 24 the east one. Nobody chose that.

Under the solid-column reading of ADR-027, grazing a column's corner blocks, so
the consistent reading emits BOTH orthogonals. This measures what that would
cost: the horizon angle of the four diagonal sectors, with and without the cell
the ulp currently drops.
"""

import json
import math
from pathlib import Path

import numpy as np

from shade_core.raycast import ray_cells
from shade_core.shade import NO_BLOCKER
from shade_pipeline.horizon import ANGLE_MAX_DEG, height_datum

DIAGONALS = (8, 24, 40, 56)
SECTORS = 64
MAX_DISTANCE_M = 500.0
OBSERVER_H = 1.6

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


def with_both_corners(
    cells: list[tuple[int, int, float, float]],
) -> list[tuple[int, int, float, float]]:
    """Add the orthogonal neighbour the ulp drops at every corner crossing.

    A tied pair is (orthogonal, diagonal): the ray reaches the corner, grazes
    one orthogonal cell and enters the diagonal. The other orthogonal -- the one
    reachable by stepping the other axis -- is touched at the very same corner
    and never emitted. Its offset is the diagonal's minus the emitted one.
    """
    out = []
    index = 0
    while index < len(cells):
        cell = cells[index]
        out.append(cell)
        nxt = cells[index + 1] if index + 1 < len(cells) else None
        if nxt is not None and math.isclose(cell[2], nxt[2], rel_tol=1e-9):
            # cell = orthogonal, nxt = diagonal; the mirror orthogonal completes
            # the square around the corner.
            mirror = (nxt[0] - cell[0], nxt[1] - cell[1])
            previous = out[-2][:2] if len(out) >= 2 else (0, 0)
            mirror = (previous[0] + mirror[0], previous[1] + mirror[1])
            out.append((mirror[0], mirror[1], cell[2], cell[3]))
            out.append(nxt)
            index += 2
        else:
            index += 1
    return out


def sweep_sector(cells: list[tuple[int, int, float, float]]) -> np.ndarray:
    best_slope = np.full((HEIGHT, WIDTH), -np.inf, dtype=np.float32)
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
        np.maximum(
            best_slope[sub],
            (SURFACE[src] - OBSERVER[sub]) / np.float32(entry),
            out=best_slope[sub],
        )
    return np.degrees(np.arctan(best_slope)).astype(np.float32)


report = {"sectors": {}}
scale = ANGLE_MAX_DEG / 255.0

for sector in DIAGONALS:
    azimuth = sector * 360.0 / SECTORS
    cells = ray_cells(azimuth, MAX_DISTANCE_M, RES)
    both = with_both_corners(cells)
    emitted = {(r, c) for r, c, _, _ in cells}
    added = [(r, c) for r, c, _, _ in both if (r, c) not in emitted]
    assert len(added) == len(set(added)), "a mirror cell was emitted twice"
    assert len(both) == len(cells) + len(added), "with_both_corners lost a cell"
    # Every added cell must touch the corner it was derived from: Chebyshev 1
    # from the diagonal that follows it in the walk.
    by_offset = {(r, c): index for index, (r, c, _, _) in enumerate(both)}
    for row, col in added:
        after = both[by_offset[(row, col)] + 1]
        assert max(abs(after[0] - row), abs(after[1] - col)) == 1, (row, col, after[:2])

    angles_now = sweep_sector(cells)
    angles_both = sweep_sector(both)
    delta = angles_both - angles_now
    moved = int((delta != 0).sum())
    quant_now = np.clip(np.rint(angles_now * 255.0 / ANGLE_MAX_DEG), 0, 255).astype(np.uint8)
    quant_both = np.clip(np.rint(angles_both * 255.0 / ANGLE_MAX_DEG), 0, 255).astype(np.uint8)
    quant_moved = int((quant_now != quant_both).sum())
    worst = float(delta.max()) if moved else 0.0

    report["sectors"][sector] = {
        "azimuth_deg": azimuth,
        "cells_now": len(cells),
        "cells_both": len(both),
        "cells_added": len(added),
        "first_added": list(added[0]) if added else None,
        "float_moved": moved,
        "quantized_moved": quant_moved,
        "quantized_pct": 100.0 * quant_moved / quant_now.size,
        "worst_deg": worst,
        "mean_deg": float(delta.mean()),
        "over_1_deg": int((delta > 1.0).sum()),
        "over_5_deg": int((delta > 5.0).sum()),
    }
    print(
        f"sector {sector:2d} ({azimuth:3.0f} deg): {len(cells)} -> {len(both)} cells "
        f"(+{len(added)}) | horizon rises at {moved:,} px, quantized {quant_moved:,} "
        f"({100.0 * quant_moved / quant_now.size:.3f}%) | worst +{worst:.2f} deg, "
        f"mean {delta.mean():+.4f}, >1 deg {(delta > 1.0).sum():,}, >5 deg {(delta > 5.0).sum():,}",
        flush=True,
    )

total_q = sum(s["quantized_moved"] for s in report["sectors"].values())
total_over5 = sum(s["over_5_deg"] for s in report["sectors"].values())
report["totals"] = {
    "quantized_moved": total_q,
    "over_5_deg": total_over5,
    "plane_cells": int(HEIGHT * WIDTH),
    "pct_of_cube": 100.0 * total_q / (SECTORS * HEIGHT * WIDTH),
}
print(
    f"\nemitting both corners moves {total_q:,} quantized cells "
    f"({100.0 * total_q / (SECTORS * HEIGHT * WIDTH):.4f}% of the whole cube), "
    f"{total_over5:,} of them by more than 5 degrees"
)
Path("data/bench/s4-corners.json").write_text(json.dumps(report, indent=2))
print("wrote data/bench/s4-corners.json")
