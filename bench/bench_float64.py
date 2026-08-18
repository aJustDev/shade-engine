"""Is the tangent identity exact in float64, or only after the float32 cast?

Same tile, 8 sectors, but keeping the float64 accumulator: compares
max_k degrees(arctan2(dz_k, d_k)) against degrees(arctan(max_k dz_k/d_k)).
"""

import math
import sys
from pathlib import Path

import numpy as np

from shade_core.shade import NO_BLOCKER
from shade_pipeline.horizon import HorizonParams, sector_offsets

data = np.load(Path(sys.argv[1]))
DSM, DTM, LC = data["dsm"], data["dtm"], data["landcover"]
RES = float(data["resolution_m"][0])
INNER = tuple(int(v) for v in data["inner"])
PARAMS = HorizonParams(sectors=64, max_distance_m=500.0)
PAD = math.ceil(PARAMS.max_distance_m / RES)

row0, col0 = INNER[0], INNER[2]
TILE = (row0, row0 + 512, col0, col0 + 512)
p0, p1, q0, q1 = TILE[0] - PAD, TILE[1] + PAD, TILE[2] - PAD, TILE[3] + PAD
DSM_W, DTM_W = DSM[p0:p1, q0:q1], DTM[p0:p1, q0:q1]
ra, rb, ca, cb = TILE[0] - p0, TILE[1] - p0, TILE[2] - q0, TILE[3] - q0

observer = DTM_W[ra:rb, ca:cb].astype(np.float64) + PARAMS.observer_height_m
surface = DSM_W.astype(np.float64)
shape = (rb - ra, cb - ca)

total_ne = 0
total_ne_f32 = 0
max_abs = 0.0
max_ulps = 0
for k in range(8):
    best_angle = np.full(shape, -np.inf)
    best_slope = np.full(shape, -np.inf)
    for d_row, d_col, distance in sector_offsets(k, PARAMS, RES):
        rows, cols = DSM_W.shape
        i_lo, i_hi = max(0, -(ra + d_row)), min(shape[0], rows - ra - d_row)
        j_lo, j_hi = max(0, -(ca + d_col)), min(shape[1], cols - ca - d_col)
        if i_lo >= i_hi or j_lo >= j_hi:
            continue
        src = (
            slice(ra + i_lo + d_row, ra + i_hi + d_row),
            slice(ca + j_lo + d_col, ca + j_hi + d_col),
        )
        sub = (slice(i_lo, i_hi), slice(j_lo, j_hi))
        dz = surface[src] - observer[sub]
        np.maximum(best_angle[sub], np.degrees(np.arctan2(dz, distance)), out=best_angle[sub])
        np.maximum(best_slope[sub], dz / distance, out=best_slope[sub])
    a = np.maximum(best_angle, 0.0)
    b = np.maximum(np.degrees(np.arctan(best_slope)), 0.0)
    ne = a != b
    total_ne += int(ne.sum())
    if ne.any():
        max_abs = max(max_abs, float(np.abs(a[ne] - b[ne]).max()))
        ulps = np.abs(a[ne].view(np.int64) - b[ne].view(np.int64))
        max_ulps = max(max_ulps, int(ulps.max()))
    total_ne_f32 += int((a.astype(np.float32) != b.astype(np.float32)).sum())

cells = shape[0] * shape[1] * 8
print(f"cells compared: {cells:,}")
print(f"float64 values differing: {total_ne:,} ({100 * total_ne / cells:.4f}%)")
print(f"  max abs diff: {max_abs:.3e} deg, max ulps: {max_ulps}")
print(f"float32 values differing after cast: {total_ne_f32:,}")
