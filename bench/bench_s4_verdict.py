"""S4, step 2: the four combinations judged by the arbiter, in verdict.

The judge is ``shade_pipeline.arbiter`` at **500 m**, fixed, for all four cubes
-- including the 250 m ones. Clipping the judge with the same parameter as the
defendant is the mistake S3 exists to stop repeating: a 250 m arbiter would
agree with a 250 m cube about a building at 300 m by construction, and both
would be wrong together.

Every figure twice: whole city and open sky. Under a crown the verdict is
decided by the opaque-canopy rule of ADR-002 and not by how far the sweep looks,
so mixing the two dilutes the measurement with a decision that has nothing to do
with what is being decided.

The breakdown by solar elevation is where the radius question actually lives: a
distant building can only matter when the sun is low enough for its shadow to
reach, and that is a handful of instants, not the average.
"""

import json
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import rasterio

from shade_core.artifacts import load_metadata
from shade_core.solar import sun_position
from shade_pipeline.arbiter import shade_bracket
from shade_pipeline.shade_raster import STATE_SUN, compute_state_raster
from shade_pipeline.tiles import bounds_wgs84, season_preset_instants

DIRS = {
    "exact500": Path("data/bench/montilla-test-s4-exact500/v1"),
    "geometric500": Path("data/bench/montilla-test-s4-geometric500/v1"),
    "exact250": Path("data/bench/montilla-test-s4-exact250/v1"),
    "geometric250": Path("data/bench/montilla-test-s4-geometric250/v1"),
}
ARBITER_DISTANCE_M = 500.0
OBSERVER_H = 1.6

data = np.load("data/bench/montilla-test-padded.npz")
DSM, DTM = data["dsm"], data["dtm"]
INNER = tuple(int(v) for v in data["inner"])
RES = float(data["resolution_m"][0])

with rasterio.open("data/cities/montilla-test/v1/canopy.tif") as src:
    CANOPY = src.read()[0] != 0
OPEN = ~CANOPY

metadata = load_metadata(DIRS["exact500"])
west, south, east, north = bounds_wgs84(metadata.crs, metadata.bbox)
center_lon, center_lat = (west + east) / 2.0, (south + north) / 2.0

wrong = dict.fromkeys(DIRS, 0)
wrong_open = dict.fromkeys(DIRS, 0)
missed = dict.fromkeys(DIRS, 0)
shaded = dict.fromkeys(DIRS, 0)
total = open_total = most_total = least_total = 0
rows = []

for when in season_preset_instants(ZoneInfo("Europe/Madrid")):
    sun = sun_position(center_lat, center_lon, when)
    if not sun.is_up:
        continue
    most, least = shade_bracket(
        DSM,
        DTM,
        INNER,
        sun,
        resolution_m=RES,
        max_distance_m=ARBITER_DISTANCE_M,
        observer_height_m=OBSERVER_H,
    )
    total += most.size
    open_total += int(OPEN.sum())
    most_total += int(most.sum())
    least_total += int(least.sum())
    row = {
        "when": when.isoformat(),
        "elevation_deg": round(sun.elevation_deg, 2),
        "azimuth_deg": round(sun.azimuth_deg, 2),
    }
    for name, path in DIRS.items():
        shade = compute_state_raster(path, sun) != STATE_SUN
        bad = shade != most
        wrong[name] += int(bad.sum())
        wrong_open[name] += int(bad[OPEN].sum())
        missed[name] += int((most & ~shade).sum())
        shaded[name] += int(shade.sum())
        row[name] = round(100.0 * float(bad[OPEN].mean()), 4)
        row[f"{name}_shaded"] = round(100.0 * float(shade.mean()), 4)
    rows.append(row)
    print(f"  {when.isoformat()} elev {sun.elevation_deg:5.1f}", flush=True)

timings = json.loads(Path("data/bench/s4-sweep.json").read_text())["variants"]

print(f"\n{len(rows)} instants, {total:,} pixel-instants")
print(f"arbiter bracket: {100.0 * most_total / total:.3f}% to {100.0 * least_total / total:.3f}%\n")
header = f"  {'variant':>13}  {'seconds':>8}  {'wrong (city)':>13}  {'wrong (open)':>13}  {'missed shade':>13}  {'shade':>8}"
print(header)
for name in DIRS:
    print(
        f"  {name:>13}  {timings[name]['seconds']:8.1f}  "
        f"{100.0 * wrong[name] / total:12.3f}%  {100.0 * wrong_open[name] / open_total:12.3f}%  "
        f"{100.0 * missed[name] / total:12.3f}%  {100.0 * shaded[name] / total:7.3f}%"
    )

print("\nby solar elevation, open sky -- where a far obstacle can still reach:")
print(f"  {'band':>14}  {'instants':>9}  " + "  ".join(f"{n:>13}" for n in DIRS))
for lo, hi, label in (
    (0.0, 10.0, "below 10 deg"),
    (10.0, 30.0, "10-30 deg"),
    (30.0, 90.0, "over 30 deg"),
):
    band = [r for r in rows if lo <= r["elevation_deg"] < hi]
    if not band:
        continue
    means = {n: float(np.mean([r[n] for r in band])) for n in DIRS}
    worst = {n: max(r[n] for r in band) for n in DIRS}
    print(
        f"  {label:>14}  {len(band):9d}  "
        + "  ".join(f"{means[n]:6.3f}/{worst[n]:6.3f}" for n in DIRS)
    )
print("  (mean / worst instant, in % of wrong verdicts)")

Path("data/bench/s4-verdict.json").write_text(
    json.dumps(
        {
            "instants": len(rows),
            "pixel_instants": total,
            "arbiter_most_pct": 100.0 * most_total / total,
            "arbiter_least_pct": 100.0 * least_total / total,
            "seconds": {n: timings[n]["seconds"] for n in DIRS},
            "wrong_pct": {n: 100.0 * wrong[n] / total for n in DIRS},
            "wrong_pct_open_sky": {n: 100.0 * wrong_open[n] / open_total for n in DIRS},
            "missed_shade_pct": {n: 100.0 * missed[n] / total for n in DIRS},
            "shaded_pct": {n: 100.0 * shaded[n] / total for n in DIRS},
            "per_instant": rows,
        },
        indent=2,
    )
)
print("\nwrote data/bench/s4-verdict.json")
