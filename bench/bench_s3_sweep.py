"""S3, step 2: sweep montilla-test under each distance convention.

Only the distance assigned to a cell changes; the set of cells each sector
visits is the same (it comes from rounding the nominal schedule). Everything
else is production code, datum included.

  S3_CONVENTION=centre|edge   -> data/bench/montilla-test-<name>/v1

**No longer reproduces its own numbers.** ``_original`` is whatever
``sector_offsets`` is today, and S3 replaced it: today's returns the DDA
traversal with entry distances, so ``nominal`` hands back the new convention
and ``centre`` re-labels the new cells. What this produced belongs to the code
of 2026-08-17 and lives in data/bench/s3-sweep-*.json and ADR-027.
"""

import json
import math
import os
import shutil
import tempfile
import time
from pathlib import Path

import numpy as np
import rasterio

import shade_pipeline.horizon as H
from shade_core.config import load_city
from shade_core.shade import NO_BLOCKER
from shade_pipeline.cog import write_cog
from shade_pipeline.grid import transform_from_bbox
from shade_pipeline.horizon import ANGLE_MAX_DEG, HorizonParams, compute_horizon_tiled

CONVENTION = os.environ.get("S3_CONVENTION", "centre")
PUBLISHED = Path("data/cities/montilla-test/v1")
NOMINAL = Path("data/bench/montilla-test-real/v1")
OUT_DIR = Path(f"data/bench/montilla-test-{CONVENTION}/v1")

_original = H.sector_offsets


def _dda(azimuth_deg, max_distance_m, res):
    """Cells the true ray crosses, with its entry distance into each."""
    azimuth = math.radians(azimuth_deg)
    d_col, d_row = math.sin(azimuth), -math.cos(azimuth)

    def first_and_delta(direction):
        if direction == 0.0:
            return math.inf, math.inf, 0
        return (0.5 * res) / abs(direction), res / abs(direction), 1 if direction > 0 else -1

    t_x, dt_x, step_col = first_and_delta(d_col)
    t_y, dt_y, step_row = first_and_delta(d_row)
    row = col = 0
    cells = []
    while True:
        if t_x < t_y:
            t, t_x = t_x, t_x + dt_x
            col += step_col
        else:
            t, t_y = t_y, t_y + dt_y
            row += step_row
        if t >= max_distance_m:
            return cells
        cells.append((row, col, t))


def sector_offsets_reconvened(sector, params, resolution_m):
    """Same cells, same order; only the distance attached to each changes."""
    if CONVENTION in ("dda", "dda_centre"):
        # Every cell the ray really enters, at its true entry distance: no
        # rounding, no per-cell convention, and none of the 15.2% the nominal
        # schedule skips. Ascending by construction, which the blocker class
        # needs (nearest wins ties).
        azimuth = sector * 360.0 / params.sectors
        cells = _dda(azimuth, params.max_distance_m, resolution_m)
        if CONVENTION == "dda_centre":
            # Ablation: the coverage fixed, the distance convention left as the
            # centre of the cell. Isolates what the 15.2% hole is worth.
            return [(r, c, math.hypot(r, c) * resolution_m) for r, c, _ in cells]
        return cells
    offsets = _original(sector, params, resolution_m)
    if CONVENTION == "nominal_entry":
        # Ablation: the convention fixed, the coverage left alone. Every
        # sampled cell is one the ray crosses (measured: zero exceptions), so
        # its true entry distance is always available.
        azimuth = sector * 360.0 / params.sectors
        entry = {(r, c): t for r, c, t in _dda(azimuth, params.max_distance_m, resolution_m)}
        return [(r, c, entry[(r, c)]) for r, c, _ in offsets if (r, c) in entry]
    out = []
    for d_row, d_col, nominal in offsets:
        centre = math.hypot(d_row, d_col) * resolution_m
        if CONVENTION == "centre":
            distance = centre
        elif CONVENTION == "edge":
            distance = centre - resolution_m / 2.0
        else:
            distance = nominal
        out.append((d_row, d_col, distance))
    return out


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

H.sector_offsets = sector_offsets_reconvened
OUT_DIR.mkdir(parents=True, exist_ok=True)
print(f"convention: {CONVENTION} -> {OUT_DIR}", flush=True)

with tempfile.TemporaryDirectory(prefix="s3-") as scratch:
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
    for name, (cube, tags) in cubes.items():
        write_cog(OUT_DIR / name, cube, transform, config.crs, tags=tags)

    # Against the nominal convention, in degrees: this is the number the
    # auditor put at +0.52, and it has to be read on the winning sample of
    # each sector, not on every offset.
    scale = ANGLE_MAX_DEG / 255.0
    report = {"convention": CONVENTION, "seconds": elapsed, "cubes": {}}
    for name in ("horizon.tif", "horizon_noveg.tif"):
        mine = cubes[name][0]
        total = up = down = 0
        signed = 0.0
        biggest = 0
        with rasterio.open(NOMINAL / name) as src:
            for band in range(1, src.count + 1):
                nominal_band = src.read([band])[0].astype(np.int16)
                delta = mine[band - 1].astype(np.int16) - nominal_band
                up += int((delta > 0).sum())
                down += int((delta < 0).sum())
                total += int((delta != 0).sum())
                signed += float(delta.sum())
                biggest = max(biggest, int(np.abs(delta).max()))
        cells = mine.size
        report["cubes"][name] = {
            "cells": cells,
            "differing": total,
            "pct": 100.0 * total / cells,
            "up": up,
            "down": down,
            "mean_signed_deg": signed / cells * scale,
            "max_step": biggest,
        }
        print(
            f"{name:20s} vs nominal: {total:,} of {cells:,} ({100.0 * total / cells:.3f}%), "
            f"up {up:,} down {down:,}, mean {signed / cells * scale:+.4f} deg, "
            f"max {biggest} steps ({biggest * scale:.3f} deg)",
            flush=True,
        )

for name in ("canopy.tif", "metadata.json"):
    shutil.copy2(PUBLISHED / name, OUT_DIR / name)
Path(f"data/bench/s3-sweep-{CONVENTION}.json").write_text(json.dumps(report, indent=2))
print(f"wrote data/bench/s3-sweep-{CONVENTION}.json")
