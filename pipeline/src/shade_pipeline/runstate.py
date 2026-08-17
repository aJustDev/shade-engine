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

DEPENDS_ON: dict[str, tuple[str, ...]] = {
    "area": (),
    "build": (),
    "graph": ("build",),
    "tiles": ("build",),
    "publish": ("build", "tiles"),
}
"""What each step is computed *from*, which is what makes it stale.

Deliberately a tree and not the running order. ``graph`` and ``tiles`` both read
the rasters and neither reads the other, so re-running the pedestrian graph must
not mark a perfectly good tile pyramid as out of date. ``area`` produces nothing
at all -- it prices the city -- so nothing depends on it; a configuration change
is caught by the digest instead, which applies to every step at once.
"""


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

        Stamped with the start time rather than overwritten, so a failed attempt
        is still readable after the retry that replaced it.
        """
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
        self.directory.mkdir(parents=True, exist_ok=True)
        return (self.directory / f"{step}-{stamp}.log", self.directory / f"{step}-{stamp}.jsonl")

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
