"""Structured events: the channel a supervisor reads, next to the human lines.

The point of the module is that nothing has to parse prose to know where a
six-hour phase is. These pin the round trip, the tolerance a live reader needs,
and that a phase really does emit.
"""

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from conftest import CUBE_CITY
from shade_pipeline.events import Event, JsonlSink, emit, read_events
from shade_pipeline.tiles import build_tiles

NOON = datetime(2026, 6, 21, 13, 0, tzinfo=ZoneInfo("Europe/Madrid"))


def test_an_event_survives_the_round_trip() -> None:
    event = Event(
        step="tiles",
        kind="unit",
        at=datetime(2026, 8, 16, 20, 7, tzinfo=UTC),
        payload={"label": "20260621T1300", "done": 3, "eta_s": 12.5},
    )
    back = Event.from_json(event.as_json())

    assert back.step == "tiles"
    assert back.kind == "unit"
    assert back.at == event.at
    assert back.payload == {"label": "20260621T1300", "done": 3, "eta_s": 12.5}


def test_a_payload_that_cannot_be_json_is_stringified_not_raised() -> None:
    """Losing a progress event must never be what kills a six-hour build."""
    event = Event(step="build", kind="phase", at=datetime.now(UTC), payload={"where": Path("/tmp")})

    assert json.loads(event.as_json())["where"] == "/tmp"


def test_the_sink_appends_and_the_reader_replays(tmp_path: Path) -> None:
    path = tmp_path / "run" / "events.jsonl"
    with JsonlSink(path) as sink:
        emit(sink, "build", "phase", name="sweep", elapsed_s=1.0)
        emit(sink, "build", "finished", bytes=10)

    replayed = list(read_events(path))
    assert [(event.step, event.kind) for event in replayed] == [
        ("build", "phase"),
        ("build", "finished"),
    ]
    assert replayed[0].payload["name"] == "sweep"


def test_the_reader_tolerates_a_line_still_being_written(tmp_path: Path) -> None:
    """Catching the file mid-write is the normal case, not an error.

    A console tails this while the phase appends to it, so a truncated last
    line has to be skipped rather than blow up the reader.
    """
    path = tmp_path / "events.jsonl"
    with JsonlSink(path) as sink:
        emit(sink, "tiles", "unit", done=1)
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"step": "tiles", "kind": "un')

    assert [event.kind for event in read_events(path)] == ["unit"]


def test_a_missing_file_replays_as_nothing(tmp_path: Path) -> None:
    assert list(read_events(tmp_path / "never-written.jsonl")) == []


def test_emitting_without_a_sink_does_nothing() -> None:
    emit(None, "build", "phase", name="sweep")


def test_the_build_reports_its_phases_and_its_sweep(tmp_path: Path) -> None:
    """The sweep reports per tile, not per phase.

    It is the four-hour step, and one event at the end of it would tell a
    console nothing during the only time it mattered.
    """
    import laz_fixture
    import synthetic
    from shade_pipeline.build import build_city
    from shade_pipeline.sources import LocalDirectory

    lidar = tmp_path / "lidar"
    lidar.mkdir()
    laz_fixture.write_cube_laz(lidar / "cube.laz", origin=synthetic.UTM_ORIGIN)
    seen: list[Event] = []

    build_city(CUBE_CITY, LocalDirectory(lidar), tmp_path / "out", events=seen.append)

    phases = [event.payload["name"] for event in seen if event.kind == "phase"]
    assert "binning" in phases
    assert "sweep" in phases
    assert "verify" in phases
    assert [event.kind for event in seen][-1] == "finished"
    tiles = [event for event in seen if event.kind == "tile"]
    assert tiles, "the sweep should report tile by tile"
    assert tiles[-1].payload["done"] == tiles[-1].payload["total"]


def test_the_tile_render_reports_its_units(built_city: Path, tmp_path: Path) -> None:
    target = tmp_path / "city"
    shutil.copytree(built_city, target)
    seen: list[Event] = []

    build_tiles(CUBE_CITY, target, [NOON], min_zoom=17, max_zoom=18, events=seen.append)

    kinds = [event.kind for event in seen]
    assert kinds[0] == "started"
    assert kinds[-1] == "finished"
    units = [event for event in seen if event.kind == "unit"]
    assert len(units) == 4, "three static sets and one instant"
    assert units[-1].payload["done"] == units[-1].payload["total"]
    assert all(event.step == "tiles" for event in seen)
