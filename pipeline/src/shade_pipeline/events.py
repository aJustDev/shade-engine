"""Structured progress events, running alongside the human progress lines.

Every long phase already reports through ``progress``, a callable taking a
formatted sentence that usually ends in ``typer.echo``. That is the right shape
for somebody watching a terminal and the wrong one for anything that has to
*react*: a supervisor deciding whether a step can be resumed, or a console
drawing a progress bar, would have to parse prose back into numbers. Doing
exactly that with ``grep`` over ``data/*.log`` is what this module exists to
stop.

So events travel on their own channel. ``progress`` is untouched -- its
sentences are still the human record -- and a caller that wants machine-readable
milestones passes an ``events`` sink as well. Both are optional and independent:
nothing that works today changes shape.

Events are deliberately coarse. A phase opening or closing, a unit finishing, a
check passing: the running commentary (one line per LiDAR file binned, one per
horizon tile swept) stays on the text channel, where a reader that does not care
pays nothing to skip it.
"""

import json
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Any, Self

STEPS: tuple[str, ...] = ("area", "build", "graph", "tiles", "publish")
"""The chain a city walks, in order. ``runner`` owns the ordering; this is the
vocabulary the ``step`` field draws from, so a reader can bucket events without
knowing which command produced them."""


@dataclass(frozen=True, slots=True)
class Event:
    """One milestone: which step, what kind, when, and the numbers.

    ``payload`` holds whatever that kind of event carries (durations, counts,
    an ETA). It is written as JSON, so the values have to survive
    ``json.dumps``; non-serialisable objects are stringified rather than
    raising, because losing a progress event must never kill a six-hour build.
    """

    step: str
    kind: str
    at: datetime
    payload: Mapping[str, Any] = field(default_factory=dict)

    def as_json(self) -> str:
        record = {
            "step": self.step,
            "kind": self.kind,
            "at": self.at.isoformat(timespec="seconds"),
            **self.payload,
        }
        return json.dumps(record, default=str, ensure_ascii=True)

    @classmethod
    def from_json(cls, line: str) -> Self:
        record = json.loads(line)
        step = record.pop("step")
        kind = record.pop("kind")
        at = datetime.fromisoformat(record.pop("at"))
        return cls(step=step, kind=kind, at=at, payload=record)


EventSink = Callable[[Event], None]
"""What a phase is handed. ``None`` everywhere means "nobody is listening"."""


def emit(sink: EventSink | None, step: str, kind: str, **payload: Any) -> None:
    """Stamp an event with the current time and hand it to ``sink``, if any.

    The ``None`` check lives here rather than at every call site so a phase can
    emit unconditionally, the same way it already calls ``say``.
    """
    if sink is None:
        return
    sink(Event(step=step, kind=kind, at=datetime.now(UTC), payload=payload))


class JsonlSink:
    """Append events to a file, one JSON object per line, flushed as they land.

    Flushing every event is the point: the console tails this file while the
    phase runs, and a buffered write would make a job look frozen for as long
    as the buffer took to fill. The volume makes that free -- these are
    milestones, tens or hundreds over hours, not a log stream.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("a", encoding="utf-8")

    def __call__(self, event: Event) -> None:
        self._handle.write(event.as_json() + "\n")
        self._handle.flush()

    def close(self) -> None:
        self._handle.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()


def read_events(path: str | Path) -> Iterator[Event]:
    """Replay a sink's file.

    A trailing partial line is skipped rather than raising: the reader is
    routinely a console tailing a file that a live phase is still appending to,
    and catching it mid-write is normal, not an error.
    """
    file = Path(path)
    if not file.exists():
        return
    with file.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.endswith("\n"):
                continue
            stripped = line.strip()
            if not stripped:
                continue
            try:
                yield Event.from_json(stripped)
            except json.JSONDecodeError, KeyError, ValueError:
                continue
