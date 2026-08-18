"""S2 measurement, step 5: how much of the float32 effect is a fixable bias?

`observer_z = dtm + 1.6` in float32 lands 6.104e-06 m too high on EVERY pixel
of montilla-test -- not on average, on every one. 1.6 m sits at 0.8 of a
float32 ulp at ~367 m of altitude, and every DTM value shares that ulp grid,
so the rounding goes the same way city-wide. A higher observer sees a lower
skyline, so the whole city loses shade by a fixed amount.

Three float32 variants against the float64 reference, on the S1 tile:

  C1  float32 as written (observer summed in float32)
  C2  observer summed in float64, cast down; the loop stays float32
  C3  heights relative to a datum (dtm.min()), then float32: the values drop
      from ~370 m to ~0-60 m, which buys back 3 bits of ulp
"""

import json
import math
from pathlib import Path

import numpy as np

from shade_core.shade import NO_BLOCKER, Landcover
from shade_pipeline.horizon import ANGLE_MAX_DEG, HorizonParams, quantize_angles, sector_offsets

N_SECTORS = 8
data = np.load("data/bench/montilla-test-padded.npz")
DSM, DTM, LC = data["dsm"], data["dtm"], data["landcover"]
INNER = tuple(int(v) for v in data["inner"])
RES = float(data["resolution_m"][0])
PARAMS = HorizonParams(sectors=64, max_distance_m=500.0)
PAD = math.ceil(PARAMS.max_distance_m / RES)

TILE = (INNER[0], INNER[0] + 512, INNER[2], INNER[2] + 512)
p0, p1, q0, q1 = TILE[0] - PAD, TILE[1] + PAD, TILE[2] - PAD, TILE[3] + PAD
DSM_W, DTM_W, LC_W = DSM[p0:p1, q0:q1], DTM[p0:p1, q0:q1], LC[p0:p1, q0:q1]
RA, CA = TILE[0] - p0, TILE[2] - q0
SHAPE = (TILE[1] - TILE[0], TILE[3] - TILE[2])
OFFSETS = [sector_offsets(k, PARAMS, RES) for k in range(N_SECTORS)]


def surfaces(variant):
    """(surface, noveg, observer) for a variant, in its own dtype."""
    h = PARAMS.observer_height_m
    noveg_raw = np.where(LC_W == Landcover.VEGETATION, DTM_W, DSM_W)
    if variant == "f64":
        obs = DTM_W[RA : RA + SHAPE[0], CA : CA + SHAPE[1]].astype(np.float64) + h
        return DSM_W.astype(np.float64), noveg_raw.astype(np.float64), obs
    if variant == "C1":
        obs = DTM_W[RA : RA + SHAPE[0], CA : CA + SHAPE[1]].astype(np.float32) + h
        return DSM_W.astype(np.float32), noveg_raw.astype(np.float32), obs
    if variant == "C2":
        obs = (DTM_W[RA : RA + SHAPE[0], CA : CA + SHAPE[1]].astype(np.float64) + h).astype(
            np.float32
        )
        return DSM_W.astype(np.float32), noveg_raw.astype(np.float32), obs
    if variant == "C3":
        datum = float(DTM_W.min())
        obs = (DTM_W[RA : RA + SHAPE[0], CA : CA + SHAPE[1]].astype(np.float64) - datum + h).astype(
            np.float32
        )
        return (
            (DSM_W.astype(np.float64) - datum).astype(np.float32),
            (noveg_raw.astype(np.float64) - datum).astype(np.float32),
            obs,
        )
    raise ValueError(variant)


def sweep(variant):
    surface, noveg, observer = surfaces(variant)
    dtype = surface.dtype
    rows, cols = surface.shape
    angles, classes, noveg_angles = [], [], []
    for k in range(N_SECTORS):
        best = np.full(SHAPE, -np.inf, dtype=dtype)
        best_class = np.full(SHAPE, NO_BLOCKER, dtype=np.uint8)
        best_noveg = np.full(SHAPE, -np.inf, dtype=dtype)
        for d_row, d_col, distance in OFFSETS[k]:
            i_lo, i_hi = max(0, -(RA + d_row)), min(SHAPE[0], rows - RA - d_row)
            j_lo, j_hi = max(0, -(CA + d_col)), min(SHAPE[1], cols - CA - d_col)
            if i_lo >= i_hi or j_lo >= j_hi:
                continue
            src = (
                slice(RA + i_lo + d_row, RA + i_hi + d_row),
                slice(CA + j_lo + d_col, CA + j_hi + d_col),
            )
            sub = (slice(i_lo, i_hi), slice(j_lo, j_hi))
            slope = (surface[src] - observer[sub]) / distance
            improved = slope > best[sub]
            np.copyto(best[sub], slope, where=improved)
            np.copyto(best_class[sub], LC_W[src], where=improved)
            np.maximum(
                best_noveg[sub], (noveg[src] - observer[sub]) / distance, out=best_noveg[sub]
            )
        best_class[best <= 0.0] = NO_BLOCKER
        angles.append(np.maximum(np.degrees(np.arctan(best)), 0.0).astype(np.float32))
        classes.append(best_class)
        noveg_angles.append(np.maximum(np.degrees(np.arctan(best_noveg)), 0.0).astype(np.float32))
    return angles, classes, noveg_angles


reference = sweep("f64")
cells = N_SECTORS * SHAPE[0] * SHAPE[1]
report = {}
for variant in ("C1", "C2", "C3"):
    angles, classes, noveg = sweep(variant)
    up = down = class_diff = 0
    signed_sum = 0.0
    max_abs = 0.0
    for (a, c, n), (ra_, rc, rn) in zip(zip(angles, classes, noveg), zip(*reference)):
        qa, qr = quantize_angles(a).astype(np.int16), quantize_angles(ra_).astype(np.int16)
        up += int((qa > qr).sum())
        down += int((qa < qr).sum())
        class_diff += int((c != rc).sum())
        delta = a.astype(np.float64) - ra_.astype(np.float64)
        signed_sum += float(delta.sum())
        max_abs = max(max_abs, float(np.abs(delta).max()))
    report[variant] = {
        "quantized_up": up,
        "quantized_down": down,
        "quantized_total": up + down,
        "pct": 100.0 * (up + down) / cells,
        "class_diff": class_diff,
        "mean_signed_angle_diff_deg": signed_sum / cells,
        "max_abs_angle_diff_deg": max_abs,
    }
    print(
        f"{variant}: cuantizado {up + down:6,} ({100.0 * (up + down) / cells:.5f}%) "
        f"[sube {up}, baja {down}]  clases {class_diff:4d}  "
        f"sesgo medio {signed_sum / cells:+.3e} deg  max |dif| {max_abs:.3e} deg",
        flush=True,
    )

report["cells"] = cells
report["quantum_deg"] = ANGLE_MAX_DEG / 255.0
Path("data/bench/s2-bias.json").write_text(json.dumps(report, indent=2))
print("wrote data/bench/s2-bias.json")
