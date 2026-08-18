"""Does bench_s4_tiebreak reproduce the production sweep, plane for plane?

The S3 reference cube's horizon.tif was overwritten with tie-row's copy, so the
exit gate cannot be trusted until we know whether the bench's own sweep is the
same code path as compute_horizon_tiled. Compare the production sweep against
both rule cubes on the four diagonal planes only -- the bench copied every other
plane from BASE, so only those four are its own work.
"""

import json
import tempfile
from pathlib import Path

import numpy as np
import rasterio

from shade_pipeline.horizon import HorizonParams, compute_horizon_tiled

REFERENCE = Path("data/bench/montilla-test-s3/v1")
ROW = Path("data/bench/montilla-test-tie-row/v1")
COL = Path("data/bench/montilla-test-tie-col/v1")
DIAGONALS = (8, 24, 40, 56)

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
with tempfile.TemporaryDirectory(prefix="probe-") as scratch:
    result = compute_horizon_tiled(
        dsm, dtm, landcover, res, params, inner,
        scratch_dir=Path(scratch), progress=lambda m: None,
    )
mine = {
    "horizon.tif": np.asarray(result.angles_q),
    "blocker_class.tif": np.asarray(result.blocker_class),
    "horizon_noveg.tif": np.asarray(result.angles_noveg_q),
}

report = {}
for filename, cube in mine.items():
    with rasterio.open(ROW / filename) as src:
        row_cube = src.read()
    with rasterio.open(COL / filename) as src:
        col_cube = src.read()
    for sector in DIAGONALS:
        report[f"{filename}:{sector}"] = {
            "vs_row": int((cube[sector] != row_cube[sector]).sum()),
            "vs_col": int((cube[sector] != col_cube[sector]).sum()),
            "row_vs_col": int((row_cube[sector] != col_cube[sector]).sum()),
        }

print(f"{'plane':>28}  {'vs tie-row':>10}  {'vs tie-col':>10}  {'row vs col':>10}")
for key, entry in report.items():
    print(f"{key:>28}  {entry['vs_row']:10,}  {entry['vs_col']:10,}  {entry['row_vs_col']:10,}")

print()
print("production == tie-row on all four diagonals:",
      all(e["vs_row"] == 0 for e in report.values()))
print("row vs col, sectors 24 and 56, three cubes:",
      f"{sum(e['row_vs_col'] for k, e in report.items() if k.endswith(('24', '56'))):,}")
print("row vs col, all four diagonals, three cubes:",
      f"{sum(e['row_vs_col'] for e in report.values()):,}")
Path("data/bench/s4-tie-probe.json").write_text(json.dumps(report, indent=2))
