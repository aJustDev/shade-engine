"""Production horizon sweep: tiled, vectorized, and true to the oracle.

This reimplements ``shade_core.horizon.compute_horizon_reference`` for real
city rasters. Both walk the same cells the same way --- the ray traversal of
:func:`shade_core.raycast.ray_cells`, which since
[[ADR-027]] is what "the sample is at distance d" means here: a DSM cell is a
1x1 m column and it starts blocking at the distance the ray enters it. That
convention took the engine's verdict from 2.263% wrong against exact traversal
to 0.714% over open sky, the single largest correction of the phase.

Restructured on top of it in two ways that cannot change the result, plus one
that changes it by less than the artifact can represent:

- **Tiling with buffer**: the target area is swept in tiles, each reading a
  window padded by ``buffer_pixels`` -- at least the largest possible offset
  -- so a sample is inside the padded window iff it is inside the dataset.
  Per-pixel sample sets are therefore identical regardless of tile bounds.
- **One arctan per sector**: both accumulators keep ``(dz / d)``, the tangent,
  and convert once per sector instead of once per sample. ``arctan`` is
  monotonic, so neither the maximum nor the argmax moves -- neither the angle
  nor the blocker class can change. This is *not* the oracle's arithmetic and
  does not claim to be: on a real 512x512 tile of ``montilla-test`` the
  float64 accumulator disagrees with a per-sample ``arctan2`` on 7.1% of
  values, by at most 2 ulp (1.4e-14 deg). What it reproduces is the oracle's
  result: all 2,097,152 float32 values measured came out identical, and the
  artifact's 0.353 deg quantization step sits twelve orders of magnitude
  above that discrepancy. Worth 5.1x on the kernel (25.0 -> 4.9
  ns/px/sample) and 4.8x on a whole montilla-test sweep, 16m 57s -> 3m 32s,
  with all three cubes coming out byte-identical to the published artifacts
  (shade-docs: learning/horizon-algorithm.md).
- **The precision of the data, relative to a datum**: float32 in, float32
  through, heights measured from ``height_datum`` rather than from the
  ellipsoid. Another 2.09x (3m 32s -> 1m 42s on montilla-test, 10.0x since the
  start of the phase) and 37% off the peak RSS of a tile. This one *does*
  change the output, and here is by how much on real data: of the 83 instants
  the product renders, **2 pixel-instants out of 106,284,820 flip verdict**;
  1,133 of 81,954,560 cube cells move, all by one quantization step and 567
  up against 566 down; 6 cells change blocker class, every one of them a tie
  whose two candidates sit within 0.0002 of a step. The datum is what makes
  those numbers small -- without it the same float32 loses shade city-wide
  (see :func:`height_datum`). Not unbiased, small: shade-docs:
  learning/precision-de-alturas.md.

The sweep also records *what* blocks each sector: whenever a sample raises a
pixel's max angle, its landcover class is kept. Strict ``>`` with ascending
distances means the nearest blocker wins ties, matching core's ray-march
intuition -- except at an exact corner crossing, where the two cells genuinely
share a distance and the tie goes to whichever
:func:`~shade_core.raycast.ray_cells` emitted first, which is a fixed rule and
not an accident of libm (measured on montilla-test: 246 classes across the four
diagonal sectors, and not one angle). Sectors whose final angle is 0 (open sky)
get ``NO_BLOCKER``.

A single class per sector cannot answer "would this pixel be shaded anyway
without the trees": the class only names whichever obstacle won the argmax,
often by centimetres. So the same pass builds a **second horizon** over the
surface with vegetation lowered to the ground, and the two cubes together give
a real decomposition (see ``shade_pipeline.shade_raster``). The whole second
cube costs +9% of sweep time because it accumulates the tangent; a second
``arctan2`` per sample would have been +109%. That measurement is what
eventually sent the first cube down the same road.

The one knob for how much a sweep costs is ``max_distance_m``, and it is a knob
with a proof attached: an obstacle of height h left outside radius R can raise
the horizon by at most ``atan(h / R)``, so shortening the radius can only ever
matter with the sun below that angle. A ``geometric`` step mode used to offer
the same speed by thinning the far field instead, and it was withdrawn in
[[ADR-028]] -- it bounded its error by where the tall thin things happened to
fall relative to its schedule, which is not a bound at all.

Because tiles are independent by construction, the sweep parallelizes by tile
(``workers``). Two properties keep the output bit-identical to a serial run:
the same ``_sweep_tile`` runs in both modes, and **only the parent writes** --
workers return quantized tiles and the main process files them into the
memmaps exactly as the serial loop does, leaving the hardened
scratch -> flush -> COG -> verify path untouched.
"""

import multiprocessing
import time
from collections.abc import Callable, Generator, Iterator
from concurrent.futures import ProcessPoolExecutor, as_completed
from concurrent.futures.process import BrokenProcessPool
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import numpy as np
import numpy.typing as npt

from shade_core.raycast import ray_cells
from shade_core.shade import NO_BLOCKER, Landcover
from shade_pipeline.budget import (
    check_worker_budget,
    cpu_budget,
    estimate_sweep_worker_bytes,
    warn_if_serial_is_tight,
)
from shade_pipeline.events import EventSink, emit
from shade_pipeline.grid import buffer_pixels
from shade_pipeline.progress import format_duration

ANGLE_MAX_DEG: Final = 90.0

__all__ = [
    "ANGLE_MAX_DEG",
    "NO_BLOCKER",
    "HorizonParams",
    "HorizonResult",
    "compute_horizon_block",
    "compute_horizon_tiled",
    "height_datum",
    "iter_horizon_sectors",
    "quantize_angles",
    "quantized_horizon_block",
    "sector_offsets",
    "tile_jobs",
]

Window = tuple[int, int, int, int]
"""A (row0, row1, col0, col1) rectangle in array coordinates."""

SweptTile = tuple[Window, npt.NDArray[np.uint8], npt.NDArray[np.uint8], npt.NDArray[np.uint8]]
"""One finished tile travelling back to the writer: where it goes, then the cubes."""


@dataclass(frozen=True)
class HorizonParams:
    """Knobs of the horizon sweep; defaults match the spec.

    ``workers`` is the one knob that is not physics: it splits the tiles
    across processes and cannot change a single output value. It defaults to
    1 so no existing runbook changes behavior by upgrading.
    """

    sectors: int = 64
    max_distance_m: float = 500.0
    observer_height_m: float = 1.6
    tile_size: int = 512
    workers: int = 1


@dataclass(frozen=True)
class HorizonResult:
    """Quantized horizon angles and blocker classes, shape (sectors, rows, cols).

    ``angles_noveg_q`` is the same horizon over a surface with vegetation
    lowered to the terrain: the sky as it would be with no trees.

    ``height_datum_m`` is the elevation the sweep subtracted before computing
    (see :func:`height_datum`). It travels out so the build can record it: a
    cube is only reproducible if you know which datum produced it.
    """

    angles_q: npt.NDArray[np.uint8]
    blocker_class: npt.NDArray[np.uint8]
    angles_noveg_q: npt.NDArray[np.uint8]
    height_datum_m: float = 0.0


def height_datum(dtm: npt.NDArray[np.floating]) -> float:
    """Elevation to subtract before sweeping: the DTM's median, to the hundred.

    The sweep only ever uses *differences* of height, so the absolute elevation
    above the ellipsoid enters no operation -- and in float32 it is expensive
    ballast. At 367 m the ulp is 256 * 2^-23 = 3.05e-05 m, and since a city's
    DTM shares one binade, a constant like the observer's 1.6 m always rounds
    the same way: 1.6 / ulp = 52428.8, so +0.2 ulp = +6.1e-06 m on *every*
    pixel of the city. A higher observer sees a lower skyline, so the whole
    city quietly loses shade (measured on montilla-test: 6,219 cells down, 0
    up). Working relative to a datum spends those bits on the heights that
    matter instead.

    **Rounded, not truncated.** Truncating Montilla's minimum to its hundred
    gives 200 m, which leaves heights at 90-212 m -- still a high binade, and
    1.6 m lands on another unlucky fraction: measured, that recovers 4% of the
    error, against 100% for the nearest hundred. The nearest hundred to the
    median also keeps the datum stable: a new LAZ tile does not move a city's
    median 50 m, and a datum that moves changes every cube bit for bit.

    A hundred is deliberate for a second reason: within a factor of two of the
    heights it subtracts, so the subtraction is exact in float32 (Sterbenz) and
    needs no float64 detour.

    See shade-docs: learning/precision-de-alturas.md.
    """
    return float(round(float(np.median(dtm)) / 100.0) * 100.0)


def sector_offsets(
    sector: int, params: HorizonParams, resolution_m: float
) -> list[tuple[int, int, float]]:
    """(d_row, d_col, distance) samples for one sector, ascending distance.

    These are the cells the ray really crosses, each at the distance it is
    entered (:func:`shade_core.raycast.ray_cells`), which is the convention of
    [[ADR-027]]: a DSM cell is a 1x1 m column and it starts blocking at its
    nearest face. Same convention in the oracle and in the API's ray-march, or
    none of them can check the others.

    There is no thinning mode any more. A ``geometric`` schedule used to drop
    the cells that fell inside a multiplicatively growing window -- dense near,
    sparse far -- on the premise that what matters is *relative* distance error.
    That premise is about resolution along a ray and it is sound on an axis; off
    an axis the dropped cells are not finer samples of the same line, they are
    different cells of the plane, and dropping them is the coverage hole
    ADR-027 closed, reopened on purpose. Measured, it cost 0.485 points of
    verdict for 3.1x, and cutting ``max_distance_m`` buys speed more cheaply and
    with an error bounded by geometry rather than by the data ([[ADR-028]]).
    """
    azimuth_deg = sector * 360.0 / params.sectors
    cells = ray_cells(azimuth_deg, params.max_distance_m, resolution_m)
    return [(d_row, d_col, entry) for d_row, d_col, entry, _ in cells]


def iter_horizon_sectors(
    dsm: npt.NDArray[np.floating],
    dtm: npt.NDArray[np.floating],
    landcover: npt.NDArray[np.uint8],
    resolution_m: float,
    params: HorizonParams,
    inner: Window,
    height_datum_m: float = 0.0,
) -> Iterator[tuple[npt.NDArray[np.float32], npt.NDArray[np.uint8], npt.NDArray[np.float32]]]:
    """Sweep the ``inner`` window one sector at a time, in sector order.

    Yields (angles, blocker classes, vegetation-free angles) for sector k as
    single planes rather than accumulating cubes, so a consumer that only
    wants the quantized result never holds a float32 cube at all.

    ``height_datum_m`` is subtracted from every height first; it defaults to 0
    and a block never picks its own, because a per-tile datum would make the
    output depend on ``tile_size``, which is the one thing this sweep
    guarantees it does not. The tiled driver derives one per city.

    Samples come from anywhere in the given arrays; the caller is responsible
    for passing enough surrounding context (see the tiled driver).
    """
    row0, row1, col0, col1 = inner
    rows, cols = dsm.shape
    height, width = row1 - row0, col1 - col0
    # The precision of the data, not more: the LiDAR is float32 and its vertical
    # accuracy is ~10-15 cm. Relative to the datum, so the mantissa is spent on
    # the heights and not on the elevation above the ellipsoid, which cancels in
    # every subtraction below. The subtraction is exact in float32 for a datum
    # within a factor of two of the heights (Sterbenz), so there is no float64
    # detour and no float64 temporary of the padded window either.
    datum = np.float32(height_datum_m)
    observer_z = (dtm[row0:row1, col0:col1].astype(np.float32, copy=False) - datum) + np.float32(
        params.observer_height_m
    )
    surface_z = dsm.astype(np.float32, copy=False) - datum
    # Felling the trees means exposing the ground under them, not deleting the
    # cell: on a wooded slope the terrain itself still blocks.
    surface_noveg_z = (
        np.where(landcover == Landcover.VEGETATION, dtm, dsm).astype(np.float32, copy=False) - datum
    )

    for k in range(params.sectors):
        best_slope = np.full((height, width), -np.inf, dtype=np.float32)
        best_class = np.full((height, width), NO_BLOCKER, dtype=np.uint8)
        best_slope_noveg = np.full((height, width), -np.inf, dtype=np.float32)
        for d_row, d_col, distance in sector_offsets(k, params, resolution_m):
            # Target pixel (i, j) samples array cell (row0 + i + d_row, ...);
            # clamp to the sub-rectangle of targets whose sample is in range.
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
        # Sign survives arctan, so a non-positive tangent is a non-positive angle.
        best_class[best_slope <= 0.0] = NO_BLOCKER
        # One arctan per sector, on the accumulated tangent. -inf (no sample in
        # range) lands on -90 and the floor at 0 absorbs it.
        yield (
            np.maximum(np.degrees(np.arctan(best_slope)), 0.0).astype(np.float32),
            best_class,
            np.maximum(np.degrees(np.arctan(best_slope_noveg)), 0.0).astype(np.float32),
        )


def compute_horizon_block(
    dsm: npt.NDArray[np.floating],
    dtm: npt.NDArray[np.floating],
    landcover: npt.NDArray[np.uint8],
    resolution_m: float,
    params: HorizonParams,
    inner: Window,
    height_datum_m: float = 0.0,
) -> tuple[npt.NDArray[np.float32], npt.NDArray[np.uint8], npt.NDArray[np.float32]]:
    """Pre-quantization cubes for the ``inner`` window: this is the oracle's peer.

    Unquantized angles, which is what the reference comparisons need;
    production goes through :func:`quantized_horizon_block` instead.
    """
    row0, row1, col0, col1 = inner
    shape = (params.sectors, row1 - row0, col1 - col0)
    angles = np.empty(shape, dtype=np.float32)
    blocker = np.empty(shape, dtype=np.uint8)
    angles_noveg = np.empty(shape, dtype=np.float32)
    sectors = iter_horizon_sectors(dsm, dtm, landcover, resolution_m, params, inner, height_datum_m)
    for k, (sector_angles, sector_blocker, sector_noveg) in enumerate(sectors):
        angles[k] = sector_angles
        blocker[k] = sector_blocker
        angles_noveg[k] = sector_noveg
    return angles, blocker, angles_noveg


def quantized_horizon_block(
    dsm: npt.NDArray[np.floating],
    dtm: npt.NDArray[np.floating],
    landcover: npt.NDArray[np.uint8],
    resolution_m: float,
    params: HorizonParams,
    inner: Window,
    height_datum_m: float = 0.0,
) -> tuple[npt.NDArray[np.uint8], npt.NDArray[np.uint8], npt.NDArray[np.uint8]]:
    """The same block, quantized sector by sector: three uint8 cubes.

    Identical to quantizing :func:`compute_horizon_block`'s output (a test
    pins that), at a quarter of the peak memory because no float32 cube ever
    exists. This is what the sweep and its workers call.
    """
    row0, row1, col0, col1 = inner
    shape = (params.sectors, row1 - row0, col1 - col0)
    angles_q = np.empty(shape, dtype=np.uint8)
    blocker = np.empty(shape, dtype=np.uint8)
    angles_noveg_q = np.empty(shape, dtype=np.uint8)
    sectors = iter_horizon_sectors(dsm, dtm, landcover, resolution_m, params, inner, height_datum_m)
    for k, (sector_angles, sector_blocker, sector_noveg) in enumerate(sectors):
        angles_q[k] = quantize_angles(sector_angles)
        blocker[k] = sector_blocker
        angles_noveg_q[k] = quantize_angles(sector_noveg)
    return angles_q, blocker, angles_noveg_q


@dataclass(frozen=True)
class _SweepState:
    """Everything a tile needs, held module-global so ``fork`` inherits it.

    Worker arguments are pickled, so passing the rasters per task would ship
    tens of megabytes down a pipe for every tile and undo the entire point.
    Set once before the pool exists, the children inherit these arrays
    copy-on-write and read-only, and a job travels as four integers.
    """

    dsm: npt.NDArray[np.floating]
    dtm: npt.NDArray[np.floating]
    landcover: npt.NDArray[np.uint8]
    resolution_m: float
    params: HorizonParams
    pad: int
    inner: Window
    coverage: npt.NDArray[np.bool_] | None
    height_datum_m: float


_SWEEP: _SweepState | None = None


@contextmanager
def _sweep_state(state: _SweepState) -> Iterator[None]:
    global _SWEEP
    _SWEEP = state
    try:
        yield
    finally:
        _SWEEP = None


def tile_jobs(inner: Window, tile_size: int) -> list[Window]:
    """Split the inner window into tiles: a partition, in row-major order."""
    row0, row1, col0, col1 = inner
    return [
        (t0, min(t0 + tile_size, row1), u0, min(u0 + tile_size, col1))
        for t0 in range(row0, row1, tile_size)
        for u0 in range(col0, col1, tile_size)
    ]


def _tile_coverage(state: _SweepState, job: Window) -> npt.NDArray[np.bool_] | None:
    """The coverage mask cropped to ``job``, or None when the city is all covered."""
    if state.coverage is None:
        return None
    t0, t1, u0, u1 = job
    row0, _, col0, _ = state.inner
    return state.coverage[t0 - row0 : t1 - row0, u0 - col0 : u1 - col0]


def _sweep_tile(job: Window) -> SweptTile:
    """Sweep one tile against the inherited rasters; the unit of work.

    The same function on both paths -- called directly in serial, submitted to
    the pool in parallel -- so the two cannot drift apart. It reads and returns
    only; every write to an artifact happens in the parent.

    A tile the computation area only partly covers is swept **whole** and
    masked afterwards. Sweeping just the covered pixels would be faster, but
    the saving is a fringe and the cost is the invariant that matters: masking
    after the fact keeps the cubes identical for every ``tile_size``, so the
    tile size decides how much work is skipped and never what is written.
    """
    state = _SWEEP
    assert state is not None, "_sweep_tile called outside a _sweep_state block"
    t0, t1, u0, u1 = job
    rows, cols = state.dsm.shape
    p0, p1 = max(0, t0 - state.pad), min(rows, t1 + state.pad)
    q0, q1 = max(0, u0 - state.pad), min(cols, u1 + state.pad)
    angles_q, blocker, angles_noveg_q = quantized_horizon_block(
        state.dsm[p0:p1, q0:q1],
        state.dtm[p0:p1, q0:q1],
        state.landcover[p0:p1, q0:q1],
        state.resolution_m,
        state.params,
        (t0 - p0, t1 - p0, u0 - q0, u1 - q0),
        state.height_datum_m,
    )
    covered = _tile_coverage(state, job)
    if covered is not None and not covered.all():
        outside = ~covered
        angles_q[:, outside] = 0
        angles_noveg_q[:, outside] = 0
        blocker[:, outside] = NO_BLOCKER
    return job, angles_q, blocker, angles_noveg_q


def _sweep_parallel(jobs: list[Window], workers: int) -> Generator[SweptTile]:
    """Yield finished tiles as they land, out of order.

    ``fork`` is pinned on purpose: Python 3.14 defaults to ``forkserver``,
    which starts a fresh interpreter and would inherit no rasters at all. A
    dead worker (usually the OOM killer) ends the build loudly -- degrading to
    serial after nine hours would be worse than failing.
    """
    executor = ProcessPoolExecutor(
        max_workers=workers, mp_context=multiprocessing.get_context("fork")
    )
    try:
        futures = [executor.submit(_sweep_tile, job) for job in jobs]
        try:
            for future in as_completed(futures):
                yield future.result()
        except BrokenProcessPool as exc:
            raise RuntimeError(
                f"a horizon sweep worker died (of {workers}); the usual cause is the "
                "OOM killer -- retry with fewer --workers or a smaller --tile-size"
            ) from exc
    finally:
        # Cancel what is still queued and let the running tiles land, so the
        # scratch directory is torn down with nobody inside it.
        executor.shutdown(wait=True, cancel_futures=True)


def compute_horizon_tiled(
    dsm: npt.NDArray[np.floating],
    dtm: npt.NDArray[np.floating],
    landcover: npt.NDArray[np.uint8],
    resolution_m: float,
    params: HorizonParams,
    inner: Window | None = None,
    *,
    coverage: npt.NDArray[np.bool_] | None = None,
    scratch_dir: Path | None = None,
    height_datum_m: float | None = None,
    progress: Callable[[str], None] | None = None,
    events: EventSink | None = None,
) -> HorizonResult:
    """Sweep the ``inner`` window (default: everything) tile by tile.

    Each tile reads a window padded by ``buffer_pixels`` on every side
    (clipped at dataset edges), so results are independent of ``tile_size``;
    memory per tile stays bounded while cost stays proportional to inner
    pixels (buffer pixels are read, never swept).

    With ``scratch_dir`` the three output cubes live in memory-mapped files
    there instead of anonymous RAM. City-scale cubes are the build's largest
    allocation (64 x 7000 x 8000 uint8 ~= 3.35 GB apiece for Cordoba) and the
    access pattern is mmap-friendly -- written tile by tile, then read back
    band by band exactly once -- so file-backed pages let the kernel evict
    under pressure instead of OOMing. Results are bit-identical either way.

    ``params.workers`` above 1 spreads the tiles across processes. The write
    loop below is the same in both modes; only who produced the tile changes,
    which is why the cubes come out identical.

    ``coverage`` is the city's computation area as a mask over ``inner``
    (see shade-docs: learning/rasterizacion-de-poligonos.md). Tiles it does not
    reach at all are never swept -- that is where the saving comes from -- and
    the rest are swept whole and masked. Uncovered pixels end up at angle 0
    with class ``NO_BLOCKER``, which is the pair the artifact invariants
    expect: "nothing raises this sector" with nothing named as the raiser.
    They are *not* a claim that the sky is open there; ``coverage.tif`` is what
    tells readers there is no data, and readers that ignore it would report
    sunshine over a hole.

    ``height_datum_m`` defaults to :func:`height_datum` of the whole ``dtm``.
    Deriving it here and not per tile is what keeps the output independent of
    ``tile_size``; it comes back in the result so the build can record it,
    because a cube is only reproducible alongside its datum.
    """
    rows, cols = dsm.shape
    if inner is None:
        inner = (0, rows, 0, cols)
    row0, row1, col0, col1 = inner
    if height_datum_m is None:
        height_datum_m = height_datum(dtm)
    pad = buffer_pixels(params.max_distance_m, resolution_m)
    workers = max(1, params.workers)
    per_worker = estimate_sweep_worker_bytes(params.sectors, params.tile_size, pad)
    if workers > 1:
        # Before the pool exists, never after: an OOM at hour 9 of 12 is the
        # worst possible ending.
        check_worker_budget(workers, per_worker, hint="a smaller --tile-size")
    else:
        warn_if_serial_is_tight(per_worker, progress, hint="a smaller --tile-size")

    shape = (params.sectors, row1 - row0, col1 - col0)
    if scratch_dir is None:
        angles_q = np.empty(shape, dtype=np.uint8)
        blocker = np.empty(shape, dtype=np.uint8)
        angles_noveg_q = np.empty(shape, dtype=np.uint8)
    else:
        angles_q = np.memmap(scratch_dir / "angles_q.u8", dtype=np.uint8, mode="w+", shape=shape)
        blocker = np.memmap(scratch_dir / "blocker.u8", dtype=np.uint8, mode="w+", shape=shape)
        angles_noveg_q = np.memmap(
            scratch_dir / "angles_noveg_q.u8", dtype=np.uint8, mode="w+", shape=shape
        )
    jobs = tile_jobs(inner, params.tile_size)
    outside: list[Window] = []
    if coverage is not None:
        if coverage.shape != shape[1:]:
            raise ValueError(
                f"coverage mask is {coverage.shape}, expected {shape[1:]} for this inner window"
            )
        inside: list[Window] = []
        for job in jobs:
            t0, t1, u0, u1 = job
            patch = coverage[t0 - row0 : t1 - row0, u0 - col0 : u1 - col0]
            (inside if patch.any() else outside).append(job)
        jobs = inside
        # The cubes are allocated, not initialized, so the tiles nobody sweeps
        # still have to be written: the same "open sky, no blocker" pair a
        # swept-but-uncovered pixel gets.
        for t0, t1, u0, u1 in outside:
            out = (slice(None), slice(t0 - row0, t1 - row0), slice(u0 - col0, u1 - col0))
            angles_q[out] = 0
            angles_noveg_q[out] = 0
            blocker[out] = NO_BLOCKER
        if not jobs:
            raise ValueError("the computation area covers no pixel of this city's bbox")
    if progress is not None:
        skipped = f" ({len(outside)} outside the area)" if outside else ""
        if workers > 1:
            progress(f"sweeping {len(jobs)} tiles on {workers} workers{skipped}")
        else:
            progress(
                f"sweeping {len(jobs)} tiles serially{skipped} "
                f"({cpu_budget()} cores available; --workers N to parallelise)"
            )
    sweep_start = time.monotonic()
    state = _SweepState(
        dsm, dtm, landcover, resolution_m, params, pad, inner, coverage, height_datum_m
    )
    # Both producers are closeable generators, so `closing` tears the pool down
    # at a known point instead of whenever the collector gets round to it.
    producer: Generator[SweptTile] = (
        (_sweep_tile(job) for job in jobs) if workers == 1 else _sweep_parallel(jobs, workers)
    )
    with _sweep_state(state), closing(producer) as results:
        for done, (job, tile_angles_q, tile_blocker, tile_noveg_q) in enumerate(results, start=1):
            t0, t1, u0, u1 = job
            out = (slice(None), slice(t0 - row0, t1 - row0), slice(u0 - col0, u1 - col0))
            angles_q[out] = tile_angles_q
            blocker[out] = tile_blocker
            angles_noveg_q[out] = tile_noveg_q
            # Reported on completion, not on start: with N tiles in flight
            # there is no meaningful "current" one, and elapsed over completed
            # is throughput, which is what an ETA wants. Held back until the
            # first full batch has landed, because until then the elapsed time
            # covers tiles that have not finished and the estimate reads about
            # `workers` times too long.
            average = (time.monotonic() - sweep_start) / done
            eta = average * (len(jobs) - done) if done >= workers else None
            if progress is not None:
                line = f"swept tile [{done}/{len(jobs)}]"
                if eta is not None:
                    line += f" (avg {format_duration(average)}/tile, eta {format_duration(eta)})"
                progress(line)
            # Per tile rather than per phase: this is the four-hour step, and a
            # single event at the end of it would tell a console nothing while
            # it mattered. Hundreds of records over hours costs nothing.
            emit(
                events,
                "build",
                "tile",
                done=done,
                total=len(jobs),
                eta_s=None if eta is None else round(eta, 1),
            )
    # Push the scratch cubes through msync before anything reads them back:
    # flush() raises OSError on write-back failure, whereas a silently
    # dropped dirty page would resurface as zeroed sectors in the artifacts.
    for cube in (angles_q, blocker, angles_noveg_q):
        if isinstance(cube, np.memmap):
            cube.flush()
    return HorizonResult(
        angles_q=angles_q,
        blocker_class=blocker,
        angles_noveg_q=angles_noveg_q,
        height_datum_m=height_datum_m,
    )


def quantize_angles(angles_deg: npt.NDArray[np.float32]) -> npt.NDArray[np.uint8]:
    """Quantize [0, 90] degrees to uint8: step 90/255 ~= 0.353 deg.

    Round-to-nearest keeps the error unbiased and below ~0.18 deg -- far under
    the sweep's own half-pixel discretization. Dequantize on read with
    ``q * (ANGLE_MAX_DEG / 255)``; readers take the scale from the artifact's
    ``angle_max_deg`` tag so files stay self-describing.
    """
    scaled = np.round(angles_deg * (255.0 / ANGLE_MAX_DEG))
    return np.clip(scaled, 0.0, 255.0).astype(np.uint8)
