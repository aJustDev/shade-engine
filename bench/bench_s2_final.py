"""S2 verification: the shipped code, not a copy of it.

No monkeypatching. Calls compute_horizon_tiled exactly as build.py does and
lets it derive its own datum, then measures the gate again against the
published float64 cubes.
"""

import json
import shutil
import tempfile
import time
from pathlib import Path

import numpy as np
import rasterio

from shade_core.config import load_city
from shade_core.shade import NO_BLOCKER
from shade_pipeline.cog import write_cog
from shade_pipeline.grid import transform_from_bbox
from shade_pipeline.horizon import ANGLE_MAX_DEG, HorizonParams, compute_horizon_tiled

PUBLISHED = Path("data/cities/montilla-test/v1")
BENCH = Path("data/bench/montilla-test-dda/v1")
OUT_DIR = Path("data/bench/montilla-test-s3/v1")

config = load_city("cities/montilla-test.yaml")
meta = json.loads((PUBLISHED / "metadata.json").read_text())["horizon"]
params = HorizonParams(
    sectors=meta["sectors"],
    max_distance_m=meta["max_distance_m"],
    observer_height_m=meta["observer_height_m"],
    tile_size=meta["tile_size"],
    workers=1,
)

data = np.load("data/bench/montilla-test-padded.npz")
dsm, dtm, landcover = data["dsm"], data["dtm"], data["landcover"]
inner = tuple(int(v) for v in data["inner"])
res = float(data["resolution_m"][0])

OUT_DIR.mkdir(parents=True, exist_ok=True)
with tempfile.TemporaryDirectory(prefix="s2-final-") as scratch:
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
    print(f"sweep done in {elapsed:.1f}s, datum {result.height_datum_m} m", flush=True)

    transform = transform_from_bbox(config.bbox, res)
    common = {"city_id": config.id}
    horizon_tags = {
        **common,
        "angle_max_deg": str(ANGLE_MAX_DEG),
        "sectors": str(params.sectors),
        "max_distance_m": str(params.max_distance_m),
        "observer_height_m": str(params.observer_height_m),
    }
    cubes = {
        "horizon.tif": (np.asarray(result.angles_q), horizon_tags),
        "blocker_class.tif": (
            np.asarray(result.blocker_class),
            {**common, "no_blocker": str(NO_BLOCKER)},
        ),
        "horizon_noveg.tif": (
            np.asarray(result.angles_noveg_q),
            {**horizon_tags, "surface": "vegetation lowered to terrain"},
        ),
    }
    report = {"seconds": elapsed, "height_datum_m": result.height_datum_m, "cubes": {}}
    for name, (cube, tags) in cubes.items():
        write_cog(OUT_DIR / name, cube, transform, config.crs, tags=tags)
        steps: dict[int, int] = {}
        up = down = 0
        with rasterio.open(BENCH / name) as src:
            for band in range(1, src.count + 1):
                published = src.read([band])[0].astype(np.int16)
                mine = cube[band - 1].astype(np.int16)
                delta = mine - published
                up += int((delta > 0).sum())
                down += int((delta < 0).sum())
                diff = np.abs(delta)
                for step in np.unique(diff[diff > 0]):
                    steps[int(step)] = steps.get(int(step), 0) + int((diff == step).sum())
        differing = sum(steps.values())
        report["cubes"][name] = {
            "cells": int(cube.size),
            "differing": differing,
            "pct": 100.0 * differing / cube.size,
            "up": up,
            "down": down,
            "steps": steps,
        }
        print(
            f"{name:20s} {differing:,} of {cube.size:,} ({100.0 * differing / cube.size:.5f}%), "
            f"up {up:,} down {down:,}, steps {steps}",
            flush=True,
        )

for name in ("canopy.tif", "metadata.json"):
    shutil.copy2(PUBLISHED / name, OUT_DIR / name)
Path("data/bench/s3-final.json").write_text(json.dumps(report, indent=2))
print("wrote data/bench/s3-final.json")
