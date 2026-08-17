"""Per-city step state, and the staleness it derives rather than stores.

The rule under test is the one that used to be prose in a runbook: artifacts
built from a configuration that has since changed are not "done", they are
stale, and so is anything built on top of them.
"""

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from shade_pipeline.cli import app
from shade_pipeline.runstate import (
    KEEP_RUNS,
    LATEST_DIRNAME,
    STATE_FILENAME,
    CityState,
    RunState,
    StepRecord,
    StepStatus,
    config_digest,
)

CONFIG = {
    "id": "cube",
    "name": "Cube",
    "country": "ES",
    "timezone": "Europe/Madrid",
    "crs": "EPSG:25830",
    "bbox": [340000, 4190000, 340100, 4190100],
    "resolution_m": 1.0,
}


@pytest.fixture
def cities_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "cities"
    directory.mkdir()
    (directory / "cube.yaml").write_text(yaml.safe_dump(CONFIG), encoding="utf-8")
    return directory


def _state(cities_dir: Path, tmp_path: Path) -> RunState:
    return RunState.open("cube", cities_dir=cities_dir, data_root=tmp_path / "data")


def _run(state: RunState, step: str) -> None:
    log, events = state.paths_for(step)
    state.begin(step, log=log, events=events)
    state.complete(step)


def test_a_step_starts_pending_and_ends_done(cities_dir: Path, tmp_path: Path) -> None:
    state = _state(cities_dir, tmp_path)
    assert state.status("build") is StepStatus.PENDING

    _run(state, "build")

    assert state.status("build") is StepStatus.DONE
    assert state.record("build").duration_s is not None
    assert state.record("build").pid is None


def test_the_state_survives_the_process_that_wrote_it(cities_dir: Path, tmp_path: Path) -> None:
    """The whole point: a console reads this, it does not own the job."""
    _run(_state(cities_dir, tmp_path), "build")

    reopened = _state(cities_dir, tmp_path)

    assert reopened.status("build") is StepStatus.DONE
    assert (tmp_path / "data" / "runs" / "cube" / STATE_FILENAME).exists()


def test_editing_the_config_makes_finished_steps_stale(cities_dir: Path, tmp_path: Path) -> None:
    """Changing the bbox invalidates the artifacts; now the tooling knows it."""
    state = _state(cities_dir, tmp_path)
    _run(state, "build")
    _run(state, "tiles")

    moved = dict(CONFIG, bbox=[340000, 4190000, 340200, 4190200])
    (cities_dir / "cube.yaml").write_text(yaml.safe_dump(moved), encoding="utf-8")
    reopened = _state(cities_dir, tmp_path)

    assert reopened.status("build") is StepStatus.STALE
    assert reopened.status("tiles") is StepStatus.STALE
    assert reopened.stale_reason("build") == "the city configuration changed since it ran"


def test_rerunning_an_earlier_step_makes_the_later_ones_stale(
    cities_dir: Path, tmp_path: Path
) -> None:
    """Tiles rendered before a rebuild describe a city that no longer exists."""
    state = _state(cities_dir, tmp_path)
    _run(state, "build")
    _run(state, "tiles")
    assert state.status("tiles") is StepStatus.DONE

    _run(state, "build")

    assert state.status("build") is StepStatus.DONE
    assert state.status("tiles") is StepStatus.STALE
    assert state.stale_reason("tiles") == "build has run again since"


def test_area_is_not_stale_for_having_rewritten_its_own_config(
    cities_dir: Path, tmp_path: Path
) -> None:
    """``area --write`` edits the very file the digest is taken over.

    Recording the digest from before it ran would leave the step stale the
    instant it succeeded, which is why ``complete`` re-fingerprints.
    """
    state = _state(cities_dir, tmp_path)
    log, events = state.paths_for("area")
    state.begin("area", log=log, events=events)
    widened = dict(CONFIG, bbox=[339000, 4189000, 341000, 4191000])
    (cities_dir / "cube.yaml").write_text(yaml.safe_dump(widened), encoding="utf-8")

    state.complete("area", digest=config_digest(cities_dir, "cube"))

    assert state.status("area") is StepStatus.DONE
    assert _state(cities_dir, tmp_path).status("area") is StepStatus.DONE


def test_a_step_whose_process_is_gone_reads_as_failed_not_running() -> None:
    """A killed supervisor leaves RUNNING behind, and a resumer must not wait on it."""
    dead = CityState(
        city="cube",
        steps={"tiles": StepRecord(status=StepStatus.RUNNING, pid=0x7FFFFFFF)},
    )

    assert dead.status("tiles") is StepStatus.FAILED

    alive = CityState(
        city="cube",
        steps={"tiles": StepRecord(status=StepStatus.RUNNING, pid=os.getpid())},
    )
    assert alive.status("tiles") is StepStatus.RUNNING


def test_a_failed_step_keeps_its_message(cities_dir: Path, tmp_path: Path) -> None:
    state = _state(cities_dir, tmp_path)
    log, events = state.paths_for("build")
    state.begin("build", log=log, events=events)

    state.fail("build", "CoverageError: 3 lidar tiles missing")

    assert state.status("build") is StepStatus.FAILED
    assert "3 lidar tiles missing" in (state.record("build").error or "")


def test_the_area_polygon_counts_towards_the_digest(cities_dir: Path, tmp_path: Path) -> None:
    """The polygon decides which pixels exist, so editing it invalidates as surely as the bbox."""
    polygon = tmp_path / "area.geojson"
    polygon.write_text('{"type": "FeatureCollection", "features": []}', encoding="utf-8")
    (cities_dir / "cube.yaml").write_text(
        yaml.safe_dump(dict(CONFIG, area=str(polygon))), encoding="utf-8"
    )
    before = config_digest(cities_dir, "cube")

    polygon.write_text('{"type": "FeatureCollection", "features": [ ]}', encoding="utf-8")

    assert config_digest(cities_dir, "cube") != before


def test_a_corrupt_state_file_reads_as_a_fresh_city(cities_dir: Path, tmp_path: Path) -> None:
    """Losing the record is a setback; refusing to start because of it is worse."""
    runs = tmp_path / "data" / "runs" / "cube"
    runs.mkdir(parents=True)
    (runs / STATE_FILENAME).write_text("{ not json", encoding="utf-8")

    state = _state(cities_dir, tmp_path)

    assert state.status("build") is StepStatus.PENDING


def test_logs_are_stamped_not_overwritten(cities_dir: Path, tmp_path: Path) -> None:
    state = _state(cities_dir, tmp_path)
    log, events = state.paths_for("tiles")

    assert log.name.startswith("tiles-")
    assert log.suffix == ".log"
    assert events.suffix == ".jsonl"
    assert log.parent == events.parent


def test_a_finished_step_is_not_stale_against_an_older_predecessor(
    cities_dir: Path, tmp_path: Path
) -> None:
    """Guarding the rule itself: order is what makes something stale, not mere presence."""
    now = datetime.now(UTC)
    digest = config_digest(cities_dir, "cube")
    state = CityState(
        city="cube",
        config_digest=digest,
        steps={
            "build": StepRecord(
                status=StepStatus.DONE, finished_at=now - timedelta(hours=2), config_digest=digest
            ),
            "tiles": StepRecord(status=StepStatus.DONE, finished_at=now, config_digest=digest),
        },
    )

    assert state.status("tiles") is StepStatus.DONE
    assert state.stale_reason("tiles") is None


def test_an_undone_step_goes_back_to_pending_but_keeps_its_log(
    cities_dir: Path, tmp_path: Path
) -> None:
    """Unpublishing removes the result, so `done` becomes a claim about nothing.

    The record survives with its log because the console follows the newest log
    across the chain, and dropping it would hide the very run that just undid
    the step.
    """
    state = _state(cities_dir, tmp_path)
    _run(state, "publish")
    log = state.record("publish").log

    state.undo("publish")

    assert state.status("publish") is StepStatus.PENDING
    assert state.record("publish").log == log
    assert state.record("publish").started_at is not None


def test_undoing_a_step_survives_a_reread(cities_dir: Path, tmp_path: Path) -> None:
    """The console is another process; it only ever sees the file."""
    state = _state(cities_dir, tmp_path)
    _run(state, "publish")
    state.undo("publish")

    assert _state(cities_dir, tmp_path).status("publish") is StepStatus.PENDING


def test_the_newest_run_of_a_step_has_a_stable_name(cities_dir: Path, tmp_path: Path) -> None:
    """Five publish-*.log in a directory and none of them says which is today's.

    The stamp is right -- a failed attempt has to survive the retry -- and it is
    exactly what makes the path unguessable, so a symlink carries the answer.
    """
    state = _state(cities_dir, tmp_path)
    log, _ = state.paths_for("publish")
    log.write_text("first", encoding="utf-8")
    link = state.directory / LATEST_DIRNAME / "publish.log"

    assert link.read_text(encoding="utf-8") == "first"

    later, _ = state.paths_for("publish")
    later.write_text("second", encoding="utf-8")

    assert link.resolve() == later.resolve()
    assert link.read_text(encoding="utf-8") == "second"


def test_the_latest_link_is_relative_so_the_tree_can_move(cities_dir: Path, tmp_path: Path) -> None:
    state = _state(cities_dir, tmp_path)
    state.paths_for("build")

    link = state.directory / LATEST_DIRNAME / "build.log"
    assert not Path(os.readlink(link)).is_absolute()


def test_old_runs_are_pruned_and_the_newest_are_kept(cities_dir: Path, tmp_path: Path) -> None:
    """A directory written to on every rebuild should not grow without end."""
    state = _state(cities_dir, tmp_path)
    for index in range(KEEP_RUNS + 4):
        # paths_for stamps to the second, so write the files by hand to get
        # distinct names without sleeping through fourteen seconds.
        (state.directory).mkdir(parents=True, exist_ok=True)
        (state.directory / f"tiles-202608{index + 1:02d}T000000.log").write_text("x")
    state.paths_for("tiles")

    kept = sorted(path.name for path in state.directory.glob("tiles-*.log"))
    assert len(kept) == KEEP_RUNS
    # The stamp sorts chronologically, so the survivors are the tail.
    assert kept[0] == "tiles-20260805T000000.log"


def test_pruning_one_step_leaves_the_others_alone(cities_dir: Path, tmp_path: Path) -> None:
    state = _state(cities_dir, tmp_path)
    state.directory.mkdir(parents=True, exist_ok=True)
    for index in range(KEEP_RUNS + 4):
        (state.directory / f"tiles-202608{index + 1:02d}T000000.log").write_text("x")
    (state.directory / "build-20260801T000000.log").write_text("keep me")

    state.paths_for("tiles")

    assert (state.directory / "build-20260801T000000.log").exists()


def test_the_logs_command_finds_a_run_without_being_told_its_timestamp(
    cities_dir: Path, tmp_path: Path
) -> None:
    """The point of the whole latest/ arrangement: a path you can hand to someone."""
    state = _state(cities_dir, tmp_path)
    log, _ = state.paths_for("publish")
    log.write_text("one\ntwo\nthree\n", encoding="utf-8")

    result = CliRunner().invoke(
        app,
        [
            "logs",
            "cube",
            "publish",
            "--cities-dir",
            str(cities_dir),
            "--data-root",
            str(tmp_path / "data"),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "three" in result.output


def test_the_logs_command_lists_the_steps_when_asked_for_none(
    cities_dir: Path, tmp_path: Path
) -> None:
    state = _state(cities_dir, tmp_path)
    log, _ = state.paths_for("build")
    log.write_text("swept\n", encoding="utf-8")

    result = CliRunner().invoke(
        app,
        ["logs", "cube", "--cities-dir", str(cities_dir), "--data-root", str(tmp_path / "data")],
    )

    assert result.exit_code == 0, result.output
    assert "latest/build.log" in result.output
    assert "publish" in result.output
