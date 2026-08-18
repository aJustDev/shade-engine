"""Deleting a city's local artifacts, and saying what that costs before it does.

The local counterpart of :func:`shade_pipeline.publish.plan_unpublish`, and
built the same way: a plan you can read in full, then an execution that does
exactly what the plan said. There was no way to do this at all -- the way to
start a city over was ``rm -rf`` typed by hand next to a directory holding two
gigabytes of LiDAR you did not mean to touch.

**Why a rebuild is not enough on its own.** A build overwrites what it writes
and leaves everything else. Take ``area:`` out of a city's YAML and
``coverage.tif`` stays behind: :func:`shade_core.artifacts.load_coverage` finds
it and the API goes on masking pixels the new build does compute. Same for
``tree_inventory.tif`` when an inventory is withdrawn, and for the archives of
instants that have left the declination ladder. So "from scratch" means
removing the directory, not passing another flag.

**What it never touches, and why that is in the plan rather than in a comment.**
The LiDAR cache (``data/lidar/<city>``, most of a gigabyte a city) is the
expensive thing to get back, and it is not derived from anything this repository
does -- it is somebody else's download. The run directory (``data/runs/<city>``)
holds the state file, the logs and ``history.jsonl``, which is the record that a
run happened at all: keeping it is the whole point of [[ADR-024]]. Both are
listed as kept, with their size, so the decision reads as deliberate instead of
as an omission.
"""

import shutil
from dataclasses import dataclass, field
from pathlib import Path

from shade_pipeline.progress import format_bytes
from shade_pipeline.runstate import RunState, StepStatus

PURGED_STEPS: tuple[str, ...] = ("basemap", "build", "graph", "tiles")
"""The steps whose product lives in the artifact directory.

Not ``publish``: the server goes on serving what it was given, and moving that
step would be a lie about a machine this command cannot reach. Not ``area``
either -- it produces no file, it prices the city.
"""


def directory_size(path: Path) -> int:
    """Bytes on disk under ``path``, symlinks not followed."""
    if path.is_file():
        return path.stat().st_size
    total = 0
    for entry in path.rglob("*"):
        if entry.is_file() and not entry.is_symlink():
            total += entry.stat().st_size
    return total


@dataclass(frozen=True)
class PurgeItem:
    """One thing the plan has an opinion about, and its size."""

    path: Path
    size: int

    def render(self) -> str:
        return f"{format_bytes(self.size):>10}  {self.path}"


@dataclass
class PurgePlan:
    """What would go, what would stay, and which steps go back to pending."""

    city: str
    artifact_dir: Path
    removed: list[PurgeItem] = field(default_factory=list)
    kept: list[PurgeItem] = field(default_factory=list)
    steps: list[str] = field(default_factory=list)
    cached_lidar: bool = False
    """Whether a LiDAR cache was found at all, which changes what to say."""

    @property
    def freed(self) -> int:
        return sum(item.size for item in self.removed)

    def render(self) -> str:
        lines = [f"{self.city}: {format_bytes(self.freed)} to delete under {self.artifact_dir}", ""]
        if not self.removed:
            lines.append("  nothing built here")
        for item in self.removed:
            lines.append(f"  {item.render()}")
        lines += ["", "kept:"]
        for item in self.kept:
            lines.append(f"  {item.render()}")
        if not self.cached_lidar:
            # Said even when nothing is found, because the sentence is about
            # the rule and not about this directory: which path holds a city's
            # LAZ depends on how the build was launched (--lidar-dir, or a
            # --cache-dir the state file only remembers if the console set it),
            # and "no line about LiDAR" must never read as "it was deleted".
            lines.append("  no LiDAR cache found; wherever it is, this never touches it")
        if self.steps:
            lines += ["", f"back to pending: {', '.join(self.steps)}"]
        return "\n".join(lines)


def plan_purge(
    city: str,
    *,
    output_root: Path,
    data_root: Path,
    lidar_root: Path = Path("data/lidar"),
    artifact_version: str = "v1",
    state: RunState | None = None,
) -> PurgePlan:
    """Everything ``purge`` would do to ``city``, priced, without doing any of it.

    The removals are the top-level entries of the artifact directory rather than
    a hardcoded list of filenames: a directory that has collected something this
    version does not know how to write is exactly the case worth seeing.
    """
    artifact_dir = output_root / city / artifact_version
    plan = PurgePlan(city=city, artifact_dir=artifact_dir)
    if artifact_dir.exists():
        for entry in sorted(artifact_dir.iterdir()):
            plan.removed.append(PurgeItem(entry, directory_size(entry)))
    remembered = (state.preferences.get("cache_dir") if state is not None else None) or ""
    caches = [lidar_root / city] + ([Path(remembered)] if remembered else [])
    for cache in caches:
        if cache.exists():
            plan.cached_lidar = True
            plan.kept.append(PurgeItem(cache, directory_size(cache)))
    runs = data_root / "runs" / city
    if runs.exists():
        plan.kept.append(PurgeItem(runs, directory_size(runs)))
    if state is not None:
        plan.steps = [
            step for step in PURGED_STEPS if state.record(step).status is not StepStatus.PENDING
        ]
    return plan


def execute_purge(plan: PurgePlan, state: RunState) -> None:
    """Delete the artifact directory and put its steps back to pending.

    ``undo`` and not ``fail``: nothing failed. The steps ran, and what they
    produced has been removed, which is the state the method was written for
    when a city was unpublished -- it keeps the record and its log, so the run
    that built the thing is still findable after the thing is gone.
    """
    shutil.rmtree(plan.artifact_dir, ignore_errors=True)
    for step in PURGED_STEPS:
        if state.record(step).status is not StepStatus.PENDING:
            state.undo(step)
