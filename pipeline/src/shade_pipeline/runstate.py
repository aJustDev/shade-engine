"""Where each city stands in the chain, kept on disk so nothing has to remember.

Building a city is five commands, two of which run for hours, and until now the
only record that any of them had happened was a shell scrollback and a handful
of log files with names like ``data/tiles-montilla.log``. That is fine while you
are watching and useless the next morning, which is how a rebuild ends up half
done with nobody able to say which half.

So each city gets a small state file listing, per step, whether it ran, when,
for how long, with which parameters, and against which configuration. Two things
follow from that. A supervisor can resume a chain instead of restarting it, and
a console can *show* the state without owning the processes that produce it --
which is what lets a six-hour render outlive the window it was launched from.

Staleness is derived, never stored. A step is stale when the city's
configuration has changed since it ran, or when an earlier step in the chain has
finished more recently than it did: both mean "this was computed from something
that has since moved". That converts a warning which today is only prose in
``shade-docs: ops/anadir-ciudad.md`` ("changing the bbox or the area invalidates
the artifacts") into something the tooling can act on.
"""

import hashlib
import os
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Self

from pydantic import BaseModel, Field

from shade_core.config import load_city

RUNS_DIRNAME = "runs"
STATE_FILENAME = "state.json"
LATEST_DIRNAME = "latest"
"""Stable names for the newest run of each step, alongside the stamped files.

A run's own log is stamped so a failed attempt survives the retry that replaced
it, which is right and makes every path unguessable: five ``publish-*.log`` in a
directory and nothing says which one is today's. ``latest/publish.log`` is a
symlink that always does, and a stable path is what you can hand to somebody --
or to an agent -- without pasting a screenful of terminal.
"""

KEEP_RUNS = 10
"""How many stamped runs of a step to keep before pruning the oldest.

Logs are small (a four-hour sweep writes a few dozen KB) so this is generous on
purpose; it exists so a directory that is written to every rebuild does not grow
without end, not to save space.
"""

HISTORY_FILENAME = "history.jsonl"
"""Every run this city has ever had, one line each, appended and never pruned.

Separate from ``state.json``, which holds only the *current* record of each step
because that is all staleness needs -- and which therefore forgets. A build that
failed and was re-run leaves its log on disk and no trace at all in the state:
duration, parameters and error were overwritten by the attempt that replaced it.

Separate from the logs too, and that is the point of it. A tile log is tens of
kilobytes and gets pruned; a line here is two hundred bytes and is kept, so
*that* something happened outlives the detail of what it printed.
"""

DEPENDS_ON: dict[str, tuple[str, ...]] = {
    "area": (),
    "basemap": (),
    "build": (),
    "graph": ("build",),
    "tiles": ("build",),
    "publish": ("basemap", "build", "tiles"),
}
"""What each step is computed *from*, which is what makes it stale.

Deliberately a tree and not the running order. ``graph`` and ``tiles`` both read
the rasters and neither reads the other, so re-running the pedestrian graph must
not mark a perfectly good tile pyramid as out of date. ``area`` produces nothing
at all -- it prices the city -- so nothing depends on it; a configuration change
is caught by the digest instead, which applies to every step at once.

``basemap`` reads nothing of ours either: it cuts a city out of a planet build
by bbox, so a rebuild of the rasters leaves it perfectly valid and a change to
the bbox invalidates it through the digest, like everything else. ``publish``
depends on it because publishing is what puts it in front of somebody.
"""


LOG_STEPS: tuple[str, ...] = (*DEPENDS_ON, "preview")
"""Everything that writes a log, which is more than the chain.

``preview`` runs two servers and produces no artifact, so it has no place in
:data:`DEPENDS_ON` and no column in ``status`` -- nothing can be stale because of
it. It still has plenty to say while it runs, and that has to be findable.
"""

CHAIN: tuple[str, ...] = ("area", "basemap", "build", "graph", "tiles", "publish")
"""The order steps run in. ``publish`` is in the chain but gated.

Here and not in :mod:`shade_pipeline.runner`, which is where it used to live and
which is where it is *used*: naming the steps and running them are different
weights. Importing ``runner`` pulls in pyproj, shapely and rasterio -- close to
a second -- and the console needs nothing more than these six strings to draw
its first table. ``runner`` re-exports both names, so nothing else changed.
"""

UNATTENDED: tuple[str, ...] = ("area", "basemap", "build", "graph", "tiles")
"""How far ``run`` goes on its own."""


class StepStatus(StrEnum):
    """What a step's own record says. The *effective* status may still be stale."""

    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    STALE = "stale"
    """Never stored: only ever returned by :meth:`CityState.status`."""


class StepRecord(BaseModel):
    """One step's last run."""

    status: StepStatus = StepStatus.PENDING
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration_s: float | None = None
    config_digest: str | None = None
    pid: int | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    log: str | None = None
    events: str | None = None
    error: str | None = None

    @property
    def is_alive(self) -> bool:
        """True when this step claims to be running and its process still exists.

        A crashed or killed supervisor leaves ``RUNNING`` behind for ever, and a
        console that believed it would wait on a job nobody is doing. Signal 0
        asks the kernel whether the pid is there without touching it.
        """
        if self.status is not StepStatus.RUNNING or self.pid is None:
            return False
        try:
            os.kill(self.pid, 0)
        except ProcessLookupError, PermissionError:
            return False
        except OSError:
            return False
        return True


class RunRecord(BaseModel):
    """One finished run, for the history. Not a state: a thing that happened.

    Which is why ``step`` is a field rather than a key, and why nothing here is
    ever updated in place. ``status`` is the outcome of the run itself, so an
    ``unpublish`` reads ``done`` even though it leaves its step pending -- the
    run succeeded, and what the step is *now* is state.json's business.
    """

    step: str
    status: StepStatus
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration_s: float | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    log: str | None = None
    error: str | None = None

    @classmethod
    def of(cls, step: str, entry: StepRecord, status: StepStatus) -> Self:
        return cls(
            step=step,
            status=status,
            started_at=entry.started_at,
            finished_at=entry.finished_at,
            duration_s=entry.duration_s,
            params=dict(entry.params),
            log=entry.log,
            error=entry.error,
        )


class CityState(BaseModel):
    """Every step of one city, plus the configuration they were run against."""

    city: str
    config_digest: str = ""
    steps: dict[str, StepRecord] = Field(default_factory=dict)
    preferences: dict[str, Any] = Field(default_factory=dict)
    """What was chosen last time this city was launched from the console.

    Kept next to the state rather than in a file of its own: it is per city, it
    is small, and it is worthless without the state it sits beside. Nothing
    reads it except the launch dialog, which uses it so a rebuild is one key
    instead of eight flags typed again."""

    def record(self, step: str) -> StepRecord:
        return self.steps.get(step, StepRecord())

    def status(self, step: str) -> StepStatus:
        """The step's status with staleness applied.

        Order matters here. A step that died with its supervisor reports
        ``FAILED`` rather than ``RUNNING``, because a stale ``RUNNING`` is the
        one state a resumer must never trust. Only a genuinely finished step can
        then be judged stale.
        """
        entry = self.record(step)
        if entry.status is StepStatus.RUNNING:
            return StepStatus.RUNNING if entry.is_alive else StepStatus.FAILED
        if entry.status is not StepStatus.DONE:
            return entry.status
        if entry.config_digest is not None and entry.config_digest != self.config_digest:
            return StepStatus.STALE
        if entry.finished_at is not None and self._outdated_by(step, entry.finished_at):
            return StepStatus.STALE
        return entry.status

    def _outdated_by(self, step: str, finished_at: datetime) -> str | None:
        """The dependency that has run since ``step`` did, if any."""
        for source in DEPENDS_ON.get(step, ()):
            other = self.record(source)
            if (
                other.status is StepStatus.DONE
                and other.finished_at is not None
                and other.finished_at > finished_at
            ):
                return source
        return None

    def stale_reason(self, step: str) -> str | None:
        """Why :meth:`status` calls a finished step stale, for the message a user reads."""
        entry = self.record(step)
        if self.status(step) is not StepStatus.STALE:
            return None
        if entry.config_digest != self.config_digest:
            return "the city configuration changed since it ran"
        if entry.finished_at is not None:
            source = self._outdated_by(step, entry.finished_at)
            if source is not None:
                return f"{source} has run again since"
        return "its inputs moved"


def config_digest(cities_dir: Path, city: str) -> str:
    """Fingerprint of everything that decides what a build produces.

    The YAML and, when it declares one, the computation area polygon: between
    them they fix the bbox, the resolution, the sweep and which pixels get
    computed at all. A missing area file is not an error here -- ``build``
    reports that properly, and this only has to notice change.
    """
    digest = hashlib.sha256()
    path = cities_dir / f"{city}.yaml"
    digest.update(path.read_bytes())
    config = load_city(path)
    if config.area is not None:
        area = Path(config.area)
        if area.exists():
            digest.update(area.read_bytes())
    return digest.hexdigest()


class RunState:
    """Read/modify/write access to one city's state file.

    Every mutation writes the whole file immediately and atomically. The file is
    tiny, the events are rare (a handful per multi-hour run), and the reader is
    a console polling from another process -- which must never catch a
    half-written file, and must see a step's start the moment it happens.
    """

    def __init__(self, directory: Path, city: str, digest: str = "") -> None:
        self.directory = directory
        self.city = city
        self.path = directory / STATE_FILENAME
        self.state = self._read(city)
        if digest:
            self.state.config_digest = digest

    @classmethod
    def open(cls, city: str, *, cities_dir: Path, data_root: Path) -> Self:
        """State for ``city``, with its configuration fingerprinted as of now."""
        return cls(
            data_root / RUNS_DIRNAME / city,
            city,
            config_digest(cities_dir, city),
        )

    def _read(self, city: str) -> CityState:
        try:
            return CityState.model_validate_json(self.path.read_text(encoding="utf-8"))
        except OSError, ValueError:
            return CityState(city=city)

    def save(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        scratch = self.path.with_name(self.path.name + ".tmp")
        scratch.write_text(self.state.model_dump_json(indent=2) + "\n", encoding="utf-8")
        scratch.replace(self.path)

    def paths_for(self, step: str) -> tuple[Path, Path]:
        """Where this step's next run should write its human log and its events.

        ``<city>/<step>/<stamp>.log``: stamped with the start time rather than
        overwritten, so a failed attempt is still readable after the retry that
        replaced it, and filed under its step so the directory reads as a
        history instead of a heap. ``latest/`` gets a symlink under a stable
        name, and runs beyond :data:`KEEP_RUNS` are pruned.
        """
        self._migrate_flat()
        self._seed_history()
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
        (self.directory / step).mkdir(parents=True, exist_ok=True)
        paths = (self.directory / step / f"{stamp}.log", self.directory / step / f"{stamp}.jsonl")
        for path in paths:
            self._point_latest_at(step, path)
        self._prune(step)
        return paths

    def newest_run(self, step: str, suffix: str = ".log") -> Path | None:
        """This step's most recent run file, by name.

        Globbed rather than read from the state file, because the state file
        only knows the last run and this has to work for a city whose logs
        predate it. The stamp sorts chronologically, so the newest is the last.
        """
        runs = sorted((self.directory / step).glob(f"*{suffix}"))
        return runs[-1] if runs else None

    def refresh_latest(self) -> None:
        """Tidy the layout and rebuild every ``latest/`` link from what is on disk.

        Links are made when a step starts, which leaves nothing for the cities
        built before this existed, and nothing correct if somebody deletes a run
        by hand. Cheap enough to do on every read.
        """
        self._migrate_flat()
        self._seed_history()
        for step in LOG_STEPS:
            for suffix in (".log", ".jsonl"):
                newest = self.newest_run(step, suffix)
                if newest is not None:
                    self._point_latest_at(step, newest)

    def _migrate_flat(self) -> None:
        """Move the old flat ``<step>-<stamp>.log`` files into ``<step>/``.

        Done here rather than in a migration script because the only thing that
        knows a runs directory exists is whatever is about to write to it. The
        state file points at the paths by name, so they are rewritten too --
        otherwise the console would go on following a log that had moved.
        """
        moved: dict[str, str] = {}
        for path in sorted(self.directory.glob("*-*.log")) + sorted(
            self.directory.glob("*-*.jsonl")
        ):
            step, _, stamp = path.stem.partition("-")
            if step not in LOG_STEPS or not stamp:
                continue
            target = self.directory / step / f"{stamp}{path.suffix}"
            target.parent.mkdir(parents=True, exist_ok=True)
            path.replace(target)
            moved[str(path)] = str(target)
        if not moved:
            return
        for entry in self.state.steps.values():
            entry.log = moved.get(entry.log or "", entry.log)
            entry.events = moved.get(entry.events or "", entry.events)
        self.save()

    def _point_latest_at(self, step: str, path: Path) -> None:
        """Repoint ``latest/<step>.<ext>`` at ``path``, best effort.

        Relative targets, so the whole ``data/`` tree stays movable. Failure is
        swallowed: a filesystem without symlinks is a reason to lose a
        convenience, never a reason to lose a build.
        """
        link = self.directory / LATEST_DIRNAME / f"{step}{path.suffix}"
        try:
            link.parent.mkdir(parents=True, exist_ok=True)
            link.unlink(missing_ok=True)
            link.symlink_to(Path("..") / step / path.name)
        except OSError:
            pass

    def _prune(self, step: str) -> None:
        """Delete this step's oldest runs, keeping the newest :data:`KEEP_RUNS`.

        Only the files. The history line for a pruned run stays, which is the
        whole reason the two are kept apart: you lose what it printed, never
        that it happened.
        """
        for suffix in (".log", ".jsonl"):
            runs = sorted((self.directory / step).glob(f"*{suffix}"))
            for stale in runs[:-KEEP_RUNS]:
                stale.unlink(missing_ok=True)

    @property
    def history_path(self) -> Path:
        return self.directory / HISTORY_FILENAME

    def history(self) -> list[RunRecord]:
        """Every run recorded for this city, oldest first.

        A malformed line is skipped rather than fatal: this is a record to read,
        and half of it is worth more than an exception.
        """
        records: list[RunRecord] = []
        try:
            text = self.history_path.read_text(encoding="utf-8")
        except OSError:
            return records
        for line in text.splitlines():
            if not line.strip():
                continue
            try:
                records.append(RunRecord.model_validate_json(line))
            except ValueError:
                continue
        return records

    def _seed_history(self) -> None:
        """Start a city's history from what the state file still remembers.

        Once, for cities that were built before there was a history. It can only
        recover one run per step -- the last, which is all ``state.json`` keeps
        -- so it is a floor and not the truth; everything after this is real.
        """
        if self.history_path.exists():
            return
        seeded = [
            RunRecord.of(step, entry, entry.status)
            for step, entry in self.state.steps.items()
            if entry.started_at is not None
        ]
        if not seeded:
            return
        seeded.sort(key=lambda entry: entry.started_at or datetime.min.replace(tzinfo=UTC))
        self.directory.mkdir(parents=True, exist_ok=True)
        with self.history_path.open("w", encoding="utf-8") as handle:
            for entry in seeded:
                handle.write(entry.model_dump_json() + "\n")

    def _remember_run(self, step: str, status: StepStatus) -> None:
        """Append what just happened. Append-only, and never rewritten."""
        record = RunRecord.of(step, self.record(step), status)
        self.directory.mkdir(parents=True, exist_ok=True)
        with self.history_path.open("a", encoding="utf-8") as handle:
            handle.write(record.model_dump_json() + "\n")

    def begin(
        self, step: str, *, params: dict[str, Any] | None = None, log: Path, events: Path
    ) -> None:
        self.state.steps[step] = StepRecord(
            status=StepStatus.RUNNING,
            started_at=datetime.now(UTC),
            config_digest=self.state.config_digest,
            pid=os.getpid(),
            params=params or {},
            log=str(log),
            events=str(events),
        )
        self.save()

    def complete(self, step: str, *, digest: str | None = None) -> None:
        """Mark a step finished, re-fingerprinting the config if it changed it.

        ``area --write`` edits the very YAML the digest is taken over, so
        recording the digest from *before* it ran would leave the step
        permanently stale the instant it succeeded.
        """
        entry = self.record(step)
        finished = datetime.now(UTC)
        if digest is not None:
            self.state.config_digest = digest
        self.state.steps[step] = entry.model_copy(
            update={
                "status": StepStatus.DONE,
                "finished_at": finished,
                "duration_s": (
                    None
                    if entry.started_at is None
                    else round((finished - entry.started_at).total_seconds(), 1)
                ),
                "config_digest": self.state.config_digest,
                "pid": None,
                "error": None,
            }
        )
        self.save()
        self._remember_run(step, StepStatus.DONE)

    def fail(self, step: str, error: str) -> None:
        entry = self.record(step)
        self.state.steps[step] = entry.model_copy(
            update={
                "status": StepStatus.FAILED,
                "finished_at": datetime.now(UTC),
                "pid": None,
                # Truncated: a traceback belongs in the log this points at, and
                # the state file is read on every console refresh.
                "error": error[:500],
            }
        )
        self.save()
        self._remember_run(step, StepStatus.FAILED)

    def undo(self, step: str) -> None:
        """Put a step back to pending because its result was removed, not redone.

        Unpublishing a city leaves ``publish`` claiming a success whose result no
        longer exists, and neither ``fail`` (it did not fail) nor a fresh
        ``begin`` (nothing is running) says that.

        The record is kept rather than dropped, with its log: the console follows
        the newest log across the chain, and deleting the record here would hide
        the log of the very thing that just ran. ``status`` returns a stored
        ``PENDING`` untouched, so the table reads as it should.
        """
        entry = self.record(step)
        self.state.steps[step] = entry.model_copy(
            update={
                "status": StepStatus.PENDING,
                "duration_s": None,
                "config_digest": None,
                "pid": None,
                "error": None,
            }
        )
        self.save()
        # The run itself succeeded -- it removed the city -- even though what it
        # leaves behind is a pending step. History records runs, not states.
        self._remember_run(step, StepStatus.DONE)

    def record(self, step: str) -> StepRecord:
        return self.state.record(step)

    def status(self, step: str) -> StepStatus:
        return self.state.status(step)

    def stale_reason(self, step: str) -> str | None:
        return self.state.stale_reason(step)

    def remember(self, preferences: dict[str, Any]) -> None:
        """Keep the launch options this city was last started with."""
        self.state.preferences = dict(preferences)
        self.save()

    @property
    def preferences(self) -> dict[str, Any]:
        return self.state.preferences
