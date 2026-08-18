"""S2 measurement, step 1: sweep montilla-test whole in float32.

Monkeypatches ``iter_horizon_sectors`` with a float32 copy -- the sweep's only
change under discussion -- and runs the real tiled driver, so everything else
(dedup, tiling, quantization, write path) is production code. Writes the three
cubes as COGs into a parallel artifact dir so the verdict step can call
``compute_state_raster`` on both without special-casing anything.

Reports, against the published float64 cubes: how many quantized cells move
and by how many steps.
"""

import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

import numpy as np
import numpy.typing as npt
import rasterio

import shade_pipeline.horizon as H
from shade_core.config import load_city
from shade_core.shade import NO_BLOCKER, Landcover
from shade_pipeline.cog import write_cog
from shade_pipeline.grid import transform_from_bbox
from shade_pipeline.horizon import ANGLE_MAX_DEG, HorizonParams, sector_offsets

import os

NPZ = Path("data/bench/montilla-test-padded.npz")
PUBLISHED = Path("data/cities/montilla-test/v1")
VARIANT = os.environ.get("S2_VARIANT", "C1")
OUT_DIR = Path(f"data/bench/montilla-test-{VARIANT.lower()}/v1")
DTYPE = np.float32
DATUM = 0.0  # C3 sets a city-wide datum; per-tile would break tile independence


def iter_horizon_sectors_f32(
    dsm: npt.NDArray[np.floating],
    dtm: npt.NDArray[np.floating],
    landcover: npt.NDArray[np.uint8],
    resolution_m: float,
    params: HorizonParams,
    inner: H.Window,
):
    """Byte-for-byte the shipped kernel, with float64 swapped for float32."""
    row0, row1, col0, col1 = inner
    rows, cols = dsm.shape
    height, width = row1 - row0, col1 - col0
    observer_z = (
        dtm[row0:row1, col0:col1].astype(np.float64) - DATUM + params.observer_height_m
    ).astype(DTYPE)
    surface_z = (dsm.astype(np.float64) - DATUM).astype(DTYPE)
    surface_noveg_z = (
        np.where(landcover == Landcover.VEGETATION, dtm, dsm).astype(np.float64) - DATUM
    ).astype(DTYPE)

    for k in range(params.sectors):
        best_slope = np.full((height, width), -np.inf, dtype=DTYPE)
        best_class = np.full((height, width), NO_BLOCKER, dtype=np.uint8)
        best_slope_noveg = np.full((height, width), -np.inf, dtype=DTYPE)
        for d_row, d_col, distance in sector_offsets(k, params, resolution_m):
            i_lo = max(0, -(row0 + d_row))
            i_hi = min(height, rows - row0 - d_row)
            j_lo = max(0, -(col0 + d_col))
            j_hi = min(width, cols - col0 - d_col)
            if i_lo >= i_hi or j_lo >= j_hi:
                continue
            src = (
                slice(row0 + i_lo + d_row, row0 + i_hi + d_row),
                slice(col0 + j_lo + d_col, col0 + j_hi + d_col),
            )
            sub = (slice(i_lo, i_hi), slice(j_lo, j_hi))
            slope = (surface_z[src] - observer_z[sub]) / distance
            improved = slope > best_slope[sub]
            np.copyto(best_slope[sub], slope, where=improved)
            np.copyto(best_class[sub], landcover[src], where=improved)
            np.maximum(
                best_slope_noveg[sub],
                (surface_noveg_z[src] - observer_z[sub]) / distance,
                out=best_slope_noveg[sub],
            )
        best_class[best_slope <= 0.0] = NO_BLOCKER
        yield (
            np.maximum(np.degrees(np.arctan(best_slope)), 0.0).astype(np.float32),
            best_class,
            np.maximum(np.degrees(np.arctan(best_slope_noveg)), 0.0).astype(np.float32),
        )


config = load_city("cities/montilla-test.yaml")
meta = json.loads((PUBLISHED / "metadata.json").read_text())["horizon"]
params = HorizonParams(
    sectors=meta["sectors"],
    max_distance_m=meta["max_distance_m"],
    observer_height_m=meta["observer_height_m"],
    tile_size=meta["tile_size"],
    workers=1,
)

data = np.load(NPZ)
dsm, dtm, landcover = data["dsm"], data["dtm"], data["landcover"]
inner = tuple(int(v) for v in data["inner"])
res = float(data["resolution_m"][0])

if VARIANT == "C3":
    # City-wide, never per tile: a per-tile datum would make the output depend
    # on tile_size, which is the one thing the sweep guarantees it does not.
    DATUM = float(np.floor(dtm.min()))
print(f"variant {VARIANT}, datum {DATUM} m -> out {OUT_DIR}")

H.iter_horizon_sectors = iter_horizon_sectors_f32
OUT_DIR.mkdir(parents=True, exist_ok=True)

with tempfile.TemporaryDirectory(prefix="s2-f32-") as scratch:
    start = time.monotonic()
    result = H.compute_horizon_tiled(
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
    print(f"float32 sweep done in {elapsed:.1f}s ({elapsed / 60:.2f} min)", flush=True)

    transform = transform_from_bbox(config.bbox, res)
    common = {"city_id": config.id}
    horizon_tags = {
        **common,
        "angle_max_deg": str(ANGLE_MAX_DEG),
        "sectors": str(params.sectors),
        "max_distance_m": str(params.max_distance_m),
        "observer_height_m": str(params.observer_height_m),
    }
    write_cog(
        OUT_DIR / "horizon.tif",
        np.asarray(result.angles_q),
        transform,
        config.crs,
        tags=horizon_tags,
    )
    write_cog(
        OUT_DIR / "blocker_class.tif",
        np.asarray(result.blocker_class),
        transform,
        config.crs,
        tags={**common, "no_blocker": str(NO_BLOCKER)},
    )
    write_cog(
        OUT_DIR / "horizon_noveg.tif",
        np.asarray(result.angles_noveg_q),
        transform,
        config.crs,
        tags={**horizon_tags, "surface": "vegetation lowered to terrain"},
    )

    cubes = {
        "horizon.tif": np.asarray(result.angles_q),
        "blocker_class.tif": np.asarray(result.blocker_class),
        "horizon_noveg.tif": np.asarray(result.angles_noveg_q),
    }
    report = {"seconds": elapsed, "cubes": {}}
    for name, cube in cubes.items():
        steps: dict[int, int] = {}
        with rasterio.open(PUBLISHED / name) as src:
            for band in range(1, src.count + 1):
                published = src.read([band])[0]
                diff = np.abs(published.astype(np.int16) - cube[band - 1].astype(np.int16))
                for step in np.unique(diff[diff > 0]):
                    steps[int(step)] = steps.get(int(step), 0) + int((diff == step).sum())
        differing = sum(steps.values())
        report["cubes"][name] = {
            "cells": int(cube.size),
            "differing": differing,
            "pct": 100.0 * differing / cube.size,
            "steps": steps,
        }
        print(
            f"{name:20s} {differing:,} of {cube.size:,} cells "
            f"({100.0 * differing / cube.size:.5f}%), steps {steps}",
            flush=True,
        )

# Everything the verdict step reads that the sweep does not produce.
for name in ("canopy.tif", "metadata.json"):
    shutil.copy2(PUBLISHED / name, OUT_DIR / name)

Path(f"data/bench/s2-sweep-{VARIANT.lower()}.json").write_text(json.dumps(report, indent=2))
print(f"wrote data/bench/s2-sweep-{VARIANT.lower()}.json")
