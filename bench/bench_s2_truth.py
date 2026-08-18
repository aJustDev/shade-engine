"""S2 measurement, step 4: of the pixels that flip, which precision is right?

Comparing float32 against float64 says they differ, not which one gets it
right -- the trap already written down in learning/muestreo-del-horizonte. For
the handful of pixel-instants that flip, the referee is cheap: march an exact
ray at the sun's true azimuth over the float64 DSM, half a pixel at a time, no
azimuth interpolation and no quantization, and ask it who was right.
"""

import json
import math
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

from shade_core.artifacts import load_metadata
from shade_core.solar import sun_position
from shade_pipeline.shade_raster import STATE_SUN, compute_state_raster
from shade_pipeline.tiles import bounds_wgs84, season_preset_instants

PUBLISHED = Path("data/cities/montilla-test/v1")
import os

VARIANT = os.environ.get("S2_VARIANT", "C1")
F32 = Path(f"data/bench/montilla-test-{VARIANT.lower()}/v1")
NPZ = Path("data/bench/montilla-test-padded.npz")
MAX_DISTANCE_M = 500.0
OBSERVER_H = 1.6

data = np.load(NPZ)
DSM, DTM = data["dsm"].astype(np.float64), data["dtm"].astype(np.float64)
INNER = tuple(int(v) for v in data["inner"])
RES = float(data["resolution_m"][0])


def exact_horizon_deg(row: int, col: int, azimuth_deg: float) -> float:
    """Horizon angle at the sun's exact azimuth from inner pixel (row, col).

    Same half-pixel schedule and same nearest-cell rounding as the sweep, but
    aimed at the true azimuth instead of a sector centre, and never quantized.
    This is the 1/64-of-a-sweep oracle of muestreo-del-horizonte.
    """
    r, c = INNER[0] + row, INNER[2] + col
    observer_z = DTM[r, c] + OBSERVER_H
    azimuth = math.radians(azimuth_deg)
    east, north = math.sin(azimuth), math.cos(azimuth)
    step = RES / 2.0
    best = 0.0
    distance = step
    rows, cols = DSM.shape
    while distance <= MAX_DISTANCE_M:
        d_col = round(distance * east / RES)
        d_row = -round(distance * north / RES)
        rr, cc = r + d_row, c + d_col
        if 0 <= rr < rows and 0 <= cc < cols and not (d_row == 0 and d_col == 0):
            angle = math.degrees(math.atan2(DSM[rr, cc] - observer_z, distance))
            best = max(best, angle)
        distance += step
    return best


metadata = load_metadata(PUBLISHED)
west, south, east, north = bounds_wgs84(metadata.crs, metadata.bbox)
center_lon, center_lat = (west + east) / 2.0, (south + north) / 2.0

verdicts = []
for when in season_preset_instants(ZoneInfo("Europe/Madrid")):
    sun = sun_position(center_lat, center_lon, when)
    if not sun.is_up:
        continue
    a = compute_state_raster(PUBLISHED, sun)
    b = compute_state_raster(F32, sun)
    flips = np.argwhere((a == STATE_SUN) != (b == STATE_SUN))
    for row, col in flips:
        horizon = exact_horizon_deg(int(row), int(col), sun.azimuth_deg)
        truth_shaded = sun.elevation_deg < horizon
        f64_shaded = bool(a[row, col] != STATE_SUN)
        f32_shaded = bool(b[row, col] != STATE_SUN)
        verdicts.append(
            {
                "when": when.isoformat(),
                "row": int(row),
                "col": int(col),
                "elevation_deg": round(sun.elevation_deg, 4),
                "azimuth_deg": round(sun.azimuth_deg, 4),
                "exact_horizon_deg": round(horizon, 4),
                "margin_deg": round(sun.elevation_deg - horizon, 4),
                "truth_shaded": bool(truth_shaded),
                "f64_shaded": f64_shaded,
                "f32_shaded": f32_shaded,
                "f64_right": f64_shaded == truth_shaded,
                "f32_right": f32_shaded == truth_shaded,
            }
        )
        print(
            f"  {when.isoformat()} ({row},{col}) elev {sun.elevation_deg:.3f} "
            f"exact horizon {horizon:.3f} margin {sun.elevation_deg - horizon:+.4f} "
            f"-> truth {'shade' if truth_shaded else 'sun':5s} "
            f"f64 {'shade' if f64_shaded else 'sun':5s} f32 {'shade' if f32_shaded else 'sun'}",
            flush=True,
        )

f64_right = sum(v["f64_right"] for v in verdicts)
f32_right = sum(v["f32_right"] for v in verdicts)
margins = [abs(v["margin_deg"]) for v in verdicts]
print(f"\nflips arbitrated: {len(verdicts)}")
print(f"  float64 right: {f64_right}")
print(f"  float32 right: {f32_right}")
if margins:
    print(f"  |elevation - exact horizon|: max {max(margins):.4f} deg, min {min(margins):.4f}")
    print("  (a quantization step is 0.3529 deg)")

Path(f"data/bench/s2-truth-{VARIANT.lower()}.json").write_text(
    json.dumps({"f64_right": f64_right, "f32_right": f32_right, "flips": verdicts}, indent=2)
)
print(f"wrote data/bench/s2-truth-{VARIANT.lower()}.json")
