"""S4, step 1: is cutting the radius really free where it costs?

The claim the ADR wants to make is that the radius lever is safe because its
whole bill is paid with the sun low, "where almost everything is already in
shade anyway". That is an intuition leaning on a figure from another document.
This turns it into a number, two ways:

1. **Direction of the flips.** Losing a distant blocker can only ever remove
   shade, so every flip should be shade -> sun. What matters is how many, and
   whether they land on pixels that some nearer sector already shades -- those
   are free by definition.
2. **What it does to a timeline.** shade_timeline is where "how long does this
   shade last?" lives, which is one of the three things the product promises.
   Its boundaries are accurate to one step (5 min) by its own docstring, so the
   question is not whether a boundary moves but whether it moves **more than one
   step**. Sampled over the pixels that actually flip, which is the worst case.
"""

import json
import random
from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import rasterio

from shade_core.artifacts import load_metadata, load_scene
from shade_core.shade import ShadeState, shade_timeline
from shade_core.solar import sun_position
from shade_pipeline.shade_raster import STATE_SUN, compute_state_raster
from shade_pipeline.tiles import bounds_wgs84, season_preset_instants

FULL = Path("data/bench/montilla-test-s4-exact500/v1")
CUT = Path("data/bench/montilla-test-s4-exact250/v1")
LOW_SUN_DEG = 10.0
TIMELINE_PIXELS = 400
STEP_MINUTES = 5
MADRID = ZoneInfo("Europe/Madrid")

metadata = load_metadata(FULL)
west, south, east, north = bounds_wgs84(metadata.crs, metadata.bbox)
center_lon, center_lat = (west + east) / 2.0, (south + north) / 2.0

with rasterio.open(FULL / "canopy.tif") as src:
    OPEN = ~(src.read()[0] != 0)

# --- 1. direction of the flips, on the low-sun instants -------------------------

lost = gained = total_flips = 0
lost_open = 0
per_instant = []
low_instants = []

for when in season_preset_instants(MADRID):
    sun = sun_position(center_lat, center_lon, when)
    if not sun.is_up or sun.elevation_deg >= LOW_SUN_DEG:
        continue
    low_instants.append(when)
    full = compute_state_raster(FULL, sun) != STATE_SUN
    cut = compute_state_raster(CUT, sun) != STATE_SUN
    shade_to_sun = int((full & ~cut).sum())
    sun_to_shade = int((~full & cut).sum())
    lost += shade_to_sun
    gained += sun_to_shade
    lost_open += int((full & ~cut)[OPEN].sum())
    total_flips += shade_to_sun + sun_to_shade
    per_instant.append(
        {
            "when": when.isoformat(),
            "elevation_deg": round(sun.elevation_deg, 2),
            "shade_to_sun": shade_to_sun,
            "sun_to_shade": sun_to_shade,
            "shaded_full_pct": round(100.0 * float(full.mean()), 3),
            "flip_pct": round(100.0 * (shade_to_sun + sun_to_shade) / full.size, 4),
        }
    )
    print(
        f"  {when.isoformat()} elev {sun.elevation_deg:5.2f}: "
        f"shade->sun {shade_to_sun:7,}  sun->shade {sun_to_shade:6,}  "
        f"(city was {100.0 * full.mean():.1f}% shaded)",
        flush=True,
    )

print(f"\n{len(low_instants)} instants below {LOW_SUN_DEG:.0f} deg")
print(f"  shade -> sun: {lost:,} ({100.0 * lost / max(total_flips, 1):.1f}% of flips)")
print(f"  sun -> shade: {gained:,}")
print(f"  a shorter radius can only remove shade, so sun->shade should be ~0")

# --- 2. what it does to a day's timeline ----------------------------------------
#
# The pixels that flip at least once are the worst case; a random pixel mostly
# does not care. Take them from the instant with the most flips.

worst = max(per_instant, key=lambda row: row["shade_to_sun"] + row["sun_to_shade"])
worst_when = next(w for w in low_instants if w.isoformat() == worst["when"])
sun = sun_position(center_lat, center_lon, worst_when)
flipped = np.argwhere(
    (compute_state_raster(FULL, sun) != STATE_SUN) != (compute_state_raster(CUT, sun) != STATE_SUN)
)
print(f"\nworst instant {worst['when']}: {len(flipped):,} flipped pixels")

scene_full = load_scene(FULL)
scene_cut = load_scene(CUT)
transform = metadata.transform if hasattr(metadata, "transform") else None

with rasterio.open(FULL / "horizon.tif") as src:
    affine = src.transform

random.seed(20260818)
sample = random.sample(range(len(flipped)), min(TIMELINE_PIXELS, len(flipped)))
day = date(2026, 12, 21)  # winter solstice: the longest shadows of the year

# The denominator. "176 of 400" is the worst case by construction -- those are
# pixels chosen because they flip. What fraction of the city is ever affected at
# all, across every low-sun instant, is the number that belongs next to it.
ever = np.zeros(flipped.shape[0] and OPEN.shape, dtype=bool)
for when in low_instants:
    sun_low = sun_position(center_lat, center_lon, when)
    ever |= (compute_state_raster(FULL, sun_low) != STATE_SUN) != (
        compute_state_raster(CUT, sun_low) != STATE_SUN
    )
ever_pct = 100.0 * float(ever.mean())
print(f"pixels that flip at any low-sun instant: {int(ever.sum()):,} ({ever_pct:.3f}% of the city)")

moved_any = moved_over_step = compared = 0
worst_shift_minutes = 0.0
shifts = []

for index in sample:
    row, col = (int(v) for v in flipped[index])
    x, y = affine * (col + 0.5, row + 0.5)
    try:
        a = shade_timeline(scene_full, x, y, center_lat, center_lon, day, MADRID, STEP_MINUTES)
        b = shade_timeline(scene_cut, x, y, center_lat, center_lon, day, MADRID, STEP_MINUTES)
    except ValueError:
        continue  # outside the grid
    compared += 1
    # Boundaries are the interval starts after the first; compare them pairwise
    # where both timelines have one, and count a differing count as a move.
    edges_a = [interval.start for interval in a[1:]]
    edges_b = [interval.start for interval in b[1:]]
    if len(edges_a) != len(edges_b):
        moved_any += 1
        moved_over_step += 1
        shifts.append(float("inf"))
        continue
    for edge_a, edge_b in zip(edges_a, edges_b, strict=True):
        delta = abs((edge_a - edge_b).total_seconds()) / 60.0
        if delta > 0:
            moved_any += 1
            shifts.append(delta)
            worst_shift_minutes = max(worst_shift_minutes, delta)
            if delta > STEP_MINUTES:
                moved_over_step += 1
            break

# And the same timeline comparison over pixels picked at random, which is what
# an arbitrary user query looks like.
random_moved = random_over_step = random_compared = 0
rows_all, cols_all = OPEN.shape
for _ in range(TIMELINE_PIXELS):
    row = random.randrange(rows_all)
    col = random.randrange(cols_all)
    x, y = affine * (col + 0.5, row + 0.5)
    try:
        a = shade_timeline(scene_full, x, y, center_lat, center_lon, day, MADRID, STEP_MINUTES)
        b = shade_timeline(scene_cut, x, y, center_lat, center_lon, day, MADRID, STEP_MINUTES)
    except ValueError:
        continue
    random_compared += 1
    edges_a = [interval.start for interval in a[1:]]
    edges_b = [interval.start for interval in b[1:]]
    if len(edges_a) != len(edges_b):
        random_moved += 1
        random_over_step += 1
        continue
    for edge_a, edge_b in zip(edges_a, edges_b, strict=True):
        delta = abs((edge_a - edge_b).total_seconds()) / 60.0
        if delta > 0:
            random_moved += 1
            if delta > STEP_MINUTES:
                random_over_step += 1
            break

finite = [s for s in shifts if s != float("inf")]
print(f"\ntimelines compared: {compared} (winter solstice, {STEP_MINUTES} min step)")
print(f"  with any boundary moved:          {moved_any}")
print(f"  with a boundary moved > one step: {moved_over_step}")
if finite:
    print(f"  worst finite shift: {max(finite):.0f} min; median {np.median(finite):.0f} min")
print(f"  timelines with a different number of intervals: {shifts.count(float('inf'))}")
print(f"\nsame comparison over {random_compared} pixels picked at random:")
print(f"  with any boundary moved:          {random_moved}")
print(f"  with a boundary moved > one step: {random_over_step}")

Path("data/bench/s4-lowsun.json").write_text(
    json.dumps(
        {
            "low_sun_instants": len(low_instants),
            "shade_to_sun": lost,
            "sun_to_shade": gained,
            "shade_to_sun_open_sky": lost_open,
            "per_instant": per_instant,
            "timelines_compared": compared,
            "timelines_moved": moved_any,
            "timelines_moved_over_one_step": moved_over_step,
            "worst_shift_minutes": worst_shift_minutes,
            "interval_count_changed": shifts.count(float("inf")),
            "city_pct_ever_flipping": ever_pct,
            "random_compared": random_compared,
            "random_moved": random_moved,
            "random_moved_over_one_step": random_over_step,
        },
        indent=2,
    )
)
print("\nwrote data/bench/s4-lowsun.json")
