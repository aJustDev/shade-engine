"""S1 exit criterion: sweep montilla-test whole and compare against its COGs.

Same inputs the real build had (the cached padded stack, which reproduces the
published dsm.tif bit for bit), same params from its metadata.json, workers=1
like the build log. Times the sweep and demands the three cubes come out
identical, band by band.
"""

import json
import sys
import tempfile
import time
from pathlib import Path

import numpy as np
import rasterio

from shade_pipeline.horizon import HorizonParams, compute_horizon_tiled

NPZ = Path(sys.argv[1])
LABEL = sys.argv[2] if len(sys.argv) > 2 else "run"
ARTIFACTS = Path("data/cities/montilla-test/v1")

meta = json.loads((ARTIFACTS / "metadata.json").read_text())["horizon"]
assert json.loads((ARTIFACTS / "metadata.json").read_text())["coverage"] is None
params = HorizonParams(
    sectors=meta["sectors"],
    max_distance_m=meta["max_distance_m"],
    observer_height_m=meta["observer_height_m"],
    tile_size=meta["tile_size"],
    workers=1,
)
print(f"[{LABEL}] params: {params}", flush=True)

data = np.load(NPZ)
dsm, dtm, landcover = data["dsm"], data["dtm"], data["landcover"]
inner = tuple(int(v) for v in data["inner"])
res = float(data["resolution_m"][0])

with tempfile.TemporaryDirectory(prefix="s1-parity-") as scratch:
    start = time.monotonic()
    result = compute_horizon_tiled(
        dsm,
        dtm,
        landcover,
        res,
        params,
        inner,
        scratch_dir=Path(scratch),
        progress=lambda m: print(f"  {m}", flush=True),
    )
    elapsed = time.monotonic() - start
    print(f"[{LABEL}] sweep done in {elapsed:.1f}s ({elapsed / 60:.2f} min)", flush=True)

    cubes = {
        "horizon.tif": np.asarray(result.angles_q),
        "blocker_class.tif": np.asarray(result.blocker_class),
        "horizon_noveg.tif": np.asarray(result.angles_noveg_q),
    }
    verdict = {}
    for name, cube in cubes.items():
        differing = 0
        worst = 0
        with rasterio.open(ARTIFACTS / name) as src:
            assert src.count == cube.shape[0], f"{name}: {src.count} bands vs {cube.shape[0]}"
            for band in range(1, src.count + 1):
                published = src.read(band)
                mine = cube[band - 1]
                bad = published != mine
                if bad.any():
                    differing += int(bad.sum())
                    worst = max(
                        worst,
                        int(np.abs(published.astype(np.int16) - mine.astype(np.int16)).max()),
                    )
        cells = cube.size
        verdict[name] = {"differing": differing, "cells": cells, "max_step": worst}
        flag = "OK" if differing == 0 else "DIFFERS"
        print(
            f"[{LABEL}] {name:20s} {flag}: {differing:,} of {cells:,} cells"
            f"{'' if differing == 0 else f', max step {worst}'}",
            flush=True,
        )

out = Path(f"data/bench/parity-{LABEL}.json")
out.write_text(json.dumps({"label": LABEL, "seconds": elapsed, "cubes": verdict}, indent=2))
print(f"[{LABEL}] wrote {out}")
