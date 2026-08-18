"""S4, step 1: the four combinations of step mode and radius, timed.

Real engine code, no monkeypatching: only ``HorizonParams`` changes. Each cube
lands in data/bench/montilla-test-s4-<mode><radius>/v1 and is compared against
the S3 reference (exact 500), which is the current engine.

The burden of proof is on ``geometric`` here, not on ``exact``: S3 closed a
coverage hole and ``geometric``'s whole premise is to reopen it in the far
field. Its published 0.050% was measured against an ``exact`` that shared its
blindness, so it does not count. This bench only produces cubes and times;
bench_s4_verdict.py judges them against the arbiter.

**Retired with what it measured.** ``step_mode="geometric"`` left the code in
40fe8fc, so this script no longer runs against the current engine. It is kept
as the record of how ADR-028's figures were obtained, not as something to
re-run: that would mean reverting the commit that retired the mode.
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
REFERENCE = Path("data/bench/montilla-test-s3/v1")
VARIANTS = (
    ("exact", 500.0),
    ("geometric", 500.0),
    ("exact", 250.0),
    ("geometric", 250.0),
)

config = load_city("cities/montilla-test.yaml")
meta = json.loads((PUBLISHED / "metadata.json").read_text())["horizon"]

data = np.load("data/bench/montilla-test-padded.npz")
dsm, dtm, landcover = data["dsm"], data["dtm"], data["landcover"]
inner = tuple(int(v) for v in data["inner"])
res = float(data["resolution_m"][0])
scale = ANGLE_MAX_DEG / 255.0

report = {"variants": {}}

for step_mode, radius in VARIANTS:
    name = f"{step_mode}{int(radius)}"
    out_dir = Path(f"data/bench/montilla-test-s4-{name}/v1")
    out_dir.mkdir(parents=True, exist_ok=True)
    params = HorizonParams(
        sectors=meta["sectors"],
        max_distance_m=radius,
        observer_height_m=meta["observer_height_m"],
        tile_size=meta["tile_size"],
        step_mode=step_mode,
        workers=1,
    )
    print(f"=== {name}: {step_mode} at {radius:.0f} m ===", flush=True)

    with tempfile.TemporaryDirectory(prefix="s4-") as scratch:
        start = time.monotonic()
        result = compute_horizon_tiled(
            dsm,
            dtm,
            landcover,
            res,
            params,
            inner,
            scratch_dir=Path(scratch),
            progress=lambda message: None,
        )
        elapsed = time.monotonic() - start
        print(f"  swept in {elapsed:.1f}s (datum {result.height_datum_m} m)", flush=True)

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
        for filename, (cube, tags) in cubes.items():
            write_cog(out_dir / filename, cube, transform, config.crs, tags=tags)

        entry = {"step_mode": step_mode, "max_distance_m": radius, "seconds": elapsed, "cubes": {}}
        # Against the current engine (exact 500). Everything here can only lose
        # horizon: fewer cells looked at means fewer chances to raise the max.
        for filename in ("horizon.tif", "horizon_noveg.tif"):
            mine = cubes[filename][0]
            total = up = down = 0
            signed = 0.0
            biggest = 0
            over_5 = over_20 = 0
            with rasterio.open(REFERENCE / filename) as src:
                for band in range(1, src.count + 1):
                    reference = src.read([band])[0].astype(np.int16)
                    delta = mine[band - 1].astype(np.int16) - reference
                    up += int((delta > 0).sum())
                    down += int((delta < 0).sum())
                    total += int((delta != 0).sum())
                    signed += float(delta.sum())
                    biggest = max(biggest, int(np.abs(delta).max()))
                    over_5 += int((delta * scale < -5.0).sum())
                    over_20 += int((delta * scale < -20.0).sum())
            entry["cubes"][filename] = {
                "cells": int(mine.size),
                "differing": total,
                "pct": 100.0 * total / mine.size,
                "up": up,
                "down": down,
                "mean_signed_deg": signed / mine.size * scale,
                "max_step": biggest,
                "lost_over_5_deg": over_5,
                "lost_over_20_deg": over_20,
            }
            print(
                f"  {filename:18s} vs exact500: {total:,} differ ({100.0 * total / mine.size:.3f}%), "
                f"down {down:,} up {up:,}, mean {signed / mine.size * scale:+.4f} deg, "
                f"lost >5 deg {over_5:,}, >20 deg {over_20:,}",
                flush=True,
            )
        report["variants"][name] = entry

    for filename in ("canopy.tif", "metadata.json"):
        shutil.copy2(PUBLISHED / filename, out_dir / filename)

base = report["variants"]["exact500"]["seconds"]
print("\n  variant       seconds   speedup vs exact500")
for name, entry in report["variants"].items():
    print(f"  {name:12s}  {entry['seconds']:7.1f}   {base / entry['seconds']:.2f}x")

Path("data/bench/s4-sweep.json").write_text(json.dumps(report, indent=2))
print("\nwrote data/bench/s4-sweep.json")
