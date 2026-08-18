"""Deleting a city's local artifacts: what goes, what stays, and what it says.

The rule under test is the one the inventory exists to make visible: the
artifacts go, the LiDAR cache and the run history do not, and the steps whose
product is gone go back to pending instead of claiming a success.
"""

from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from shade_pipeline.cli import app
from shade_pipeline.purge import PURGED_STEPS, PurgePlan, execute_purge, plan_purge
from shade_pipeline.runstate import RunState, StepStatus

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
def city(tmp_path: Path) -> Path:
    """A city with artifacts, a LiDAR cache and four finished steps."""
    (tmp_path / "cities").mkdir()
    (tmp_path / "cities" / "cube.yaml").write_text(yaml.safe_dump(CONFIG), encoding="utf-8")

    artifacts = tmp_path / "out" / "cube" / "v1"
    (artifacts / "tiles").mkdir(parents=True)
    (artifacts / "horizon.tif").write_bytes(b"x" * 2048)
    (artifacts / "coverage.tif").write_bytes(b"x" * 512)
    (artifacts / "tiles" / "0.pmtiles").write_bytes(b"x" * 4096)

    cache = tmp_path / "lidar" / "cube"
    cache.mkdir(parents=True)
    (cache / "one.laz").write_bytes(b"x" * 8192)

    state = _state(tmp_path)
    for step in ("basemap", "build", "graph", "tiles"):
        log, events = state.paths_for(step)
        state.begin(step, log=log, events=events)
        state.complete(step)
    return tmp_path


def _state(root: Path) -> RunState:
    return RunState.open("cube", cities_dir=root / "cities", data_root=root / "data")


def _plan(root: Path, state: RunState | None = None) -> PurgePlan:
    return plan_purge(
        "cube",
        output_root=root / "out",
        data_root=root / "data",
        lidar_root=root / "lidar",
        state=state if state is not None else _state(root),
    )


def test_the_plan_prices_what_goes_and_names_what_stays(city: Path) -> None:
    plan = _plan(city)

    assert plan.freed == 2048 + 512 + 4096
    assert [item.path.name for item in plan.removed] == ["coverage.tif", "horizon.tif", "tiles"]
    assert [item.path.name for item in plan.kept] == ["cube", "cube"]
    assert plan.cached_lidar
    assert plan.steps == list(PURGED_STEPS)


def test_purging_leaves_the_lidar_cache_and_the_history(city: Path) -> None:
    """The expensive thing to get back is somebody else's download, not ours."""
    state = _state(city)

    execute_purge(_plan(city, state), state)

    assert not (city / "out" / "cube" / "v1").exists()
    assert (city / "lidar" / "cube" / "one.laz").exists()
    assert (city / "data" / "runs" / "cube" / "history.jsonl").exists()


def test_the_steps_go_back_to_pending_and_keep_their_logs(city: Path) -> None:
    state = _state(city)
    logs = {step: state.record(step).log for step in PURGED_STEPS}

    execute_purge(_plan(city, state), state)

    reopened = _state(city)
    for step in PURGED_STEPS:
        assert reopened.status(step) is StepStatus.PENDING, step
        assert reopened.record(step).log == logs[step], step


def test_publish_is_left_alone_because_the_server_still_has_it(city: Path) -> None:
    """Purging is local. Saying otherwise would be a claim about another machine."""
    state = _state(city)
    log, events = state.paths_for("publish")
    state.begin("publish", log=log, events=events)
    state.complete("publish")

    execute_purge(_plan(city, state), state)

    assert _state(city).record("publish").status is StepStatus.DONE


def test_a_dry_run_deletes_nothing(city: Path) -> None:
    result = CliRunner().invoke(
        app,
        [
            "purge",
            "cube",
            "--dry-run",
            "--cities-dir",
            str(city / "cities"),
            "--output-root",
            str(city / "out"),
            "--data-root",
            str(city / "data"),
            "--lidar-root",
            str(city / "lidar"),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "nothing deleted" in result.output
    assert "horizon.tif" in result.output
    assert (city / "out" / "cube" / "v1" / "horizon.tif").exists()


def test_the_inventory_says_the_lidar_rule_even_with_no_cache(tmp_path: Path) -> None:
    """Silence about the cache would read as "it went"."""
    plan = plan_purge("cube", output_root=tmp_path / "out", data_root=tmp_path / "data")

    assert not plan.cached_lidar
    assert "never touches it" in plan.render()
