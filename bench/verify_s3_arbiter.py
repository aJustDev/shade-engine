"""S3 verification: the shipped arbiter reproduces the bench figures.

Imports shade_bracket from the module instead of carrying its own copy, which
is the point of the module existing at all.
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

CUBE = Path("data/bench/montilla-test-s3/v1")
data = np.load("data/bench/montilla-test-padded.npz")
DSM, DTM = data["dsm"], data["dtm"]
INNER = tuple(int(v) for v in data["inner"])

with rasterio.open("data/cities/montilla-test/v1/canopy.tif") as src:
    OPEN = src.read()[0] == 0

metadata = load_metadata(CUBE)
west, south, east, north = bounds_wgs84(metadata.crs, metadata.bbox)
center_lon, center_lat = (west + east) / 2.0, (south + north) / 2.0

most_total = least_total = wrong_open = total = open_total = 0
for when in season_preset_instants(ZoneInfo("Europe/Madrid")):
    sun = sun_position(center_lat, center_lon, when)
    if not sun.is_up:
        continue
    most, least = shade_bracket(DSM, DTM, INNER, sun, max_distance_m=500.0)
    shade = compute_state_raster(CUBE, sun) != STATE_SUN
    most_total += int(most.sum())
    least_total += int(least.sum())
    wrong_open += int((shade != most)[OPEN].sum())
    total += most.size
    open_total += int(OPEN.sum())

print(f"most shade  {100.0 * most_total / total:.3f}%  (bench: 53.472%)")
print(f"least shade {100.0 * least_total / total:.3f}%  (bench: 50.622%)")
print(f"bracket     {100.0 * (most_total - least_total) / total:.3f} points  (bench: 2.850)")
print(f"wrong, open sky {100.0 * wrong_open / open_total:.3f}%  (bench: 0.714%)")

Path("data/bench/s3-verify.json").write_text(
    json.dumps(
        {
            "most_shade_pct": 100.0 * most_total / total,
            "least_shade_pct": 100.0 * least_total / total,
            "wrong_open_sky_pct": 100.0 * wrong_open / open_total,
        },
        indent=2,
    )
)
print("wrote data/bench/s3-verify.json")
