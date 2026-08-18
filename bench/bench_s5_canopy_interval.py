"""S5 entry gate: what is a seasonal canopy worth, in hours of shade per day?

S5 wants to stop treating every crown as opaque twelve months a year. Before
building that, one number decides whether the session exists at all -- and it
must not depend on Cordoba's tree inventory, which is the only one of the five
cities that has one.

**Three scenes, because the first two answer the wrong question.** The engine
ships two cubes, and the obvious bracket is opaque-against-felled -- but that
gap mixes two different defects, and only one of them is seasonal:

- **opaque**: ``horizon.tif`` plus the canopy mask. Under a crown ``is_shaded``
  returns SHADE whenever the sun is up, full stop. This is what the product
  promises today, mask included (``compute_state_raster`` applies it too).
- **geometric**: the same cube with the mask off. The crown is still there as a
  solid obstacle in the skyline -- it just stops overriding the geometry.
- **felled**: ``horizon_noveg.tif`` with the mask off, the same skyline over a
  surface with vegetation lowered to the terrain.

**opaque - geometric is the opaque-canopy rule**, and it is not seasonal: it
costs the same in June as in December. **geometric - felled is what the
vegetation is worth as geometry**, and that is the one a deciduous winter
modulates -- so it is the number that decides whether S5 exists. Reporting the
outer gap alone would have credited the rule's cost to seasonality.

**Measured as hours, not as pixels.** This is what S4 left written for S5: an
instantaneous metric can call harmless something that, integrated over the day,
is a product defect -- measured factor 330 in the far field. "How long is this
bench in the shade" is the promise, so hours per day is the unit. Both metrics
come out of the same sweep here, so the factor between them is measured too.

**What this does NOT measure.** Which crowns are deciduous. The bracket assumes
100% deciduous, i.e. its upper end; with a fraction f of deciduous crowns the
error scales roughly with f. Montilla has no inventory, and that is precisely
why the gate is built as a bracket instead of as an estimate.

Runs over the seven rungs of the 2026 declination ladder, each of which carries
the calendar days it covers, so the daily figures integrate into a year.
"""

import json
import random
from dataclasses import replace
from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import rasterio

from shade_core.artifacts import load_metadata, load_scene
from shade_core.shade import ShadeState, is_shaded
from shade_pipeline.tiles import LADDER_PRESET_2026, bounds_wgs84, declination_ladder

BASE = Path("data/bench/montilla-test-far500/v1")
"""The current engine (verified: it moves exactly the 1,409,468 cells of S4
against the S3 reference, sectors 24 and 56 only). The published
montilla-test still carries the S1 kernel."""

PIXELS = 400
STEP_MINUTES = 5
MADRID = ZoneInfo("Europe/Madrid")
LEAFLESS_MONTHS = (12, 1, 2, 3)
"""Months assumed leafless for the year-integration. A declared assumption,
not a measurement: Montilla has no phenology data. Sensitivity is linear."""

scene_opaque = load_scene(BASE)
assert scene_opaque.horizon_noveg is not None, "this measurement needs horizon_noveg.tif"
assert scene_opaque.canopy is not None
scene_geometric = replace(scene_opaque, canopy=None)
scene_felled = replace(
    scene_opaque,
    canopy=None,
    horizon=scene_opaque.horizon_noveg,
    horizon_noveg=None,
)

metadata = load_metadata(BASE)
west, south, east, north = bounds_wgs84(metadata.crs, metadata.bbox)
center_lon, center_lat = (west + east) / 2.0, (south + north) / 2.0

with rasterio.open(BASE / "horizon.tif") as src:
    affine = src.transform
    height, width = src.height, src.width

canopy = scene_opaque.canopy
canopy_pct = 100.0 * float(canopy.mean())
print(f"montilla-test: {height}x{width}, {canopy.sum():,} pixels under canopy ({canopy_pct:.3f}%)")

# How much of the city the two cubes disagree about at all -- pixels not under a
# crown but reached by one's cast shadow. Under-canopy is where the mask acts;
# this is where the skyline does.
scale_deg = 90.0 / 255.0
reached = np.zeros((height, width), dtype=bool)
worst_drop_deg = 0.0
with (
    rasterio.open(BASE / "horizon.tif") as opaque_src,
    rasterio.open(BASE / "horizon_noveg.tif") as felled_src,
):
    for band in range(1, opaque_src.count + 1):
        delta = opaque_src.read([band])[0].astype(np.int16) - felled_src.read([band])[0].astype(
            np.int16
        )
        assert delta.min() >= 0, "felling a crown cannot raise the horizon"
        reached |= delta > 0
        worst_drop_deg = max(worst_drop_deg, float(delta.max()) * scale_deg)
reached_pct = 100.0 * float(reached.mean())
outside_canopy_pct = 100.0 * float((reached & ~canopy).mean())
print(
    f"pixels whose skyline changes when crowns fall: {reached_pct:.3f}% "
    f"({outside_canopy_pct:.3f}% of the city is reached without being under a crown); "
    f"worst single drop {worst_drop_deg:.2f} deg"
)

# --- the samples -----------------------------------------------------------

random.seed(20260818)
under = np.argwhere(canopy)
under_sample = [
    (int(under[i][0]), int(under[i][1]))
    for i in random.sample(range(len(under)), min(PIXELS, len(under)))
]
random_sample = [(random.randrange(height), random.randrange(width)) for _ in range(PIXELS)]
SAMPLES = {"under canopy": under_sample, "random pixel": random_sample}


def hours_of_shade(scene, pixels, suns):
    """Shade hours per pixel for one day, plus the raw per-sample verdicts.

    Counting samples rather than merging intervals is the same verdict code
    (``is_shaded``) with a cheaper aggregation, and it keeps the instantaneous
    metric available for free: hours = samples in shade x step / 60.
    """
    hours = []
    verdicts = []
    for row, col in pixels:
        x, y = affine * (col + 0.5, row + 0.5)
        shaded = [is_shaded(scene, x, y, sun).state is ShadeState.SHADE for sun in suns]
        verdicts.append(shaded)
        hours.append(sum(shaded) * STEP_MINUTES / 60.0)
    return np.array(hours), np.array(verdicts)


# --- the ladder ------------------------------------------------------------

from shade_core.solar import sun_positions_for_day  # noqa: E402

covers = {entry["date"]: entry for entry in declination_ladder()}
report: dict[str, dict] = {}

header = (
    f"{'rung':>12} {'sample':>13} {'light':>6} {'opaque':>7} {'geom':>6} {'felled':>7} "
    f"{'RULE':>7} {'VEG med':>8} {'p95':>6} {'max':>6} {'>30m':>6} {'instant':>8}"
)

for day_str, _first, _last in LADDER_PRESET_2026:
    day = date.fromisoformat(day_str)
    samples = sun_positions_for_day(center_lat, center_lon, day, MADRID, STEP_MINUTES)
    suns = [sun for _when, sun in samples if sun.is_up]
    daylight_h = len(suns) * STEP_MINUTES / 60.0
    report[day_str] = {"daylight_hours": daylight_h, "covers": covers[day_str]["covers"]}
    if day_str == LADDER_PRESET_2026[0][0]:
        print(f"\n{header}")
    for label, pixels in SAMPLES.items():
        opaque_h, opaque_v = hours_of_shade(scene_opaque, pixels, suns)
        geometric_h, geometric_v = hours_of_shade(scene_geometric, pixels, suns)
        felled_h, felled_v = hours_of_shade(scene_felled, pixels, suns)
        rule = opaque_h - geometric_h
        veg = geometric_h - felled_h
        assert rule.min() >= -1e-9, "dropping the mask cannot add shade"
        assert veg.min() >= -1e-9, "felling crowns cannot add shade"
        entry = {
            "opaque_median_h": float(np.median(opaque_h)),
            "geometric_median_h": float(np.median(geometric_h)),
            "felled_median_h": float(np.median(felled_h)),
            "rule_median_h": float(np.median(rule)),
            "rule_mean_h": float(rule.mean()),
            "gap_median_h": float(np.median(veg)),
            "gap_p95_h": float(np.percentile(veg, 95)),
            "gap_max_h": float(veg.max()),
            "gap_mean_h": float(veg.mean()),
            "pct_over_30min": 100.0 * float((veg > 0.5).mean()),
            "pct_over_1h": 100.0 * float((veg > 1.0).mean()),
            "instant_pct": 100.0 * float((geometric_v != felled_v).mean()),
            "instant_pct_rule": 100.0 * float((opaque_v != geometric_v).mean()),
        }
        report[day_str][label] = entry
        print(
            f"{day_str:>12} {label:>13} {daylight_h:5.1f}h {entry['opaque_median_h']:6.1f}h "
            f"{entry['geometric_median_h']:5.1f}h {entry['felled_median_h']:6.1f}h "
            f"{entry['rule_median_h']:6.2f}h {entry['gap_median_h']:7.2f}h "
            f"{entry['gap_p95_h']:5.2f} {entry['gap_max_h']:5.2f} "
            f"{entry['pct_over_30min']:5.1f}% {entry['instant_pct']:7.2f}%",
            flush=True,
        )

# --- what it adds up to ----------------------------------------------------


def days_covered(entry):
    total = 0
    for start, end in entry["covers"]:
        total += (date.fromisoformat(end) - date.fromisoformat(start)).days + 1
    return total


def leafless_days(entry):
    total = 0
    for start, end in entry["covers"]:
        first, last = date.fromisoformat(start), date.fromisoformat(end)
        for offset in range((last - first).days + 1):
            if (first + __import__("datetime").timedelta(days=offset)).month in LEAFLESS_MONTHS:
                total += 1
    return total


print("\nintegrated over the year, mean hours per pixel:")
summary = {}
for label in SAMPLES:
    veg_year = veg_leafless = rule_year = 0.0
    for day_str, entry in report.items():
        rung = covers[day_str]
        veg_year += entry[label]["gap_mean_h"] * days_covered(rung)
        veg_leafless += entry[label]["gap_mean_h"] * leafless_days(rung)
        rule_year += entry[label]["rule_mean_h"] * days_covered(rung)
    summary[label] = {
        "vegetation_hours_per_year": veg_year,
        "vegetation_hours_in_leafless_months": veg_leafless,
        "rule_hours_per_year": rule_year,
    }
    print(
        f"  {label:>13}: {veg_year:7.1f} h/year of shade is the vegetation as geometry, "
        f"of which {veg_leafless:6.1f} h fall in {LEAFLESS_MONTHS} "
        f"({100.0 * veg_leafless / veg_year if veg_year else 0:.1f}%); "
        f"the opaque-canopy rule adds {rule_year:.1f} h/year on top, all year round"
    )

Path("data/bench/s5-canopy-interval.json").write_text(
    json.dumps(
        {
            "base": str(BASE),
            "pixels_per_sample": PIXELS,
            "step_minutes": STEP_MINUTES,
            "canopy_pct": canopy_pct,
            "skyline_changed_pct": reached_pct,
            "skyline_changed_outside_canopy_pct": outside_canopy_pct,
            "worst_horizon_drop_deg": worst_drop_deg,
            "leafless_months": list(LEAFLESS_MONTHS),
            "rungs": report,
            "year": summary,
        },
        indent=2,
    )
)
print("\nwrote data/bench/s5-canopy-interval.json")
