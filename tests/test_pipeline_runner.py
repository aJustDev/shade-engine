"""The chain: order, resumption, the gate before publishing, and the preflight.

What these protect is the thing the whole phase exists for -- that a build no
longer depends on somebody remembering which command comes next, or noticing at
hour four that the city was never going to fit.
"""

import inspect
import shutil
from collections.abc import Callable
from dataclasses import fields
from pathlib import Path

import pytest
import yaml

import laz_fixture
import synthetic
from conftest import CUBE_CITY
from shade_pipeline import budget, cli, runner
from shade_pipeline.basemap import BasemapError
from shade_pipeline.cadastre import CadastreSource
from shade_pipeline.footprints import OsmnxFootprintSource
from shade_pipeline.horizon import HorizonParams
from shade_pipeline.runner import (
    ChainError,
    ChainOptions,
    preflight,
    run_chain,
    steps_between,
)
from shade_pipeline.runstate import CHAIN, RunState, StepStatus
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
    """Chain options for a test: like production, minus the network.

    ``footprints`` and ``cadastre`` are off here and **on** in
    :class:`ChainOptions` itself. One needs Overpass and the other the
    Catastro WFS, and a suite that reaches the internet is a suite that fails
    on a train; the shipped defaults are asserted directly instead, and the
    tests that care about them intercept the call.
    """
    defaults: dict[str, object] = {
        "cities_dir": workspace / "cities",
        "output_root": workspace / "out",
        "data_root": workspace / "data",
        "min_zoom": 17,
        "max_zoom": 18,
        "footprints": False,
        "cadastre": False,
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


def test_an_unreachable_basemap_does_not_stop_the_build(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """It is a download from a third party, in front of four hours of sweeping.

    Optional here and refused by ``publish``: losing a night's render because
    build.protomaps.com was down would be absurd, and so would letting the city
    reach a browser with the overlay drawn on black.
    """
    options = _options(workspace)
    monkeypatch.setattr(
        runner,
        "build_basemap",
        lambda *args, **kwargs: (_ for _ in ()).throw(BasemapError("connection reset")),
    )
    monkeypatch.setattr(runner, "ensure_assets", lambda *args, **kwargs: None)
    lines: list[str] = []

    outcomes = run_chain(
        "cube",
        steps=("basemap", "build"),
        options=options,
        source=_source(workspace),  # type: ignore[arg-type]
        progress=lines.append,
    )

    statuses = {outcome.step: outcome.status for outcome in outcomes}
    assert statuses["basemap"] is StepStatus.FAILED
    assert statuses["build"] is StepStatus.DONE
    assert any("it is optional, carrying on" in line for line in lines)


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


def test_the_sweep_gets_the_workers_and_tile_size_the_chain_was_asked_for(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """It did not, and that is what made a chain build slower than a direct one.

    `run_chain` called `build_city` without params, so the sweep fell back to
    its own defaults -- one worker, tile 512 -- while the chain priced the build
    with whatever was asked for and recorded it in the state file. Measured on
    Montalban: 3m 18s of serial sweep with `workers: 13` written next to it.
    """
    seen: list[HorizonParams] = []

    def spy(
        config: object, source: object, output_root: Path, params: HorizonParams, **kw: object
    ) -> Path:
        seen.append(params)
        return output_root

    monkeypatch.setattr("shade_pipeline.runner.build_city", spy)

    run_chain(
        "cube",
        steps=("build",),
        options=_options(workspace, workers=3, tile_size=256),
        source=_source(workspace),  # type: ignore[arg-type]
    )

    assert (seen[0].workers, seen[0].tile_size) == (3, 256)
    # And the city's own physics is still the city's, not the flag's.
    assert seen[0].sectors == CUBE_CITY.horizon_sectors
    assert seen[0].max_distance_m == CUBE_CITY.horizon_max_distance_m
    assert seen[0].observer_height_m == CUBE_CITY.observer_height_m


def _spy_on_build(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, object]]:
    """Record what the chain hands `build_city`, without building anything."""
    seen: list[dict[str, object]] = []

    def spy(
        config: object, source: object, output_root: Path, params: HorizonParams, **kw: object
    ) -> Path:
        seen.append({"params": params, **kw})
        return output_root

    monkeypatch.setattr("shade_pipeline.runner.build_city", spy)
    return seen


def test_the_chain_corrects_roofs_like_the_standalone_command(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """It did not, and the difference only showed in a metadata field.

    `run_chain` called `build_city` with no footprint source, so every city
    built through the chain -- which is every city built from the console --
    came out without [[ADR-016]]. Cordoba and Montilla, built by hand, had
    248.645 and 12.256 cells relabelled; Montalban, built by the chain,
    reported `footprints: null` and shipped that way.
    """
    seen = _spy_on_build(monkeypatch)

    run_chain(
        "cube",
        steps=("build",),
        options=_options(workspace, footprints=True),
        source=_source(workspace),  # type: ignore[arg-type]
    )

    assert isinstance(seen[0]["footprints"], OsmnxFootprintSource)
    assert seen[0]["declutter"] is True
    # What ships, as opposed to what this suite runs with.
    assert ChainOptions().footprints is True
    assert ChainOptions().tree_inventory is True
    assert ChainOptions().cadastre is True


def test_the_chain_asks_the_cadastre_like_the_standalone_command(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The third parameter with a default, and the third chance to diverge.

    Twice already a `build_city` argument the chain forgot to pass produced a
    quietly different artifact. `build_tiles` now takes one too, so the two
    invocations are checked against each other rather than trusted.
    """
    seen: list[dict[str, object]] = []

    def spy(config: object, artifact_dir: Path, instants: object, **kw: object) -> Path:
        seen.append(dict(kw))
        return artifact_dir

    monkeypatch.setattr("shade_pipeline.runner.build_tiles", spy)
    # The artifacts are not what is under test here: this is about which
    # arguments cross the boundary, and building a city to find out costs a
    # minute per run.
    monkeypatch.setattr("shade_pipeline.runner._require_artifacts", lambda *_args: None)

    run_chain(
        "cube",
        steps=("tiles",),
        options=_options(workspace, cadastre=True),
        source=_source(workspace),  # type: ignore[arg-type]
    )

    assert isinstance(seen[0]["cadastre"], CadastreSource)


def test_every_chain_switch_is_reachable_from_the_command_line() -> None:
    """The generic version of a mistake that has now been made three times.

    `run_chain` gained `footprints`/`trees`, then the sweep's `workers`, then
    `cadastre` -- and each time the switch existed in `ChainOptions` while
    `shade-engine run` had no way to set it, so the chain silently produced
    something the standalone command did not. Checking the two lists against
    each other costs nothing and catches the fourth one before it ships.
    """
    switches = {field.name for field in fields(ChainOptions) if field.type in (bool, "bool")}
    exposed = set(inspect.signature(cli.run).parameters)

    assert switches <= exposed, f"not reachable from `shade-engine run`: {switches - exposed}"


def test_a_build_with_no_network_is_still_one_flag_away(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The escape hatch `build` already had: no Overpass, no correction, no build failure."""
    seen = _spy_on_build(monkeypatch)

    run_chain(
        "cube",
        steps=("build",),
        options=_options(workspace, declutter=False),
        source=_source(workspace),  # type: ignore[arg-type]
    )

    assert seen[0]["footprints"] is None
    assert seen[0]["declutter"] is False


def test_the_tree_audit_needs_the_city_to_declare_an_inventory(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two of seven cities declare one, so the flag alone cannot decide it."""
    seen = _spy_on_build(monkeypatch)

    run_chain(
        "cube",
        steps=("build",),
        options=_options(workspace, footprints=True),
        source=_source(workspace),  # type: ignore[arg-type]
    )

    assert CUBE_CITY.tree_inventory is None
    assert seen[0]["trees"] is None


def test_step_slicing() -> None:
    assert steps_between("build", "tiles") == ("build", "graph", "tiles")
    assert steps_between(None, None) == ("area", "basemap", "build", "graph", "tiles")
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
