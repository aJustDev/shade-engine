"""S1 entry measurement: the horizon kernel on real Montilla data.

One 512x512 tile of montilla-test with the full 500 px pad, 8 of the 64
sectors timed, cost normalised by samples actually processed. Variants:

  A   today: float64, arctan2 per sample, np.where
  A2  arctan2 per sample, np.copyto(where=) in place   (isolates np.where)
  B   accumulate the tangent, copyto, one arctan per sector (the S1 change)
  D   B plus preallocated out= buffers                  (informative)
  C   B in float32                                      (S2's territory)

Every variant yields the same three planes per sector, so the quantized
cubes and the class cube are compared against A for exact equality.
"""

import itertools
import json
import math
import sys
import time
from pathlib import Path

import numpy as np

from shade_core.shade import NO_BLOCKER, Landcover
from shade_pipeline.horizon import HorizonParams, quantize_angles, sector_offsets

import os

NPZ = Path(sys.argv[1])
OUT = Path(sys.argv[2])
N_SECTORS_TIMED = 8
START = int(os.environ.get("S1_START", "0"))
ONLY = os.environ.get("S1_ONLY", "")
REPEATS = 2

data = np.load(NPZ)
DSM, DTM, LANDCOVER = data["dsm"], data["dtm"], data["landcover"]
RES = float(data["resolution_m"][0])
INNER = tuple(int(v) for v in data["inner"])
PARAMS = HorizonParams(sectors=64, max_distance_m=500.0, tile_size=512)

# First full tile of the real tile_jobs partition, and the window _sweep_tile
# would read around it: pad 500 on every side, uncropped.
row0, col0 = INNER[0], INNER[2]
TILE = (row0, row0 + 512, col0, col0 + 512)
PAD = math.ceil(PARAMS.max_distance_m / RES)
p0, p1 = TILE[0] - PAD, TILE[1] + PAD
q0, q1 = TILE[2] - PAD, TILE[3] + PAD
assert p0 >= 0 and q0 >= 0 and p1 <= DSM.shape[0] and q1 <= DSM.shape[1], "tile pad is cropped"
DSM_W, DTM_W, LC_W = DSM[p0:p1, q0:q1], DTM[p0:p1, q0:q1], LANDCOVER[p0:p1, q0:q1]
INNER_W = (TILE[0] - p0, TILE[1] - p0, TILE[2] - q0, TILE[3] - q0)


def offsets_and_samples() -> tuple[list[list[tuple[int, int, float]]], list[int]]:
    """Per-sector offsets and the number of array elements each sector touches."""
    per_sector, samples = [], []
    row_a, row_b, col_a, col_b = INNER_W
    rows, cols = DSM_W.shape
    height, width = row_b - row_a, col_b - col_a
    for k in range(N_SECTORS_TIMED):
        offs = sector_offsets(START + k, PARAMS, RES)
        per_sector.append(offs)
        total = 0
        for d_row, d_col, _ in offs:
            i_lo, i_hi = max(0, -(row_a + d_row)), min(height, rows - row_a - d_row)
            j_lo, j_hi = max(0, -(col_a + d_col)), min(width, cols - col_a - d_col)
            if i_lo < i_hi and j_lo < j_hi:
                total += (i_hi - i_lo) * (j_hi - j_lo)
        samples.append(total)
    return per_sector, samples


OFFSETS, SAMPLES = offsets_and_samples()


def _prepare(dtype):
    row_a, row_b, col_a, col_b = INNER_W
    observer = DTM_W[row_a:row_b, col_a:col_b].astype(dtype) + PARAMS.observer_height_m
    surface = DSM_W.astype(dtype)
    noveg = np.where(LC_W == Landcover.VEGETATION, DTM_W, DSM_W).astype(dtype)
    return observer, surface, noveg


def _slices(d_row, d_col):
    row_a, row_b, col_a, col_b = INNER_W
    rows, cols = DSM_W.shape
    height, width = row_b - row_a, col_b - col_a
    i_lo, i_hi = max(0, -(row_a + d_row)), min(height, rows - row_a - d_row)
    j_lo, j_hi = max(0, -(col_a + d_col)), min(width, cols - col_a - d_col)
    if i_lo >= i_hi or j_lo >= j_hi:
        return None
    src = (
        slice(row_a + i_lo + d_row, row_a + i_hi + d_row),
        slice(col_a + j_lo + d_col, col_a + j_hi + d_col),
    )
    return src, (slice(i_lo, i_hi), slice(j_lo, j_hi))


def variant_today(dtype=np.float64):
    """A: exactly what pipeline/horizon.py does today."""
    observer, surface, noveg = _prepare(dtype)
    row_a, row_b, col_a, col_b = INNER_W
    shape = (row_b - row_a, col_b - col_a)
    for k in range(N_SECTORS_TIMED):
        best = np.full(shape, -np.inf, dtype=dtype)
        best_class = np.full(shape, NO_BLOCKER, dtype=np.uint8)
        best_slope = np.full(shape, -np.inf, dtype=dtype)
        for d_row, d_col, distance in OFFSETS[k]:
            sl = _slices(d_row, d_col)
            if sl is None:
                continue
            src, sub = sl
            angle = np.degrees(np.arctan2(surface[src] - observer[sub], distance))
            improved = angle > best[sub]
            best[sub] = np.where(improved, angle, best[sub])
            best_class[sub] = np.where(improved, LC_W[src], best_class[sub])
            np.maximum(
                best_slope[sub], (noveg[src] - observer[sub]) / distance, out=best_slope[sub]
            )
        best_class[best <= 0.0] = NO_BLOCKER
        yield (
            np.maximum(best, 0.0).astype(np.float32),
            best_class,
            np.maximum(np.degrees(np.arctan(best_slope)), 0.0).astype(np.float32),
        )


def variant_arctan2_copyto(dtype=np.float64):
    """A2: still an arctan2 per sample, but in-place writes."""
    observer, surface, noveg = _prepare(dtype)
    row_a, row_b, col_a, col_b = INNER_W
    shape = (row_b - row_a, col_b - col_a)
    for k in range(N_SECTORS_TIMED):
        best = np.full(shape, -np.inf, dtype=dtype)
        best_class = np.full(shape, NO_BLOCKER, dtype=np.uint8)
        best_slope = np.full(shape, -np.inf, dtype=dtype)
        for d_row, d_col, distance in OFFSETS[k]:
            sl = _slices(d_row, d_col)
            if sl is None:
                continue
            src, sub = sl
            angle = np.degrees(np.arctan2(surface[src] - observer[sub], distance))
            improved = angle > best[sub]
            np.copyto(best[sub], angle, where=improved)
            np.copyto(best_class[sub], LC_W[src], where=improved)
            np.maximum(
                best_slope[sub], (noveg[src] - observer[sub]) / distance, out=best_slope[sub]
            )
        best_class[best <= 0.0] = NO_BLOCKER
        yield (
            np.maximum(best, 0.0).astype(np.float32),
            best_class,
            np.maximum(np.degrees(np.arctan(best_slope)), 0.0).astype(np.float32),
        )


def variant_tangent(dtype=np.float64):
    """B: accumulate dz/d, one arctan per sector, in-place writes."""
    observer, surface, noveg = _prepare(dtype)
    row_a, row_b, col_a, col_b = INNER_W
    shape = (row_b - row_a, col_b - col_a)
    for k in range(N_SECTORS_TIMED):
        best = np.full(shape, -np.inf, dtype=dtype)
        best_class = np.full(shape, NO_BLOCKER, dtype=np.uint8)
        best_slope = np.full(shape, -np.inf, dtype=dtype)
        for d_row, d_col, distance in OFFSETS[k]:
            sl = _slices(d_row, d_col)
            if sl is None:
                continue
            src, sub = sl
            slope = (surface[src] - observer[sub]) / distance
            improved = slope > best[sub]
            np.copyto(best[sub], slope, where=improved)
            np.copyto(best_class[sub], LC_W[src], where=improved)
            np.maximum(
                best_slope[sub], (noveg[src] - observer[sub]) / distance, out=best_slope[sub]
            )
        best_class[best <= 0.0] = NO_BLOCKER
        yield (
            np.maximum(np.degrees(np.arctan(best)), 0.0).astype(np.float32),
            best_class,
            np.maximum(np.degrees(np.arctan(best_slope)), 0.0).astype(np.float32),
        )


def variant_tangent_buffers(dtype=np.float64):
    """D: B with preallocated scratch, so no temporary per sample."""
    observer, surface, noveg = _prepare(dtype)
    row_a, row_b, col_a, col_b = INNER_W
    shape = (row_b - row_a, col_b - col_a)
    scratch = np.empty(shape, dtype=dtype)
    scratch2 = np.empty(shape, dtype=dtype)
    flag = np.empty(shape, dtype=bool)
    for k in range(N_SECTORS_TIMED):
        best = np.full(shape, -np.inf, dtype=dtype)
        best_class = np.full(shape, NO_BLOCKER, dtype=np.uint8)
        best_slope = np.full(shape, -np.inf, dtype=dtype)
        for d_row, d_col, distance in OFFSETS[k]:
            sl = _slices(d_row, d_col)
            if sl is None:
                continue
            src, sub = sl
            slope = scratch[sub]
            np.subtract(surface[src], observer[sub], out=slope)
            np.divide(slope, distance, out=slope)
            improved = flag[sub]
            np.greater(slope, best[sub], out=improved)
            np.copyto(best[sub], slope, where=improved)
            np.copyto(best_class[sub], LC_W[src], where=improved)
            other = scratch2[sub]
            np.subtract(noveg[src], observer[sub], out=other)
            np.divide(other, distance, out=other)
            np.maximum(best_slope[sub], other, out=best_slope[sub])
        best_class[best <= 0.0] = NO_BLOCKER
        yield (
            np.maximum(np.degrees(np.arctan(best)), 0.0).astype(np.float32),
            best_class,
            np.maximum(np.degrees(np.arctan(best_slope)), 0.0).astype(np.float32),
        )


ALL_VARIANTS = {
    "A today (float64, arctan2, where)": (variant_today, np.float64),
    "A2 arctan2 + copyto (float64)": (variant_arctan2_copyto, np.float64),
    "B tangent + copyto (float64)": (variant_tangent, np.float64),
    "D tangent + copyto + out= (float64)": (variant_tangent_buffers, np.float64),
    "C tangent + copyto (float32)": (variant_tangent, np.float32),
}


VARIANTS = (
    {k: v for k, v in ALL_VARIANTS.items() if k.split()[0] in ONLY.split(",")}
    if ONLY
    else ALL_VARIANTS
)


def run(fn, dtype):
    """Time each sector, keep the quantized planes."""
    planes, per_sector = [], []
    gen = fn(dtype)
    for _ in range(N_SECTORS_TIMED):
        start = time.perf_counter()
        angles, blocker, noveg = next(gen)
        per_sector.append(time.perf_counter() - start)
        planes.append((quantize_angles(angles), blocker, quantize_angles(noveg), angles, noveg))
    gen.close()
    return per_sector, planes


total_samples = sum(SAMPLES)
px = (INNER_W[1] - INNER_W[0]) * (INNER_W[3] - INNER_W[2])
print(f"tile {TILE}, window {DSM_W.shape}, pad {PAD}, sectors {START}..{START + N_SECTORS_TIMED - 1}")
print(
    f"{N_SECTORS_TIMED} sectors, {total_samples:,} sample-pixels "
    f"({total_samples / px / N_SECTORS_TIMED:.1f} samples/px/sector)\n",
    flush=True,
)

results = {}
baseline_planes = None
for name, (fn, dtype) in VARIANTS.items():
    best_time = None
    for _ in range(REPEATS):
        per_sector, planes = run(fn, dtype)
        elapsed = sum(per_sector)
        if best_time is None or elapsed < best_time:
            best_time, best_planes = elapsed, planes
    ns = best_time / total_samples * 1e9
    results[name] = {"seconds": best_time, "ns_per_px_sample": ns}
    if baseline_planes is None:
        baseline_planes = best_planes
        baseline_seconds = best_time
        results[name]["factor"] = 1.0
    else:
        results[name]["factor"] = (
            results["A today (float64, arctan2, where)"]["seconds"] / best_time
        )
        q_diff = cls_diff = noveg_diff = 0
        max_angle_diff = max_noveg_diff = 0.0
        q_steps: dict[int, int] = {}
        for (aq, cl, nq, af, nf), (bq, bcl, bnq, bf, bnf) in zip(baseline_planes, best_planes):
            d = np.abs(aq.astype(np.int16) - bq.astype(np.int16))
            q_diff += int((d > 0).sum())
            for step in np.unique(d[d > 0]):
                q_steps[int(step)] = q_steps.get(int(step), 0) + int((d == step).sum())
            cls_diff += int((cl != bcl).sum())
            noveg_diff += int((nq != bnq).sum())
            max_angle_diff = max(max_angle_diff, float(np.abs(af - bf).max()))
            max_noveg_diff = max(max_noveg_diff, float(np.abs(nf - bnf).max()))
        cells = px * N_SECTORS_TIMED
        results[name].update(
            quantized_diff=q_diff,
            quantized_diff_pct=100.0 * q_diff / cells,
            quantized_steps=q_steps,
            class_diff=cls_diff,
            class_diff_pct=100.0 * cls_diff / cells,
            noveg_quantized_diff=noveg_diff,
            max_angle_diff_deg=max_angle_diff,
            max_noveg_diff_deg=max_noveg_diff,
            cells_compared=cells,
        )
    r = results[name]
    line = f"{name:38s} {r['seconds']:7.2f}s  {ns:6.2f} ns/px/sample  {r['factor']:5.2f}x"
    if "quantized_diff" in r:
        line += (
            f"  q!={r['quantized_diff']:,} ({r['quantized_diff_pct']:.4f}%)"
            f"  class!={r['class_diff']:,}  maxdiff={r['max_angle_diff_deg']:.6f} deg"
        )
    print(line, flush=True)

OUT.write_text(
    json.dumps(
        {
            "tile": TILE,
            "window": list(DSM_W.shape),
            "pad": PAD,
            "sectors_timed": N_SECTORS_TIMED,
            "samples": total_samples,
            "pixels": px,
            "results": results,
        },
        indent=2,
    )
)
print(f"\nwrote {OUT}")
