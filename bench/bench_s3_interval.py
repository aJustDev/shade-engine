"""S3, step 6: the arbiter as an interval, and where the residual lives.

Three things the auditor asked for before the ADR:

1. The arbiter stops being a number and becomes a bracket. The DDA gives the
   exit distance in the same closed form as the entry: entry is the MOST shade
   a solid-column reading can justify, exit the LEAST. Any real obstacle -- a
   crown that transmits, a pierced parapet, a half-occupied cell -- sits
   inside. That bracket is what S7 gets to falsify in the street.
2. The two defects separated by ablation, one pass each: coverage fixed with
   the old convention (dda_centre), convention fixed with the old coverage
   (nominal_entry).
3. Where the residual of the DDA cube lives: if the excess concentrates on
   azimuths that fall mid-sector, it is the azimuthal interpolation smearing
   edges and it is explained. If it is spread out, something else is going on
   and it must not be frozen into an artifact.
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
    "nominal_entry": Path("data/bench/montilla-test-nominal_entry/v1"),
    "dda_centre": Path("data/bench/montilla-test-dda_centre/v1"),
    "dda": Path("data/bench/montilla-test-dda/v1"),
}
MAX_DISTANCE_M = 500.0
OBSERVER_H = 1.6
SECTORS = 64

data = np.load("data/bench/montilla-test-padded.npz")
DSM, DTM = data["dsm"], data["dtm"]
INNER = tuple(int(v) for v in data["inner"])
RES = float(data["resolution_m"][0])
ROWS, COLS = INNER[1] - INNER[0], INNER[3] - INNER[2]
OBSERVER = DTM[INNER[0] : INNER[1], INNER[2] : INNER[3]].astype(np.float32) + OBSERVER_H

# The arbiter is geometry; compute_state_raster also applies the opaque-canopy
# rule of ADR-002 (under a crown is shade whenever the sun is up), which no ray
# trace can reproduce. Measured: 88% of the DDA cube's excess shade sits there.
# So every figure is reported twice, and the open-sky one is the geometric read.
import rasterio as _rio

with _rio.open("data/cities/montilla-test/v1/canopy.tif") as _src:
    CANOPY = _src.read()[0] != 0
OPEN = ~CANOPY


def dda_cells(azimuth_deg: float) -> list[tuple[int, int, float, float]]:
    """(d_row, d_col, entry, exit) for every cell the ray crosses."""
    azimuth = math.radians(azimuth_deg)
    d_col, d_row = math.sin(azimuth), -math.cos(azimuth)

    def first_and_delta(direction: float) -> tuple[float, float, int]:
        if direction == 0.0:
            return math.inf, math.inf, 0
        return (0.5 * RES) / abs(direction), RES / abs(direction), 1 if direction > 0 else -1

    t_x, dt_x, step_col = first_and_delta(d_col)
    t_y, dt_y, step_row = first_and_delta(d_row)
    row = col = 0
    raw = []
    while True:
        if t_x < t_y:
            t, t_x = t_x, t_x + dt_x
            col += step_col
        else:
            t, t_y = t_y, t_y + dt_y
            row += step_row
        if t >= MAX_DISTANCE_M:
            break
        raw.append((row, col, t))
    # A cell's exit is the next cell's entry; the last one exits at the radius.
    return [
        (r, c, t, raw[i + 1][2] if i + 1 < len(raw) else MAX_DISTANCE_M)
        for i, (r, c, t) in enumerate(raw)
    ]


def exact_shade(azimuth_deg: float, elevation_deg: float) -> tuple[np.ndarray, np.ndarray]:
    """(most shade, least shade) a solid-column reading can justify."""
    tan_e = math.tan(math.radians(elevation_deg))
    most = np.zeros((ROWS, COLS), dtype=bool)
    least = np.zeros((ROWS, COLS), dtype=bool)
    for d_row, d_col, t_in, t_out in dda_cells(azimuth_deg):
        src = DSM[
            INNER[0] + d_row : INNER[0] + d_row + ROWS,
            INNER[2] + d_col : INNER[2] + d_col + COLS,
        ]
        if src.shape != (ROWS, COLS):
            continue
        np.logical_or(most, src > OBSERVER + np.float32(tan_e * t_in), out=most)
        np.logical_or(least, src > OBSERVER + np.float32(tan_e * t_out), out=least)
    return most, least


metadata = load_metadata(DIRS["nominal"])
west, south, east, north = bounds_wgs84(metadata.crs, metadata.bbox)
center_lon, center_lat = (west + east) / 2.0, (south + north) / 2.0

wrong = dict.fromkeys(DIRS, 0)
wrong_open = dict.fromkeys(DIRS, 0)
shaded = dict.fromkeys(DIRS, 0)
total = most_total = least_total = open_total = 0
rows = []

for when in season_preset_instants(ZoneInfo("Europe/Madrid")):
    sun = sun_position(center_lat, center_lon, when)
    if not sun.is_up:
        continue
    most, least = exact_shade(sun.azimuth_deg, sun.elevation_deg)
    total += most.size
    open_total += int(OPEN.sum())
    most_total += int(most.sum())
    least_total += int(least.sum())
    # How far this azimuth falls from a sector line, in units of a sector.
    phase = abs(((sun.azimuth_deg % (360.0 / SECTORS)) / (360.0 / SECTORS)) - 0.5)
    phase = 0.5 - phase  # 0 = on a grid line, 0.5 = mid-sector
    row = {
        "when": when.isoformat(),
        "elevation_deg": round(sun.elevation_deg, 2),
        "azimuth_phase": round(phase, 4),
        "most_shade_pct": round(100.0 * float(most.mean()), 4),
        "least_shade_pct": round(100.0 * float(least.mean()), 4),
    }
    for name, path in DIRS.items():
        shade = compute_state_raster(path, sun) != STATE_SUN
        wrong[name] += int((shade != most).sum())
        wrong_open[name] += int((shade != most)[OPEN].sum())
        shaded[name] += int(shade.sum())
        row[name] = round(100.0 * float((shade != most).mean()), 4)
        row[f"{name}_shaded"] = round(100.0 * float(shade.mean()), 4)
    rows.append(row)

print(f"{len(rows)} instants, {total:,} pixel-instants")
print(f"\nthe bracket a solid-column reading allows:")
print(f"  most shade (entry distance): {100.0 * most_total / total:.3f}%")
print(f"  least shade (exit distance): {100.0 * least_total / total:.3f}%")
print(f"  width of the bracket:        {100.0 * (most_total - least_total) / total:.3f} points")

print("\nwrong verdicts against the MOST-shade edge of the bracket:")
print(f"  {'variant':>14}  {'cells':>12}  {'distance':>10}  {'wrong':>8}  {'open sky':>9}  {'shade':>8}")
labels = {
    "nominal": ("nominal", "nominal"),
    "centre": ("nominal", "centre"),
    "edge": ("nominal", "near edge"),
    "nominal_entry": ("nominal", "entry"),
    "dda_centre": ("ray", "centre"),
    "dda": ("ray", "entry"),
}
for name in DIRS:
    cells, dist = labels[name]
    print(
        f"  {name:>14}  {cells:>12}  {dist:>10}  {100.0 * wrong[name] / total:7.3f}%  "
        f"{100.0 * wrong_open[name] / open_total:9.3f}%  {100.0 * shaded[name] / total:7.3f}%"
    )

print("\nablation, in points of wrong verdict (open sky only, the geometric read):")
base = 100.0 * wrong_open["nominal"] / open_total
wrong, total = wrong_open, open_total
print(f"  today                              {base:.3f}%")
print(
    f"  fixing only the convention         {100.0 * wrong['nominal_entry'] / total:.3f}%"
    f"  ({base - 100.0 * wrong['nominal_entry'] / total:+.3f})"
)
print(
    f"  fixing only the coverage           {100.0 * wrong['dda_centre'] / total:.3f}%"
    f"  ({base - 100.0 * wrong['dda_centre'] / total:+.3f})"
)
print(
    f"  fixing both (the proposal)         {100.0 * wrong['dda'] / total:.3f}%"
    f"  ({base - 100.0 * wrong['dda'] / total:+.3f})"
)

print("\nwhere the DDA cube's excess shade lives, by azimuth phase:")
print(f"  {'phase':>12}  {'instants':>9}  {'excess over truth':>18}  {'wrong':>8}")
for lo, hi, label in (
    (0.0, 0.15, "on a line"),
    (0.15, 0.35, "middling"),
    (0.35, 0.51, "mid-sector"),
):
    band = [r for r in rows if lo <= r["azimuth_phase"] < hi]
    if not band:
        continue
    excess = np.mean([r["dda_shaded"] - r["most_shade_pct"] for r in band])
    err = np.mean([r["dda"] for r in band])
    print(f"  {label:>12}  {len(band):9d}  {excess:+17.3f}  {err:7.3f}%")

Path("data/bench/s3-interval-open.json").write_text(
    json.dumps(
        {
            "pixel_instants": total,
            "most_shade_pct": 100.0 * most_total / total,
            "least_shade_pct": 100.0 * least_total / total,
            "wrong_pct_open_sky": {n: 100.0 * wrong_open[n] / open_total for n in DIRS},
            "shaded_pct": {n: 100.0 * shaded[n] / total for n in DIRS},
            "per_instant": rows,
        },
        indent=2,
    )
)
print("\nwrote data/bench/s3-interval.json")
