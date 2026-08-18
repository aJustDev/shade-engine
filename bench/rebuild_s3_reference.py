"""Rebuild the S3 reference cube with the tie-break the S3 engine really had.

data/bench/montilla-test-s3/v1/horizon.tif was overwritten on 2026-08-18 at
02:33 with montilla-test-tie-row's copy (identical md5), so the S4 exit gate
compares today's sweep against a cube that already contains today's rule. The
other two cubes survived.

Nothing else in the sweep changed between bcd4add (S3) and today except the
corner rule and the retirement of geometric, which never touched the exact
path. So patching ray_cells back to `next_col < next_row` and sweeping with
production code reproduces the S3 cube -- and the surviving blocker_class.tif
and horizon_noveg.tif are the check: if the rebuild matches them bit for bit,
it is the right cube.
"""

import json
import math
import tempfile
from pathlib import Path

import numpy as np
import rasterio

import shade_pipeline.horizon as H
from shade_pipeline.cog import write_cog
from shade_pipeline.horizon import HorizonParams, compute_horizon_tiled

REFERENCE = Path("data/bench/montilla-test-s3/v1")


def ray_cells_pre_rule(azimuth_deg, max_distance_m, resolution_m):
    """ray_cells as of bcd4add: the corner tie decided by whichever compares
    smaller, i.e. by an ulp of sin against cos."""
    azimuth = math.radians(azimuth_deg)
    d_col, d_row = math.sin(azimuth), -math.cos(azimuth)

    def schedule(direction):
        if direction == 0.0:
            return math.inf, math.inf, 0
        return (
            (0.5 * resolution_m) / abs(direction),
            resolution_m / abs(direction),
            (1 if direction > 0 else -1),
        )

    next_col, step_col_m, step_col = schedule(d_col)
    next_row, step_row_m, step_row = schedule(d_row)

    row = col = 0
    entries = []
    while True:
        if next_col < next_row:
            entry, next_col = next_col, next_col + step_col_m
            col += step_col
        else:
            entry, next_row = next_row, next_row + step_row_m
            row += step_row
        if entry >= max_distance_m:
            break
        entries.append((row, col, entry))
    return [
        (r, c, e, entries[i + 1][2] if i + 1 < len(entries) else max_distance_m)
        for i, (r, c, e) in enumerate(entries)
    ]


H.ray_cells = ray_cells_pre_rule

meta = json.loads((REFERENCE / "metadata.json").read_text())["horizon"]
data = np.load("data/bench/montilla-test-padded.npz")
dsm, dtm, landcover = data["dsm"], data["dtm"], data["landcover"]
inner = tuple(int(v) for v in data["inner"])
res = float(data["resolution_m"][0])

params = HorizonParams(
    sectors=meta["sectors"],
    max_distance_m=meta["max_distance_m"],
    observer_height_m=meta["observer_height_m"],
    tile_size=meta["tile_size"],
    workers=1,
)
with tempfile.TemporaryDirectory(prefix="s3r-") as scratch:
    result = compute_horizon_tiled(
        dsm, dtm, landcover, res, params, inner,
        scratch_dir=Path(scratch), progress=lambda m: print(f"  {m}", flush=True),
    )

rebuilt = {
    "horizon.tif": np.asarray(result.angles_q),
    "blocker_class.tif": np.asarray(result.blocker_class),
    "horizon_noveg.tif": np.asarray(result.angles_noveg_q),
}

# The two survivors are the proof. If they match, the patch is the S3 engine.
ok = True
for filename in ("blocker_class.tif", "horizon_noveg.tif"):
    with rasterio.open(REFERENCE / filename) as src:
        survivor = src.read()
    differing = int((survivor != rebuilt[filename]).sum())
    print(f"{filename}: {differing:,} cells differ from the surviving reference")
    ok = ok and differing == 0

print(f"\nthe rebuild reproduces the surviving cubes: {ok}")
if ok:
    with rasterio.open(REFERENCE / "horizon.tif") as src:
        tags, transform, crs = src.tags(), src.transform, src.crs
    write_cog(REFERENCE / "horizon.tif", rebuilt["horizon.tif"], transform, crs, tags=tags)
    print("restored", REFERENCE / "horizon.tif")
