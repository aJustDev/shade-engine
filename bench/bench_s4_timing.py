"""S4, step 0: the six variants timed on a quiet machine.

The published S4 table has exact500 at 130.7 s while S3 closed at 116.4 s. Same
machine, different contention: the S4 sweeps ran with background jobs alongside.
The factors *within* that batch are fine (everything shared the same load); the
absolute is not comparable with S3, and an ADR cannot carry two baselines
without saying which is which.

So: time only. No COG written, no cube compared -- those are already measured
and do not depend on the clock. exact500 runs first and last, and the gap
between the two is how much drift the numbers carry.

**Retired with what it measured.** ``step_mode="geometric"`` left the code in
40fe8fc, so this script no longer runs against the current engine. It is kept
as the record of how ADR-028's figures were obtained, not as something to
re-run: that would mean reverting the commit that retired the mode.
"""

import json
import tempfile
import time
from pathlib import Path

import numpy as np

from shade_pipeline.horizon import HorizonParams, compute_horizon_tiled

PUBLISHED = Path("data/cities/montilla-test/v1")
RUNS = (
    ("exact", 500.0),
    ("exact", 250.0),
    ("exact", 187.0),
    ("exact", 125.0),
    ("geometric", 500.0),
    ("geometric", 250.0),
    ("exact", 500.0),  # again, to bound the drift
)

meta = json.loads((PUBLISHED / "metadata.json").read_text())["horizon"]
data = np.load("data/bench/montilla-test-padded.npz")
dsm, dtm, landcover = data["dsm"], data["dtm"], data["landcover"]
inner = tuple(int(v) for v in data["inner"])
res = float(data["resolution_m"][0])

results: list[dict[str, object]] = []
for step_mode, radius in RUNS:
    params = HorizonParams(
        sectors=meta["sectors"],
        max_distance_m=radius,
        observer_height_m=meta["observer_height_m"],
        tile_size=meta["tile_size"],
        step_mode=step_mode,
        workers=1,
    )
    with tempfile.TemporaryDirectory(prefix="s4t-") as scratch:
        start = time.monotonic()
        compute_horizon_tiled(
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
    name = f"{step_mode}{int(radius)}"
    results.append({"name": name, "step_mode": step_mode, "radius": radius, "seconds": elapsed})
    print(f"  {name:14s} {elapsed:7.1f} s", flush=True)

first = results[0]["seconds"]
last = results[-1]["seconds"]
drift = abs(last - first) / first
print(f"\ndrift between the two exact500 runs: {abs(last - first):.1f} s ({100.0 * drift:.1f}%)")

base = (float(first) + float(last)) / 2.0
print(f"\n  {'variant':>14}  {'seconds':>8}  {'factor':>7}")
for entry in results[:-1]:
    print(
        f"  {entry['name']:>14}  {float(entry['seconds']):8.1f}  {base / float(entry['seconds']):6.2f}x"
    )

Path("data/bench/s4-timing.json").write_text(
    json.dumps({"runs": results, "baseline_seconds": base, "drift_pct": 100.0 * drift}, indent=2)
)
print("\nwrote data/bench/s4-timing.json")
