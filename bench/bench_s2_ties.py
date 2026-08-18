"""S2 measurement, step 3: is every blocker change a tie?

The gate: for each cell whose blocker class changes between float64 and
float32, the two candidates disputing that sector must differ by less than one
quantization step (0.353 deg). If they all do, the class change is the
indeterminacy ADR-017 already named -- the argmax decided by centimetres --
and not an error. If any does not, there is a real defect and the session
stops.

Sweeps the whole city tile by tile, in both precisions, tracking *which*
offset won each cell. Then, for every disagreeing cell, recomputes both
candidates' angles in exact float64 and measures the gap. Also accumulates the
full distribution of the angular difference, which the entry measurement asks
for and the quantized cubes cannot show.
"""

import json
import math
import time
from pathlib import Path

import numpy as np

from shade_core.shade import NO_BLOCKER, Landcover
from shade_pipeline.horizon import ANGLE_MAX_DEG, HorizonParams, sector_offsets, tile_jobs

import os

VARIANT = os.environ.get("S2_VARIANT", "C1")
NPZ = Path("data/bench/montilla-test-padded.npz")
QUANTUM_DEG = ANGLE_MAX_DEG / 255.0

data = np.load(NPZ)
DSM, DTM, LC = data["dsm"], data["dtm"], data["landcover"]
INNER = tuple(int(v) for v in data["inner"])
RES = float(data["resolution_m"][0])
PARAMS = HorizonParams(sectors=64, max_distance_m=500.0, tile_size=512)
PAD = math.ceil(PARAMS.max_distance_m / RES)
OFFSETS = [sector_offsets(k, PARAMS, RES) for k in range(PARAMS.sectors)]
DATUM = float(os.environ.get("S2_DATUM", "0"))
print(f"variant {VARIANT}, datum {DATUM} m")

# Angular difference histogram: fine bins near zero, then decades.
EDGES = np.concatenate([[0.0], np.logspace(-9, 1, 101)])
histogram = np.zeros(len(EDGES) - 1, dtype=np.int64)
exact_zero = 0
max_diff = 0.0

ties: list[dict] = []
class_changes = 0
start = time.monotonic()


def sweep_sector(surface, noveg, observer, lc_win, shape, offsets, dtype):
    """One sector, returning (class, winning offset index, angle plane)."""
    best = np.full(shape, -np.inf, dtype=dtype)
    best_class = np.full(shape, NO_BLOCKER, dtype=np.uint8)
    best_idx = np.full(shape, -1, dtype=np.int32)
    rows, cols = surface.shape
    ra, ca = OFFSET_ORIGIN
    for index, (d_row, d_col, distance) in enumerate(offsets):
        i_lo, i_hi = max(0, -(ra + d_row)), min(shape[0], rows - ra - d_row)
        j_lo, j_hi = max(0, -(ca + d_col)), min(shape[1], cols - ca - d_col)
        if i_lo >= i_hi or j_lo >= j_hi:
            continue
        src = (
            slice(ra + i_lo + d_row, ra + i_hi + d_row),
            slice(ca + j_lo + d_col, ca + j_hi + d_col),
        )
        sub = (slice(i_lo, i_hi), slice(j_lo, j_hi))
        slope = (surface[src] - observer[sub]) / distance
        improved = slope > best[sub]
        np.copyto(best[sub], slope, where=improved)
        np.copyto(best_class[sub], lc_win[src], where=improved)
        np.copyto(best_idx[sub], np.int32(index), where=improved)
    open_sky = best <= 0.0
    best_class[open_sky] = NO_BLOCKER
    angles = np.maximum(np.degrees(np.arctan(best)), 0.0).astype(np.float32)
    return best_class, best_idx, angles


for job_number, job in enumerate(tile_jobs(INNER, PARAMS.tile_size), start=1):
    t0, t1, u0, u1 = job
    rows, cols = DSM.shape
    p0, p1 = max(0, t0 - PAD), min(rows, t1 + PAD)
    q0, q1 = max(0, u0 - PAD), min(cols, u1 + PAD)
    dsm_w, dtm_w, lc_w = DSM[p0:p1, q0:q1], DTM[p0:p1, q0:q1], LC[p0:p1, q0:q1]
    OFFSET_ORIGIN = (t0 - p0, u0 - q0)
    ra, ca = OFFSET_ORIGIN
    shape = (t1 - t0, u1 - u0)

    obs64 = dtm_w[ra : ra + shape[0], ca : ca + shape[1]].astype(np.float64) + 1.6
    obs32 = (
        dtm_w[ra : ra + shape[0], ca : ca + shape[1]].astype(np.float64) - DATUM + 1.6
    ).astype(np.float32)
    surf64 = dsm_w.astype(np.float64)
    surf32 = (dsm_w.astype(np.float64) - DATUM).astype(np.float32)
    noveg = np.where(lc_w == Landcover.VEGETATION, dtm_w, dsm_w)

    for k in range(PARAMS.sectors):
        cls64, idx64, ang64 = sweep_sector(
            surf64, noveg, obs64, lc_w, shape, OFFSETS[k], np.float64
        )
        cls32, idx32, ang32 = sweep_sector(
            surf32, noveg, obs32, lc_w, shape, OFFSETS[k], np.float32
        )

        diff = np.abs(ang64.astype(np.float64) - ang32.astype(np.float64))
        exact_zero += int((diff == 0.0).sum())
        max_diff = max(max_diff, float(diff.max()))
        histogram += np.histogram(diff[diff > 0.0], bins=EDGES)[0]

        disagree = np.argwhere(cls64 != cls32)
        class_changes += len(disagree)
        for i, j in disagree:
            candidates = []
            for cls, idx in ((cls64[i, j], idx64[i, j]), (cls32[i, j], idx32[i, j])):
                if cls == NO_BLOCKER:
                    # Nothing raised this sector: the rival is the open sky at 0.
                    candidates.append(("sky", 0.0))
                    continue
                d_row, d_col, distance = OFFSETS[k][int(idx)]
                dz = float(dsm_w[ra + i + d_row, ca + j + d_col]) - float(
                    dtm_w[ra + i, ca + j] + 1.6
                )
                candidates.append((int(cls), math.degrees(math.atan2(dz, distance))))
            gap = abs(candidates[0][1] - candidates[1][1])
            ties.append(
                {
                    "sector": k,
                    "row": int(t0 - INNER[0] + i),
                    "col": int(u0 - INNER[2] + j),
                    "class_f64": candidates[0][0],
                    "class_f32": candidates[1][0],
                    "angle_f64": round(candidates[0][1], 6),
                    "angle_f32": round(candidates[1][1], 6),
                    "gap_deg": gap,
                    "is_tie": bool(gap < QUANTUM_DEG),
                }
            )
    print(
        f"tile {job_number}/6 done ({time.monotonic() - start:.0f}s), "
        f"{class_changes} class changes so far",
        flush=True,
    )

not_ties = [t for t in ties if not t["is_tie"]]
gaps = sorted(t["gap_deg"] for t in ties)
total_cells = PARAMS.sectors * (INNER[1] - INNER[0]) * (INNER[3] - INNER[2])

print(f"\nquantum: {QUANTUM_DEG:.6f} deg")
print(f"class changes: {class_changes} of {total_cells:,} cells")
print(f"  all ties (gap < quantum): {not not_ties}")
if gaps:
    print(f"  gap: max {gaps[-1]:.6f} deg, p50 {gaps[len(gaps) // 2]:.6f}, min {gaps[0]:.6f}")
    print(f"  gap as fraction of a quantum: max {gaps[-1] / QUANTUM_DEG:.4f}")
for bad in not_ties[:20]:
    print(f"  NOT A TIE: {bad}")

print(f"\nangle difference over {total_cells:,} cells:")
print(f"  exactly zero: {exact_zero:,} ({100.0 * exact_zero / total_cells:.3f}%)")
print(f"  max: {max_diff:.3e} deg ({max_diff / QUANTUM_DEG:.2e} quanta)")
nonzero = int(histogram.sum())
if nonzero:
    cumulative = np.cumsum(histogram)
    for q in (0.5, 0.9, 0.99, 0.999, 1.0):
        idx = int(np.searchsorted(cumulative, q * nonzero))
        edge = EDGES[min(idx + 1, len(EDGES) - 1)]
        print(f"  p{q * 100:g} of the nonzero ones: <= {edge:.3e} deg")

Path(f"data/bench/s2-ties-{VARIANT.lower()}.json").write_text(
    json.dumps(
        {
            "quantum_deg": QUANTUM_DEG,
            "cells": total_cells,
            "class_changes": class_changes,
            "all_ties": not not_ties,
            "max_gap_deg": gaps[-1] if gaps else 0.0,
            "max_angle_diff_deg": max_diff,
            "exact_zero_cells": exact_zero,
            "ties": ties,
        },
        indent=2,
    )
)
print(f"wrote data/bench/s2-ties-{VARIANT.lower()}.json")
