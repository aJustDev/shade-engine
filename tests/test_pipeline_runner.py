"""The chain: order, resumption, the gate before publishing, and the preflight.

What these protect is the thing the whole phase exists for -- that a build no
longer depends on somebody remembering which command comes next, or noticing at
hour four that the city was never going to fit.
"""

import shutil
from collections.abc import Callable
from pathlib import Path

import pytest
import yaml

import laz_fixture
import synthetic
from conftest import CUBE_CITY
from shade_pipeline import budget
from shade_pipeline.runner import (
    CHAIN,
    ChainError,
    ChainOptions,
    preflight,
    run_chain,
    steps_between,
)
from shade_pipeline.runstate import RunState, StepStatus
from shade_pipeline.sources import LocalDirectory


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """A cities dir with the cube config and a LAZ tile to build it from."""
    cities = tmp_path / "cities"
    cities.mkdir()
    (cities / "cube.yaml").write_text(
        yaml.safe_dump(CUBE_CITY.model_dump(mode="json", exclude_none=True)), encoding="utf-8"
    )
    lidar = tmp_path / "lidar"
    lidar.mkdir()
    laz_fixture.write_cube_laz(lidar / "cube.laz", origin=synthetic.UTM_ORIGIN)
    return tmp_path


def _options(workspace: Path, **overrides: object) -> ChainOptions:
    defaults: dict[str, object] = {
        "cities_dir": workspace / "cities",
        "output_root": workspace / "out",
        "data_root": workspace / "data",
        "min_zoom": 17,
        "max_zoom": 18,
    }
    return ChainOptions(**{**defaults, **overrides})  # type: ignore[arg-type]


def _source(workspace: Path) -> object:
    # Two arguments: the chain hands each source the step's own log callback, so
    # a LiDAR download reports where somebody can see it.
    return lambda _config, _say: LocalDirectory(workspace / "lidar")


def test_the_chain_runs_in_order_and_stops_before_publishing(workspace: Path) -> None:
    """Publishing is the one step that is not unattended."""
    lines: list[str] = []

    outcomes = run_chain(
        "cube",
        steps=("area", "build", "tiles", "publish"),
        options=_options(workspace),
        source=_source(workspace),  # type: ignore[arg-type]
        progress=lines.append,
    )

    assert [outcome.step for outcome in outcomes] == ["area", "build", "tiles", "publish"]
    assert outcomes[-1].status is StepStatus.PENDING
    assert any("waiting for your go-ahead" in line for line in lines)


def test_a_second_run_skips_what_is_already_done(workspace: Path) -> None:
    options = _options(workspace)
    run_chain(
        "cube",
        steps=("area", "build"),
        options=options,
        source=_source(workspace),  # type: ignore[arg-type]
    )
    lines: list[str] = []

    outcomes = run_chain(
        "cube",
        steps=("area", "build"),
        options=options,
        source=_source(workspace),  # type: ignore[arg-type]
        progress=lines.append,
    )

    assert all(outcome.detail == "already done" for outcome in outcomes)
    assert any("already done, skipping" in line for line in lines)


def test_editing_the_config_makes_the_chain_redo_the_build(workspace: Path) -> None:
    """The rule that used to be prose: change the bbox, the artifacts are void."""
    options = _options(workspace)
    run_chain(
        "cube",
        steps=("build",),
        options=options,
        source=_source(workspace),  # type: ignore[arg-type]
    )
    config = CUBE_CITY.model_dump(mode="json", exclude_none=True)
    config["observer_height_m"] = 1.7
    (workspace / "cities" / "cube.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    lines: list[str] = []

    run_chain(
        "cube",
        steps=("build",),
        options=options,
        source=_source(workspace),  # type: ignore[arg-type]
        progress=lines.append,
    )

    assert any("configuration changed" in line for line in lines)


def test_a_failed_step_is_recorded_and_stops_the_chain(workspace: Path) -> None:
    options = _options(workspace)
    shutil.rmtree(workspace / "lidar")

    with pytest.raises(Exception):  # noqa: B017 -- the driver's own error type
        run_chain(
            "cube",
            steps=("area", "build", "tiles"),
            options=options,
            source=_source(workspace),  # type: ignore[arg-type]
        )

    state = RunState.open("cube", cities_dir=options.cities_dir, data_root=options.data_root)
    assert state.status("area") is StepStatus.DONE
    assert state.status("build") is StepStatus.FAILED
    assert state.record("build").error
    assert state.status("tiles") is StepStatus.PENDING, "the chain should not have gone on"


def test_a_failed_step_keeps_its_log(workspace: Path) -> None:
    options = _options(workspace)
    shutil.rmtree(workspace / "lidar")
    with pytest.raises(Exception):  # noqa: B017
        run_chain(
            "cube",
            steps=("build",),
            options=options,
            source=_source(workspace),  # type: ignore[arg-type]
        )

    state = RunState.open("cube", cities_dir=options.cities_dir, data_root=options.data_root)
    log = Path(state.record("build").log or "")
    assert log.exists()
    assert "Traceback" in log.read_text(encoding="utf-8")


def test_tiles_refuses_to_run_before_the_build(workspace: Path) -> None:
    with pytest.raises(ChainError, match="run build first"):
        run_chain(
            "cube",
            steps=("tiles",),
            options=_options(workspace),
            source=_source(workspace),  # type: ignore[arg-type]
        )


def test_the_preflight_refuses_a_city_the_tile_phase_could_never_render(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The check that saves the four hours.

    ``tiles`` already refuses to start when a worker will not fit -- but it does
    so *after* the build. A city that can be swept and not rendered used to burn
    the whole sweep before anyone found out.
    """
    # Patched in `budget`, which is where `workers_that_fit` reads it: since the
    # preflight delegates to `plan_city`, there is one cost model and one place
    # the memory budget comes from.
    monkeypatch.setattr(budget, "available_bytes", lambda: 64 * 1024 * 1024)

    with pytest.raises(ChainError, match="tile-render worker needs"):
        preflight(CUBE_CITY, _options(workspace))


def test_the_preflight_says_what_it_measured(workspace: Path) -> None:
    notes = preflight(CUBE_CITY, _options(workspace))

    assert any("Mpx" in note for note in notes)
    assert any("per worker" in note for note in notes)


def test_an_unreadable_memory_budget_does_not_block_the_build(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Being unable to measure is not a reason to refuse; the phases check again."""
    monkeypatch.setattr(budget, "available_bytes", lambda: None)

    notes = preflight(CUBE_CITY, _options(workspace))

    assert any("unreadable" in note for note in notes)


def test_a_failed_graph_does_not_stop_the_map(workspace: Path) -> None:
    """The synthetic city has no OSM walk network, which is the real case too.

    A dead Overpass, or a city whose streets nobody has mapped, must not cost
    six hours of tiles: the API answers 503 for routes and serves the shade map
    exactly as it would have.
    """
    options = _options(workspace)
    lines: list[str] = []

    outcomes = run_chain(
        "cube",
        steps=("build", "graph", "tiles"),
        options=options,
        source=_source(workspace),  # type: ignore[arg-type]
        graph_source=lambda: (_ for _ in ()).throw(ValueError("no graph nodes here")),
        progress=lines.append,
    )

    statuses = {outcome.step: outcome.status for outcome in outcomes}
    assert statuses["graph"] is StepStatus.FAILED
    assert statuses["tiles"] is StepStatus.DONE
    assert any("it is optional, carrying on" in line for line in lines)
    assert (options.output_root / "cube" / "v1" / "tiles" / "index.json").exists()


def test_rerunning_the_graph_does_not_invalidate_the_tiles(workspace: Path) -> None:
    """Staleness follows what a step is computed from, not the running order."""
    options = _options(workspace)
    run_chain(
        "cube",
        steps=("build", "tiles"),
        options=options,
        source=_source(workspace),  # type: ignore[arg-type]
    )
    state = RunState.open("cube", cities_dir=options.cities_dir, data_root=options.data_root)
    log, events = state.paths_for("graph")
    state.begin("graph", log=log, events=events)
    state.complete("graph")

    assert state.status("tiles") is StepStatus.DONE, "the graph does not feed the tiles"
    assert state.status("graph") is StepStatus.DONE


def test_the_lidar_download_reports_into_the_step_log(workspace: Path) -> None:
    """A silent twelve-minute download is indistinguishable from a hang.

    The driver already emits one line per PNOA tile, but it used to send them to
    stdout -- which a detached run points at /dev/null. The log stopped on the
    computation-area line and stayed there, and the only way to tell a healthy
    download from a wedged process was to go and stat the cache directory.
    """
    options = _options(workspace)
    spoken: list[str] = []

    def talkative_source(_config: object, say: Callable[[str], None]) -> object:
        say("[1/12] PNOA-2024-AND-345-4160-H30-NPC01.laz")
        spoken.append("called")
        return LocalDirectory(workspace / "lidar")

    run_chain(
        "cube",
        steps=("build",),
        options=options,
        source=talkative_source,  # type: ignore[arg-type]
    )

    assert spoken, "the chain has to build the source, not receive one ready-made"
    state = RunState.open("cube", cities_dir=options.cities_dir, data_root=options.data_root)
    log = Path(state.record("build").log or "")
    assert "PNOA-2024-AND-345-4160" in log.read_text(encoding="utf-8")


def test_step_slicing() -> None:
    assert steps_between("build", "tiles") == ("build", "graph", "tiles")
    assert steps_between(None, None) == ("area", "build", "graph", "tiles")
    assert steps_between("tiles", "tiles") == ("tiles",)
    assert steps_between(None, "publish") == CHAIN

    with pytest.raises(ChainError, match="unknown step"):
        steps_between("nope", None)
    with pytest.raises(ChainError, match="comes after"):
        steps_between("tiles", "build")


def test_a_running_step_is_not_started_twice(workspace: Path) -> None:
    """Two supervisors on one city is how you get two renders into one directory."""
    options = _options(workspace)
    state = RunState.open("cube", cities_dir=options.cities_dir, data_root=options.data_root)
    log, events = state.paths_for("build")
    state.begin("build", log=log, events=events)  # records this live pid

    with pytest.raises(ChainError, match="already running"):
        run_chain(
            "cube",
            steps=("build",),
            options=options,
            source=_source(workspace),  # type: ignore[arg-type]
        )
