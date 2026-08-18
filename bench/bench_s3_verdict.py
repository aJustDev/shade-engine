"""S3, step 3: what each convention does to the verdict the product ships.

The three cubes through the real compute_state_raster, over the 83 instants of
the declination ladder. Reports how far apart the conventions are (the gate:
0.3-0.6% would make this comparable to the whole azimuthal error) and how much
shade each one claims.
"""

import json
from itertools import combinations
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
}

metadata = load_metadata(DIRS["nominal"])
west, south, east, north = bounds_wgs84(metadata.crs, metadata.bbox)
center_lon, center_lat = (west + east) / 2.0, (south + north) / 2.0

pairs = list(combinations(DIRS, 2))
disagree = dict.fromkeys(pairs, 0)
shaded_px = dict.fromkeys(DIRS, 0)
per_instant = []
total_px = 0

for when in season_preset_instants(ZoneInfo("Europe/Madrid")):
    sun = sun_position(center_lat, center_lon, when)
    if not sun.is_up:
        continue
    states = {name: compute_state_raster(path, sun) for name, path in DIRS.items()}
    shade = {name: state != STATE_SUN for name, state in states.items()}
    total_px += next(iter(shade.values())).size
    row = {"when": when.isoformat(), "elevation_deg": round(sun.elevation_deg, 2)}
    for name in DIRS:
        shaded_px[name] += int(shade[name].sum())
        row[f"shaded_{name}"] = int(shade[name].sum())
    for a, b in pairs:
        flips = int((shade[a] != shade[b]).sum())
        disagree[(a, b)] += flips
        row[f"{a}_vs_{b}"] = flips
    per_instant.append(row)

print(f"{len(per_instant)} instants, {total_px:,} pixel-instants\n")
print("shade claimed by each convention:")
for name in DIRS:
    print(f"  {name:8s} {100.0 * shaded_px[name] / total_px:6.3f}% of pixel-instants")
print("\nverdict disagreement between conventions:")
for a, b in pairs:
    n = disagree[(a, b)]
    worst = max(per_instant, key=lambda r: r[f"{a}_vs_{b}"])
    print(
        f"  {a:8s} vs {b:8s}: {n:9,} ({100.0 * n / total_px:.3f}% of pixel-instants), "
        f"worst instant {worst[f'{a}_vs_{b}'] / (total_px / len(per_instant)) * 100:.3f}% "
        f"at {worst['when']}"
    )

Path("data/bench/s3-verdict.json").write_text(
    json.dumps(
        {
            "instants": len(per_instant),
            "pixel_instants": total_px,
            "shaded_pct": {k: 100.0 * v / total_px for k, v in shaded_px.items()},
            "disagreement_pct": {
                f"{a}_vs_{b}": 100.0 * v / total_px for (a, b), v in disagree.items()
            },
            "per_instant": per_instant,
        },
        indent=2,
    )
)
print("\nwrote data/bench/s3-verdict.json")
