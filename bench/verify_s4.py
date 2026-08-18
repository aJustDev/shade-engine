"""S4 exit gate: the real engine must move exactly what the bench predicted.

Two things could have gone wrong in the edit, and only a full sweep with the
production code can tell:

- retiring ``geometric`` was supposed to touch nothing that ``exact`` does, so
  every sector except the four diagonals must come out byte-identical to the S3
  cube;
- fixing the corner tie to the row axis was measured, ahead of the change, to
  move 1,409,468 cells. If the sweep moves a different number, the code is not
  what was measured and the session stops.
"""

import json
import tempfile
from pathlib import Path

import numpy as np
import rasterio

from shade_pipeline.horizon import HorizonParams, compute_horizon_tiled

REFERENCE = Path("data/bench/montilla-test-s3/v1")
PREDICTED_MOVED = 1_409_468
DIAGONALS = {8, 24, 40, 56}

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

with tempfile.TemporaryDirectory(prefix="s4v-") as scratch:
    result = compute_horizon_tiled(
        dsm,
        dtm,
        landcover,
        res,
        params,
        inner,
        scratch_dir=Path(scratch),
        progress=lambda message: print(f"  {message}", flush=True),
    )
    cubes = {
        "horizon.tif": np.asarray(result.angles_q),
        "blocker_class.tif": np.asarray(result.blocker_class),
        "horizon_noveg.tif": np.asarray(result.angles_noveg_q),
    }

moved_diagonal = moved_elsewhere = 0
per_sector: dict[int, int] = {}
for name, mine in cubes.items():
    with rasterio.open(REFERENCE / name) as src:
        for band in range(1, src.count + 1):
            sector = band - 1
            differing = int((src.read([band])[0] != mine[sector]).sum())
            if not differing:
                continue
            per_sector[sector] = per_sector.get(sector, 0) + differing
            if sector in DIAGONALS:
                moved_diagonal += differing
            else:
                moved_elsewhere += differing

print(f"\ncells moved in the four diagonal sectors: {moved_diagonal:,}")
print(f"cells moved anywhere else:                {moved_elsewhere:,}")
print(f"sectors touched: {sorted(per_sector)}")

ok = moved_elsewhere == 0 and moved_diagonal == PREDICTED_MOVED
print(
    f"\npredicted {PREDICTED_MOVED:,} moved, all diagonal -> "
    + ("MATCHES" if ok else "DOES NOT MATCH, stop")
)
Path("data/bench/s4-verify.json").write_text(
    json.dumps(
        {
            "moved_diagonal": moved_diagonal,
            "moved_elsewhere": moved_elsewhere,
            "predicted": PREDICTED_MOVED,
            "matches": ok,
            "per_sector": per_sector,
        },
        indent=2,
    )
)
raise SystemExit(0 if ok else 1)
