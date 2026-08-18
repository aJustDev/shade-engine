"""S4, follow-up 2: a fraction without a magnitude decides nothing.

"13.5% of pixels move a boundary" will be read in six months as "the timeline is
wrong in one pixel out of seven", which is probably not what it says. Three
numbers close it:

- **which** boundary moves: the first of the day, the last, or an interior one;
- **how much**, in median and p95 minutes;
- what fraction of the moved ones are the last transition of the day.

The distinction is the whole point. If what moves is the last edge by 5-15
minutes, the sentence to write is "the timeline is exact except in the last
quarter hour of daylight" and the matter is closed -- at 1.45 degrees of
elevation the sun is about seven minutes from setting, falling ~0.2 deg/min. If
an interior edge moves -- a midday shadow starting or ending somewhere else --
that is a different and worse thing.

Run over both radius comparisons, because they are the same question asked in
opposite directions:

  500 vs 950 m (the far field)   and   500 vs 250 m (the cut)
"""

import json
import random
from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import rasterio

from shade_core.artifacts import load_metadata, load_scene
from shade_core.shade import shade_timeline
from shade_pipeline.tiles import bounds_wgs84

PAIRS = {
    "far field (500 -> 950)": (
        Path("data/bench/montilla-test-far500/v1"),
        Path("data/bench/montilla-test-far950/v1"),
    ),
    "the cut (500 -> 250)": (
        Path("data/bench/montilla-test-s4-exact500/v1"),
        Path("data/bench/montilla-test-s4-exact250/v1"),
    ),
}
DAYS = {"winter solstice": date(2026, 12, 21), "summer solstice": date(2026, 6, 21)}
PIXELS = 600
STEP_MINUTES = 5
MADRID = ZoneInfo("Europe/Madrid")

metadata = load_metadata(Path("data/cities/montilla-test/v1"))
west, south, east, north = bounds_wgs84(metadata.crs, metadata.bbox)
center_lon, center_lat = (west + east) / 2.0, (south + north) / 2.0

with rasterio.open("data/cities/montilla-test/v1/horizon.tif") as src:
    affine = src.transform
    height, width = src.height, src.width

report: dict[str, dict] = {}

for label, (base_dir, other_dir) in PAIRS.items():
    scene_a = load_scene(base_dir)
    scene_b = load_scene(other_dir)
    report[label] = {}
    for day_label, day in DAYS.items():
        random.seed(20260818)
        where = {"first": 0, "last": 0, "interior": 0, "count_changed": 0}
        extra_where = {"first": 0, "last": 0, "interior": 0}
        extra_spans: list[float] = []
        shifts: list[float] = []
        last_shifts: list[float] = []
        compared = moved = 0

        for _ in range(PIXELS):
            row, col = random.randrange(height), random.randrange(width)
            x, y = affine * (col + 0.5, row + 0.5)
            try:
                a = shade_timeline(scene_a, x, y, center_lat, center_lon, day, MADRID, STEP_MINUTES)
                b = shade_timeline(scene_b, x, y, center_lat, center_lon, day, MADRID, STEP_MINUTES)
            except ValueError:
                continue
            compared += 1
            edges_a = [interval.start for interval in a[1:]]
            edges_b = [interval.start for interval in b[1:]]
            if len(edges_a) != len(edges_b):
                moved += 1
                where["count_changed"] += 1
                # "The number of intervals changed" sounds worse than it may be:
                # a 5-minute sliver appearing at dusk is the same event as a
                # boundary moving one step. Measure the odd interval's duration
                # and where it sits, instead of leaving the category opaque.
                longer, shorter = (a, b) if len(a) > len(b) else (b, a)
                spans = [
                    (interval.end - interval.start).total_seconds() / 60.0 for interval in longer
                ]
                # The extra interval is the shortest one not present in the other
                # timeline's shape; the shortest is the right proxy and is what
                # decides whether this is a sliver or a real segment.
                odd = min(spans)
                extra_spans.append(odd)
                position = spans.index(odd)
                if position == 0:
                    extra_where["first"] += 1
                elif position == len(spans) - 1:
                    extra_where["last"] += 1
                else:
                    extra_where["interior"] += 1
                continue
            if not edges_a:
                continue
            deltas = [
                abs((p - q).total_seconds()) / 60.0 for p, q in zip(edges_a, edges_b, strict=True)
            ]
            if max(deltas, default=0.0) == 0.0:
                continue
            moved += 1
            # Where does the largest move sit in the day's sequence?
            index = int(np.argmax(deltas))
            if index == len(deltas) - 1:
                where["last"] += 1
                last_shifts.append(deltas[index])
            elif index == 0:
                where["first"] += 1
            else:
                where["interior"] += 1
            shifts.append(deltas[index])

        entry = {
            "compared": compared,
            "moved": moved,
            "moved_pct": 100.0 * moved / max(compared, 1),
            "where": where,
            "median_min": float(np.median(shifts)) if shifts else 0.0,
            "p95_min": float(np.percentile(shifts, 95)) if shifts else 0.0,
            "max_min": float(max(shifts)) if shifts else 0.0,
            "extra_interval_where": extra_where,
            "extra_interval_median_min": float(np.median(extra_spans)) if extra_spans else 0.0,
            "extra_interval_p95_min": float(np.percentile(extra_spans, 95)) if extra_spans else 0.0,
            "extra_interval_max_min": float(max(extra_spans)) if extra_spans else 0.0,
            "last_fraction": (
                where["last"] / (where["first"] + where["last"] + where["interior"])
                if (where["first"] + where["last"] + where["interior"])
                else 0.0
            ),
        }
        report[label][day_label] = entry
        print(
            f"{label:24s} {day_label:16s}: {moved}/{compared} move "
            f"({entry['moved_pct']:.1f}%) | last {where['last']} first {where['first']} "
            f"interior {where['interior']} count-changed {where['count_changed']} | "
            f"median {entry['median_min']:.0f} p95 {entry['p95_min']:.0f} "
            f"max {entry['max_min']:.0f} min",
            flush=True,
        )
        if extra_spans:
            print(
                f"{'':24s} {'':16s}  the extra/missing interval: "
                f"median {np.median(extra_spans):.0f} p95 {np.percentile(extra_spans, 95):.0f} "
                f"max {max(extra_spans):.0f} min | at first {extra_where['first']} "
                f"last {extra_where['last']} interior {extra_where['interior']}",
                flush=True,
            )

print("\nwhat fraction of the moved boundaries is the last transition of the day:")
for label, days in report.items():
    for day_label, entry in days.items():
        print(f"  {label:24s} {day_label:16s}  {100.0 * entry['last_fraction']:5.1f}%")

Path("data/bench/s4-edges.json").write_text(json.dumps(report, indent=2))
print("\nwrote data/bench/s4-edges.json")
