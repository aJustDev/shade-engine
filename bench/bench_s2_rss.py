"""S2 measurement, step 6: the auditor's third point -- real RSS, not the model.

estimate_sweep_worker_bytes is deliberately pessimistic: ADR-018 records 162 MB
modelled against 87 MiB of actual process growth. If the model drops 19% but
the process does not, what shrank is the safety margin, and that margin is what
stands between a twelve-hour build and the OOM killer at hour nine.

Sweeps one real tile per variant and samples /proc/self/statm while it runs.
"""

import json
import math
import threading
import time
from pathlib import Path

import numpy as np
import numpy.typing as npt

import shade_pipeline.horizon as H
from shade_core.shade import NO_BLOCKER, Landcover
from shade_pipeline.budget import estimate_sweep_worker_bytes
from shade_pipeline.horizon import HorizonParams, quantized_horizon_block, sector_offsets

PAGE = 4096
DATUM = 0.0
DTYPE = np.float64


def rss_bytes() -> int:
    return int(Path("/proc/self/statm").read_text().split()[1]) * PAGE


class Sampler(threading.Thread):
    """Peak RSS while the sweep runs; 20 ms is far finer than a 17 s tile."""

    def __init__(self) -> None:
        super().__init__(daemon=True)
        self.peak = rss_bytes()
        self.stop = threading.Event()

    def run(self) -> None:
        while not self.stop.wait(0.02):
            self.peak = max(self.peak, rss_bytes())


def kernel(dsm, dtm, landcover, resolution_m, params, inner):
    """The kernel under test: DTYPE, and heights relative to DATUM."""
    row0, row1, col0, col1 = inner
    rows, cols = dsm.shape
    height, width = row1 - row0, col1 - col0
    datum = DTYPE(DATUM)
    observer_z = (dtm[row0:row1, col0:col1].astype(DTYPE) - datum) + params.observer_height_m
    surface_z = dsm.astype(DTYPE) - datum
    surface_noveg_z = np.where(landcover == Landcover.VEGETATION, dtm, dsm).astype(DTYPE) - datum

    for k in range(params.sectors):
        best_slope = np.full((height, width), -np.inf, dtype=DTYPE)
        best_class = np.full((height, width), NO_BLOCKER, dtype=np.uint8)
        best_slope_noveg = np.full((height, width), -np.inf, dtype=DTYPE)
        for d_row, d_col, distance in sector_offsets(k, params, resolution_m):
            i_lo, i_hi = max(0, -(row0 + d_row)), min(height, rows - row0 - d_row)
            j_lo, j_hi = max(0, -(col0 + d_col)), min(width, cols - col0 - d_col)
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


data = np.load("data/bench/montilla-test-padded.npz")
DSM, DTM, LC = data["dsm"], data["dtm"], data["landcover"]
INNER = tuple(int(v) for v in data["inner"])
RES = float(data["resolution_m"][0])
PARAMS = HorizonParams(sectors=64, max_distance_m=500.0, tile_size=512)
PAD = math.ceil(PARAMS.max_distance_m / RES)
TILE = (INNER[0], INNER[0] + 512, INNER[2], INNER[2] + 512)
p0, p1, q0, q1 = TILE[0] - PAD, TILE[1] + PAD, TILE[2] - PAD, TILE[3] + PAD
WINDOW = (DSM[p0:p1, q0:q1], DTM[p0:p1, q0:q1], LC[p0:p1, q0:q1])
LOCAL = (TILE[0] - p0, TILE[1] - p0, TILE[2] - q0, TILE[3] - q0)

model = estimate_sweep_worker_bytes(PARAMS.sectors, PARAMS.tile_size, PAD)
print(f"model (unchanged code): {model / 1e6:.0f} MB")
report = {"model_bytes_float64": model}

H.iter_horizon_sectors = kernel
for label, dtype, datum in (
    ("float64 (today)", np.float64, 0.0),
    ("float32 + datum", np.float32, 350.0),
):
    DTYPE, DATUM = dtype, datum
    # Touch nothing else between runs: the baseline is what the process holds
    # with the rasters loaded and no sweep in flight.
    baseline = rss_bytes()
    sampler = Sampler()
    sampler.start()
    start = time.monotonic()
    cubes = quantized_horizon_block(*WINDOW, RES, PARAMS, LOCAL)
    elapsed = time.monotonic() - start
    sampler.stop.set()
    sampler.join()
    growth = sampler.peak - baseline
    del cubes
    print(
        f"{label:16s} tile in {elapsed:5.1f}s | baseline {baseline / 2**20:6.1f} MiB | "
        f"peak {sampler.peak / 2**20:6.1f} MiB | growth {growth / 2**20:5.1f} MiB",
        flush=True,
    )
    report[label] = {
        "seconds": elapsed,
        "baseline_mib": baseline / 2**20,
        "peak_mib": sampler.peak / 2**20,
        "growth_mib": growth / 2**20,
    }

Path("data/bench/s2-rss.json").write_text(json.dumps(report, indent=2))
print("wrote data/bench/s2-rss.json")
