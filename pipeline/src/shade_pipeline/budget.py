"""What the machine can actually give the build, and what a sweep worker costs.

Parallel sweeps fail in one spectacular way: the OOM killer takes a worker at
hour 9 of 12 and the build dies with nothing to show. So the check happens
*before* the pool is created, never during, and it asks the right authority.

That authority is the cgroup, not the host. ``compose.yml`` runs the pipeline
service under ``mem_limit: 6g`` and ``cpus: 3``, and inside that container
``/proc/meminfo`` still reports the host's RAM while ``os.cpu_count()`` still
reports the host's cores -- a CPU quota is not an affinity mask. Reading the
cgroup v2 files is the only way to see the real budget; everything degrades to
the host view when they are absent (a laptop, a plain systemd scope).
"""

import os
from collections.abc import Callable
from pathlib import Path
from typing import Final

CGROUP_ROOT: Final = Path("/sys/fs/cgroup")
HEADROOM: Final = 0.8
"""Fraction of the available memory a sweep may plan to occupy.

The estimate below models the arrays the code allocates; it cannot model the
allocator's retention, the interpreter's own pages or whatever else shares the
box. One fifth in reserve is what turns a model into a guardrail.
"""


class MemoryBudgetError(ValueError):
    """The requested workers do not fit in the memory this machine can give."""


def _cgroup_dir() -> Path | None:
    """This process's cgroup v2 directory, or None outside cgroup v2.

    In a container ``/sys/fs/cgroup`` is already the namespaced root and the
    path is ``/``; on a normal host it is the long systemd slice path.
    """
    try:
        for line in Path("/proc/self/cgroup").read_text(encoding="utf-8").splitlines():
            hierarchy, _, rest = line.partition(":")
            if hierarchy == "0":  # v2 unified hierarchy: 0::<path>
                return CGROUP_ROOT / rest.partition(":")[2].lstrip("/")
    except OSError:
        return None
    return None


def _read_cgroup(name: str) -> str | None:
    """First readable ``name`` walking up from this cgroup towards the root.

    Controller files only exist where the parent enabled the controller, so a
    leaf frequently lacks ``memory.max`` while an ancestor holds the limit that
    actually applies.
    """
    directory = _cgroup_dir()
    while directory is not None:
        try:
            return (directory / name).read_text(encoding="utf-8").strip()
        except OSError:
            pass
        if directory == CGROUP_ROOT or CGROUP_ROOT not in directory.parents:
            return None
        directory = directory.parent
    return None


def _meminfo_available() -> int | None:
    """MemAvailable from /proc/meminfo, in bytes (the host's view)."""
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) * 1024
    except OSError, ValueError, IndexError:
        return None
    return None


def _cgroup_available() -> int | None:
    """Room left in this cgroup's memory limit, in bytes; None if unlimited.

    ``memory.current`` counts the page cache, and this runs right after binning
    has streamed gigabytes of LAZ through it. That cache is reclaimable, so
    subtracting ``memory.stat``'s ``file`` is what keeps the check from
    refusing perfectly sound runs.
    """
    limit = _read_cgroup("memory.max")
    if limit is None or limit == "max":
        return None
    try:
        total = int(limit)
        used = int(_read_cgroup("memory.current") or 0)
    except ValueError:
        return None
    cache = 0
    for line in (_read_cgroup("memory.stat") or "").splitlines():
        key, _, value = line.partition(" ")
        if key == "file":
            try:
                cache = int(value)
            except ValueError:
                cache = 0
            break
    return max(0, total - max(0, used - cache))


def available_bytes() -> int | None:
    """Memory the build may plan to use, or None when nothing is readable.

    The minimum of what the cgroup allows and what the kernel says is free:
    either can be the binding constraint, and being wrong in the optimistic
    direction is what the OOM killer punishes.
    """
    readable = [value for value in (_cgroup_available(), _meminfo_available()) if value is not None]
    return min(readable) if readable else None


def cpu_budget() -> int:
    """Cores this process may actually use: cgroup quota first, affinity after.

    ``cpus: 3`` in compose is a CFS quota (``"300000 100000"`` = 3 cores), and
    a quota is invisible to both ``os.cpu_count()`` and the affinity mask.
    Informational only -- it names a number in the progress line, it never
    decides how many workers run.
    """
    quota = _read_cgroup("cpu.max")
    if quota is not None and not quota.startswith("max"):
        try:
            allowance, period = (int(part) for part in quota.split())
            return max(1, allowance // period)
        except ValueError:
            pass
    return len(os.sched_getaffinity(0))


def estimate_sweep_worker_bytes(sectors: int, tile_size: int, pad_px: int) -> int:
    """Peak memory of one horizon-sweep worker, deliberately over-estimated.

    Three terms, each an allocation in the tile's call tree: the three uint8
    output cubes it returns, the two float64 surfaces it derives from the
    padded window, and the per-sector accumulators plus the ufunc temporaries
    the inner loop churns through. The parent holds one finished tile per
    worker while it writes them into the memmap, so the returned cubes are
    counted once more. Measured against a real tile (512 px, 64 sectors, 500 px
    of pad) the model says 162 MB where the process grew 87 MiB -- erring high
    is the whole point.
    """
    window = tile_size + 2 * pad_px
    cubes = 3 * sectors * tile_size * tile_size
    surfaces = 2 * window * window * 8
    working = 12 * tile_size * tile_size * 8
    in_flight = cubes  # the copy the parent holds while filing it away
    return cubes + surfaces + working + in_flight


TILES_BYTES_PER_PIXEL: Final = 26
"""Peak bytes per raster pixel while one instant renders.

Four float32 fields (the two horizon margins and the two signed distances)
plus two uint8 labels is 18 bytes; the rest is the float64 that
``distance_transform_edt`` insists on, held one at a time. Nothing here scales
with zoom: the tile grid never exists as an array, only 256 px windows do.
"""
TILES_BASE_BYTES: Final = 360 * 1024 * 1024
"""What one render worker costs before any city-sized array exists.

Interpreter, numpy, rasterio and GDAL are ~180 MiB of it, and GDAL's block
cache is the rest -- pinned by ``tiles.GDAL_CACHE_MB`` rather than left at its
default of 5% of physical RAM, precisely so this stays a constant instead of a
fraction of whatever machine is running. Neither term grows with the zoom: the
tile grid never exists as an array, only 256 px windows do.
"""


def estimate_tiles_worker_bytes(rows: int, cols: int) -> int:
    """Peak memory of one tile-render worker: one instant's rasters plus the base.

    Unlike the sweep, whose footprint is set by knobs, this one is fixed by the
    city: an instant holds whole-raster arrays and nothing chunks them. That is
    why the tile phase is bound by RAM and the sweep by cores.
    """
    return TILES_BYTES_PER_PIXEL * rows * cols + TILES_BASE_BYTES


def workers_that_fit(per_worker_bytes: int) -> int | None:
    """How many workers of that size the budget allows; None when unreadable.

    The same arithmetic :func:`check_worker_budget` enforces, exposed as a
    number so a planner can print it before anything is built.

    **Zero is a real answer.** A city big enough that one worker does not fit
    exists (a metropolitan area at 1 m/px puts tens of GiB in a single
    instant), and flooring this at 1 would report that case as "one fits",
    which is the opposite of the truth and the exact number a planner acts on.
    """
    available = available_bytes()
    if available is None:
        return None
    return int(available * HEADROOM) // per_worker_bytes


def warn_if_serial_is_tight(
    per_worker_bytes: int, say: Callable[[str], None] | None, hint: str = ""
) -> None:
    """Say so when even a serial run looks too big for this machine.

    Deliberately a warning and not a refusal. The estimates err high by design
    and the kernel has swap, so a serial run that the model dislikes may still
    finish; refusing would take away the last escape hatch on a tight box. What
    is not acceptable is silence: without this, a city whose single unit of
    work needs tens of GiB runs until the OOM killer ends it, with no warning
    that it was ever going to.
    """
    if say is None:
        return
    available = available_bytes()
    if available is None or per_worker_bytes <= int(available * HEADROOM):
        return
    say(
        f"warning: one unit of work needs about {per_worker_bytes / 2**30:.1f} GiB and only "
        f"{available / 2**30:.1f} GiB is available; this may be killed by the OOM killer{hint}"
    )


def check_worker_budget(workers: int, per_worker_bytes: int, hint: str = "") -> None:
    """Raise unless ``workers`` processes of that size fit in the memory available.

    Silence means either the run fits or the machine would not say -- an
    unreadable budget is not a reason to block a build that may well be fine.
    ``hint`` names whatever else the caller can turn down, as a bare phrase
    ("a smaller --tile-size"): the wording around it differs depending on
    whether lowering ``--workers`` is still an option at all.
    """
    available = available_bytes()
    if available is None:
        return
    budget = int(available * HEADROOM)
    if workers * per_worker_bytes <= budget:
        return
    fits = budget // per_worker_bytes
    if fits >= 1:
        advice = f"use --workers {fits} or fewer" + (f", or {hint}" if hint else "")
    else:
        # Nothing to turn down: one unit of work does not fit on this machine.
        # Saying "--workers 0 or fewer" would be nonsense, and saying
        # "--workers 1" would send the caller straight into the OOM killer.
        advice = "not even one worker fits; " + (
            f"try {hint}" if hint else "this needs a bigger machine"
        )
    raise MemoryBudgetError(
        f"--workers {workers} needs about {workers * per_worker_bytes / 2**30:.1f} GiB "
        f"({per_worker_bytes / 2**30:.1f} GiB each) but only {available / 2**30:.1f} GiB is "
        f"available; {advice}"
    )
