"""S3, step 5: the three conventions judged by exact ray traversal.

For each instant, walk the true ray (DDA) over the DSM read as 1x1 m columns
and decide sun/shade exactly: cell c blocks iff dsm[c] > observer_z +
tan(elev) * t_in(c), with t_in the ray's true entry distance into that cell.
No rounded offsets, no per-cell distance convention, no azimuth sectors.

Then count how often each cube agrees with it. The three carry the same
azimuthal discretization, so the comparison between them is fair; what is not
fair is comparing any of them against an oracle that shares their convention,
which is the whole reason this file exists.
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

DIRS = {
    "nominal": Path("data/bench/montilla-test-real/v1"),
    "centre": Path("data/bench/montilla-test-centre/v1"),
    "edge": Path("data/bench/montilla-test-edge/v1"),
    # Judged by an arbiter that shares its geometry, so its number is not
    # comparable with the other three: what is left in it is the azimuthal
    # discretization alone, which makes it the floor, not a contender.
    "dda": Path("data/bench/montilla-test-dda/v1"),
}
MAX_DISTANCE_M = 500.0
OBSERVER_H = 1.6

data = np.load("data/bench/montilla-test-padded.npz")
DSM = data["dsm"]
DTM = data["dtm"]
INNER = tuple(int(v) for v in data["inner"])
RES = float(data["resolution_m"][0])
ROWS = INNER[1] - INNER[0]
COLS = INNER[3] - INNER[2]
OBSERVER = DTM[INNER[0] : INNER[1], INNER[2] : INNER[3]].astype(np.float32) + OBSERVER_H


def dda_cells(azimuth_deg: float) -> list[tuple[int, int, float]]:
    """(d_row, d_col, entry distance) for every cell the ray crosses."""
    azimuth = math.radians(azimuth_deg)
    d_col, d_row = math.sin(azimuth), -math.cos(azimuth)

    def first_and_delta(direction: float) -> tuple[float, float, int]:
        if direction == 0.0:
            return math.inf, math.inf, 0
        return (0.5 * RES) / abs(direction), RES / abs(direction), 1 if direction > 0 else -1

    t_x, dt_x, step_col = first_and_delta(d_col)
    t_y, dt_y, step_row = first_and_delta(d_row)
    row = col = 0
    t = 0.0
    cells = []
    while True:
        if t_x < t_y:
            t, t_x = t_x, t_x + dt_x
            col += step_col
        else:
            t, t_y = t_y, t_y + dt_y
            row += step_row
        if t >= MAX_DISTANCE_M:
            return cells
        cells.append((row, col, t))


def exact_shade(azimuth_deg: float, elevation_deg: float) -> np.ndarray:
    """Sun blocked at every inner pixel, by exact traversal over columns."""
    tan_e = math.tan(math.radians(elevation_deg))
    blocked = np.zeros((ROWS, COLS), dtype=bool)
    for d_row, d_col, t_in in dda_cells(azimuth_deg):
        src = DSM[
            INNER[0] + d_row : INNER[0] + d_row + ROWS,
            INNER[2] + d_col : INNER[2] + d_col + COLS,
        ]
        if src.shape != (ROWS, COLS):
            continue  # ray left the padded stack
        np.logical_or(blocked, src > OBSERVER + np.float32(tan_e * t_in), out=blocked)
    return blocked


metadata = load_metadata(DIRS["nominal"])
west, south, east, north = bounds_wgs84(metadata.crs, metadata.bbox)
center_lon, center_lat = (west + east) / 2.0, (south + north) / 2.0

wrong = dict.fromkeys(DIRS, 0)
missed_shade = dict.fromkeys(DIRS, 0)  # truth says shade, cube says sun
false_shade = dict.fromkeys(DIRS, 0)
total = 0
exact_shaded = 0
rows = []

for when in season_preset_instants(ZoneInfo("Europe/Madrid")):
    sun = sun_position(center_lat, center_lon, when)
    if not sun.is_up:
        continue
    truth = exact_shade(sun.azimuth_deg, sun.elevation_deg)
    total += truth.size
    exact_shaded += int(truth.sum())
    row = {"when": when.isoformat(), "elevation_deg": round(sun.elevation_deg, 2)}
    for name, path in DIRS.items():
        shade = compute_state_raster(path, sun) != STATE_SUN
        bad = shade != truth
        wrong[name] += int(bad.sum())
        missed_shade[name] += int((truth & ~shade).sum())
        false_shade[name] += int((~truth & shade).sum())
        row[name] = round(100.0 * float(bad.mean()), 4)
    rows.append(row)
    print(
        f"  {when.isoformat()} elev {sun.elevation_deg:5.1f}  "
        + "  ".join(f"{n} {row[n]:6.3f}%" for n in DIRS),
        flush=True,
    )

print(
    f"\n{len(rows)} instants, {total:,} pixel-instants, "
    f"exact shade {100.0 * exact_shaded / total:.3f}%"
)
print("\nwrong verdicts against exact ray traversal:")
print(f"  {'convention':>10}  {'wrong':>9}  {'%':>8}  {'missed shade':>13}  {'false shade':>12}")
for name in DIRS:
    print(
        f"  {name:>10}  {wrong[name]:9,}  {100.0 * wrong[name] / total:7.3f}%  "
        f"{missed_shade[name]:13,}  {false_shade[name]:12,}"
    )
best = min(DIRS, key=lambda n: wrong[n])
print(f"\nclosest to the truth: {best}")

Path("data/bench/s3-arbiter-all.json").write_text(
    json.dumps(
        {
            "instants": len(rows),
            "pixel_instants": total,
            "exact_shaded_pct": 100.0 * exact_shaded / total,
            "wrong_pct": {n: 100.0 * wrong[n] / total for n in DIRS},
            "missed_shade": missed_shade,
            "false_shade": false_shade,
            "per_instant": rows,
        },
        indent=2,
    )
)
print("wrote data/bench/s3-arbiter.json")
