"""Starting work from the console, and following it without owning it.

Everything the console launches leaves its process group immediately, so the
console is never on the critical path of a six-hour render: closing it, or
losing the ssh session it runs over, changes nothing. What comes back is a pid
and a promise that the state file will say the rest.

Following is the mirror image. Progress is read from the step's event stream
rather than scraped from its prose, and only the tail of it: a four-hour sweep
writes hundreds of records and the console asks every couple of seconds, so
re-reading the whole file each time would be work for nothing.
"""

import shutil
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from shade_pipeline.events import Event
from shade_pipeline.progress import format_duration
from shade_pipeline.runstate import RunState

TAIL_BYTES = 16 * 1024
"""How much of an event file to read looking for the latest progress.

Comfortably more than the longest few records, and small enough that polling it
every couple of seconds costs nothing.
"""


@dataclass(frozen=True)
class Progress:
    """How far a running step has got, as its own events report it."""

    done: int
    total: int
    eta_s: float | None
    label: str

    @property
    def fraction(self) -> float:
        return self.done / self.total if self.total else 0.0

    def describe(self) -> str:
        text = f"{self.label} {self.done}/{self.total}"
        if self.eta_s is not None:
            text += f", eta {format_duration(self.eta_s)}"
        return text


def engine_argv(*arguments: str) -> list[str]:
    """A command line for this same engine.

    ``sys.argv[0]`` is the ``shade-engine`` entry point that started the
    console, so a job runs the exact installation the console came from --
    which matters in a workspace where a checkout and a virtualenv can disagree.
    """
    return [sys.argv[0], *arguments]


def launch(argv: Sequence[str]) -> int:
    """Start a command detached from this process; returns its pid.

    ``start_new_session`` is ``setsid``: the child leaves the console's process
    group, so a terminal that closes cannot signal it.
    """
    child = subprocess.Popen(
        list(argv),
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return child.pid


def copy_to_system_clipboard(text: str) -> bool:
    """Put ``text`` on the clipboard through a helper the terminal cannot veto.

    Textual copies with OSC 52, which is a request to the terminal emulator and
    can be declined -- by ssh without a multiplexer, by tmux with
    ``set-clipboard off``, by a terminal that simply does not do it -- and the
    refusal is silent. Under WSL there is a second route that does not go
    through the terminal at all, and taking it is the difference between a
    traceback landing in the other window and a message that says it did.

    Returns whether that route was there and worked; the caller still tries
    OSC 52, since outside WSL it is all there is.
    """
    helper = shutil.which("clip.exe")
    if helper is None:
        return False
    try:
        subprocess.run(
            [helper],
            input=text.encode("utf-8", errors="replace"),
            check=True,
            timeout=5,
        )
    except OSError, subprocess.SubprocessError:
        return False
    return True


def _tail_events(path: Path) -> list[Event]:
    """The last few complete records of an event file, oldest first."""
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            handle.seek(max(0, size - TAIL_BYTES))
            chunk = handle.read()
    except OSError:
        return []
    # The first line is very likely cut in half by the seek, and the last may
    # be cut in half by a writer mid-append; both are dropped.
    lines = chunk.decode("utf-8", errors="replace").split("\n")
    events: list[Event] = []
    for line in lines[1:-1] if len(lines) > 2 else []:
        try:
            events.append(Event.from_json(line))
        except ValueError, KeyError:
            continue
    return events


PROGRESS_LABELS = {"tile": "sweep tiles", "unit": "render units"}
"""Event kinds that carry a count, and what that count is of."""


def progress_of(state: RunState, step: str) -> Progress | None:
    """The latest progress the step's events report, or None if there is none yet."""
    recorded = state.record(step).events
    if not recorded:
        return None
    for event in reversed(_tail_events(Path(recorded))):
        if event.kind not in PROGRESS_LABELS:
            continue
        done = event.payload.get("done")
        total = event.payload.get("total")
        if not isinstance(done, int) or not isinstance(total, int):
            continue
        eta = event.payload.get("eta_s")
        return Progress(
            done=done,
            total=total,
            eta_s=float(eta) if isinstance(eta, int | float) else None,
            label=PROGRESS_LABELS[event.kind],
        )
    return None


def latest_phase(state: RunState, step: str) -> str | None:
    """The name of the last phase the step announced, for the line above the bar."""
    recorded = state.record(step).events
    if not recorded:
        return None
    for event in reversed(_tail_events(Path(recorded))):
        if event.kind == "phase":
            name = event.payload.get("name")
            return str(name) if name is not None else None
    return None
