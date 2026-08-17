"""The console: reads state, explains settings, edits them, and never owns the work.

Driven through Textual's own harness where the interaction matters, and directly
where the logic is pure. Skipped whole when the optional extra is absent,
because the base package has to work without it.

The scenarios are async because ``run_test`` is, and they are driven with
``asyncio.run`` rather than by adding an async pytest plugin: this is the only
async test in the repository and a helper is cheaper than a dependency.
"""

import asyncio
import os
import signal
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import yaml

from conftest import CUBE_CITY
from shade_pipeline.events import JsonlSink, emit
from shade_pipeline.runstate import RunState, StepStatus

# Everything below this line needs the optional extra, imports included.
pytest.importorskip("textual", reason="the console needs the 'tui' extra")

from textual.widgets import DataTable, Input, Static, Switch

from shade_pipeline.area import utm_crs
from shade_pipeline.console.app import ConsoleApp
from shade_pipeline.console.cities import CitiesScreen
from shade_pipeline.console.city import CityScreen
from shade_pipeline.console.confirm import ConfirmScreen
from shade_pipeline.console.cost import CostPanel
from shade_pipeline.console.jobs import progress_of
from shade_pipeline.console.launch import (
    LaunchScreen,
    defaults_for,
    suggested_workers,
    to_argv,
)
from shade_pipeline.console.newcity import NewCityScreen, drawing_url, newest_geojson
from shade_pipeline.console.utilities import UTILITIES
from shade_pipeline.console.utilities import to_argv as utility_argv


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    cities = tmp_path / "cities"
    cities.mkdir()
    (cities / "cube.yaml").write_text(
        yaml.safe_dump(CUBE_CITY.model_dump(mode="json", exclude_none=True)), encoding="utf-8"
    )
    return tmp_path


def _app(workspace: Path) -> ConsoleApp:
    return ConsoleApp(
        cities_dir=workspace / "cities",
        output_root=workspace / "out",
        data_root=workspace / "data",
        watch_dir=workspace / "downloads",
    )


def drive(app: ConsoleApp, scenario: Callable[[Any], Awaitable[None]]) -> None:
    """Run one scenario against a live app and shut it down again."""

    async def main() -> None:
        async with app.run_test() as pilot:
            await pilot.pause()
            await scenario(pilot)

    asyncio.run(main())


def _state(workspace: Path) -> RunState:
    return RunState.open("cube", cities_dir=workspace / "cities", data_root=workspace / "data")


# --------------------------------------------------------------- the city list


def test_the_city_list_shows_a_row_per_city_and_a_column_per_step(workspace: Path) -> None:
    app = _app(workspace)
    seen: dict[str, Any] = {}

    async def scenario(pilot: Any) -> None:
        table = app.screen.query_one("#cities", DataTable)
        seen["rows"] = table.row_count
        seen["columns"] = [str(column.label) for column in table.columns.values()]

    drive(app, scenario)

    assert seen["rows"] == 1
    assert seen["columns"] == [
        "city",
        "area",
        "basemap",
        "build",
        "graph",
        "tiles",
        "publish",
    ]


def test_a_step_recorded_by_another_process_shows_up(workspace: Path) -> None:
    """The console polls the state file; something else entirely is what writes it."""
    app = _app(workspace)
    statuses: dict[str, StepStatus] = {}

    async def scenario(pilot: Any) -> None:
        state = _state(workspace)
        log, events = state.paths_for("build")
        state.begin("build", log=log, events=events)
        state.complete("build")
        assert isinstance(app.screen, CitiesScreen)
        app.screen.refresh_rows()
        await pilot.pause()
        statuses["build"] = app.state_of("cube").status("build")

    drive(app, scenario)

    assert statuses["build"] is StepStatus.DONE


# ------------------------------------------------------------------- launching


def test_the_launch_dialog_proposes_workers_that_actually_fit() -> None:
    """The tile phase is bound by memory, so proposing every core proposes a refusal."""
    assert suggested_workers(CUBE_CITY) >= 1


def test_the_launch_dialog_remembers_what_was_chosen_last_time() -> None:
    fresh = defaults_for(CUBE_CITY, {})
    remembered = defaults_for(CUBE_CITY, {"workers": 3, "max_zoom": 17, "force": True})

    assert remembered["workers"] == 3
    assert remembered["max_zoom"] == 17
    assert remembered["force"] is True
    assert remembered["tile_size"] == fresh["tile_size"], "untouched options keep their default"


def test_the_dialog_options_become_the_command_line(tmp_path: Path) -> None:
    argv = to_argv(
        "cube",
        {
            "workers": 5,
            "tile_size": 256,
            "min_zoom": 12,
            "max_zoom": 19,
            "from_step": "build",
            "to_step": "tiles",
            "cache_dir": "data/lidar/montilla",
            "resume": False,
            "force": True,
        },
        cities_dir=tmp_path / "cities",
        output_root=tmp_path / "out",
        data_root=tmp_path / "data",
    )

    assert argv[:2] == ["run", "cube"]
    assert argv[argv.index("--workers") + 1] == "5"
    assert argv[argv.index("--from") + 1] == "build"
    assert argv[argv.index("--to") + 1] == "tiles"
    assert argv[argv.index("--cache-dir") + 1] == "data/lidar/montilla"
    assert "--no-resume" in argv
    assert "--force" in argv


def test_the_dialog_leaves_out_what_was_not_chosen(tmp_path: Path) -> None:
    argv = to_argv(
        "cube",
        defaults_for(CUBE_CITY, {}),
        cities_dir=tmp_path / "cities",
        output_root=tmp_path / "out",
        data_root=tmp_path / "data",
    )

    assert "--from" not in argv
    assert "--cache-dir" not in argv
    assert "--force" not in argv
    assert "--resume" in argv


def test_pressing_r_opens_the_dialog_and_launching_is_detached(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rule the whole design rests on: closing the console must not stop a build."""
    launched: list[list[str]] = []

    class FakePopen:
        def __init__(self, argv: list[str], **kwargs: object) -> None:
            launched.append(argv)
            assert kwargs["start_new_session"] is True, "a chain must outlive the console"
            self.pid = 4242

    monkeypatch.setattr("shade_pipeline.console.jobs.subprocess.Popen", FakePopen)
    app = _app(workspace)

    async def scenario(pilot: Any) -> None:
        await pilot.press("enter")  # open the city
        await pilot.pause()
        await pilot.press("r")  # open the launch dialog
        await pilot.pause()
        await pilot.press("ctrl+r")  # accept it
        await pilot.pause()

    drive(app, scenario)

    assert launched, "the dialog should have launched a chain"
    assert launched[0][1:3] == ["run", "cube"]
    assert _state(workspace).preferences, "the choice should have been remembered"


def test_a_city_with_a_step_running_is_not_launched_again(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two supervisors writing one artifact directory is not a recoverable state."""
    launched: list[list[str]] = []
    monkeypatch.setattr(
        "shade_pipeline.console.jobs.subprocess.Popen",
        lambda argv, **kwargs: launched.append(argv),
    )
    state = _state(workspace)
    log, events = state.paths_for("build")
    state.begin("build", log=log, events=events)  # records this very much alive pid
    app = _app(workspace)

    async def scenario(pilot: Any) -> None:
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press("r")
        await pilot.pause()

    drive(app, scenario)

    assert launched == []


def test_the_switches_round_trip_through_the_dialog(workspace: Path) -> None:
    app = _app(workspace)
    collected: dict[str, Any] = {}

    async def scenario(pilot: Any) -> None:
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press("r")
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, LaunchScreen)
        screen.query_one("#force", Switch).value = True
        screen.query_one("#workers", Input).value = "3"
        await pilot.pause()
        collected.update(screen._collect() or {})

    drive(app, scenario)

    assert collected["force"] is True
    assert collected["workers"] == 3


def test_the_dialog_refuses_an_impossible_zoom_range(workspace: Path) -> None:
    app = _app(workspace)
    collected: list[Any] = []

    async def scenario(pilot: Any) -> None:
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press("r")
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, LaunchScreen)
        screen.query_one("#min_zoom", Input).value = "19"
        screen.query_one("#max_zoom", Input).value = "12"
        await pilot.pause()
        collected.append(screen._collect())

    drive(app, scenario)

    assert collected == [None]


# -------------------------------------------------------------------- progress


def test_progress_comes_from_the_event_stream_not_from_the_prose(workspace: Path) -> None:
    """ "Running" becomes "unit 19 of 85, eta 5h 23m" without parsing a sentence."""
    state = _state(workspace)
    log, events = state.paths_for("tiles")
    state.begin("tiles", log=log, events=events)
    with JsonlSink(events) as sink:
        emit(sink, "tiles", "started", units=85)
        emit(sink, "tiles", "unit", label="20260621T0900", done=19, total=85, eta_s=19380.0)

    progress = progress_of(state, "tiles")

    assert progress is not None
    assert (progress.done, progress.total) == (19, 85)
    assert "19/85" in progress.describe()
    assert "5h 23m" in progress.describe()


def test_progress_is_read_from_the_tail_of_a_long_stream(workspace: Path) -> None:
    """A four-hour sweep writes hundreds of records; only the end of it is read."""
    state = _state(workspace)
    log, events = state.paths_for("build")
    state.begin("build", log=log, events=events)
    with JsonlSink(events) as sink:
        for done in range(1, 801):
            emit(sink, "build", "tile", done=done, total=800, eta_s=float(800 - done))

    progress = progress_of(state, "build")

    assert progress is not None
    assert progress.done == 800
    assert progress.fraction == 1.0


def test_no_events_yet_is_not_an_error(workspace: Path) -> None:
    state = _state(workspace)
    log, events = state.paths_for("build")
    state.begin("build", log=log, events=events)

    assert progress_of(state, "build") is None


# -------------------------------------------------------------- editing config


def test_editing_a_setting_writes_the_yaml_and_stales_the_build(workspace: Path) -> None:
    """The point of editing here: the consequence is mechanical, not remembered."""
    state = _state(workspace)
    log, events = state.paths_for("build")
    state.begin("build", log=log, events=events)
    state.complete("build")
    assert state.status("build") is StepStatus.DONE
    app = _app(workspace)

    async def scenario(pilot: Any) -> None:
        await pilot.press("enter")
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, CityScreen)
        screen.save_setting("horizon_sectors", "128")
        await pilot.pause()

    drive(app, scenario)

    text = (workspace / "cities" / "cube.yaml").read_text(encoding="utf-8")
    assert "horizon_sectors: 128" in text
    assert _state(workspace).status("build") is StepStatus.STALE


def test_a_bad_value_does_not_reach_the_file(workspace: Path) -> None:
    before = (workspace / "cities" / "cube.yaml").read_text(encoding="utf-8")
    app = _app(workspace)

    async def scenario(pilot: Any) -> None:
        await pilot.press("enter")
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, CityScreen)
        screen.save_setting("horizon_sectors", "nonsense")
        await pilot.pause()

    drive(app, scenario)

    assert (workspace / "cities" / "cube.yaml").read_text(encoding="utf-8") == before


def test_the_locked_settings_are_refused_with_a_reason(workspace: Path) -> None:
    """bbox and area belong to `shade-engine area`, which knows how to snap them."""
    before = (workspace / "cities" / "cube.yaml").read_text(encoding="utf-8")
    app = _app(workspace)
    shown: dict[str, str] = {}

    async def scenario(pilot: Any) -> None:
        await pilot.press("enter")
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, CityScreen)
        screen.explain("bbox")
        await pilot.pause()
        shown["text"] = str(screen.query_one("#explain", Static).render())
        screen.save_setting("bbox", "[0, 0, 1, 1]")
        await pilot.pause()

    drive(app, scenario)

    assert "belongs to" in shown["text"]
    assert (workspace / "cities" / "cube.yaml").read_text(encoding="utf-8") == before


def test_a_setting_explains_itself_with_its_cost(workspace: Path) -> None:
    app = _app(workspace)
    shown: dict[str, str] = {}

    async def scenario(pilot: Any) -> None:
        await pilot.press("enter")
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, CityScreen)
        screen.explain("horizon_sectors")
        await pilot.pause()
        shown["text"] = str(screen.query_one("#explain", Static).render())

    drive(app, scenario)

    assert "Azimuth sectors" in shown["text"]
    assert "doubles both the sweep" in shown["text"]
    assert "ADR-001" in shown["text"]


# --------------------------------------------------------------- the new city


def test_the_crs_is_derived_from_the_point_not_asked_for() -> None:
    """The field easiest to get wrong by hand, and one that needs no hand at all."""
    code, description = utm_crs(37.88, -4.78)  # Cordoba

    assert code == "EPSG:25830", "the zone the hand-written cordoba.yaml uses"
    assert "30N" in description


def test_a_point_outside_the_european_datum_still_resolves() -> None:
    code, _ = utm_crs(-33.45, -70.67)  # Santiago de Chile

    assert code.startswith("EPSG:")


def test_the_drawing_url_opens_over_the_city() -> None:
    assert drawing_url(37.88, -4.78) == "https://geojson.io/#map=13/37.8800/-4.7800"


def test_the_newest_export_is_the_one_picked_up(tmp_path: Path) -> None:
    old = tmp_path / "old.geojson"
    new = tmp_path / "new.geojson"
    old.write_text("{}", encoding="utf-8")
    new.write_text("{}", encoding="utf-8")
    os.utime(old, (1, 1))

    assert newest_geojson(tmp_path) == new
    assert newest_geojson(tmp_path / "nowhere") is None


MONTALBAN = {
    "type": "Polygon",
    "coordinates": [
        [
            [-4.754379, 37.589995],
            [-4.742928, 37.589995],
            [-4.742928, 37.572506],
            [-4.754379, 37.572506],
            [-4.754379, 37.589995],
        ]
    ],
}


def test_the_polygon_decides_the_crs_not_what_you_typed(workspace: Path) -> None:
    """The regression: a longitude typed without its minus sign.

    It put a town beside Montilla into UTM 31N, and because the bbox came from
    the polygon while the CRS came from the point, the two disagreed by nine
    degrees with nothing in between to notice. The point is now only a map
    centre.
    """
    import json

    polygon = workspace / "montalban.geojson"
    polygon.write_text(json.dumps(MONTALBAN), encoding="utf-8")
    app = _app(workspace)
    seen: dict[str, Any] = {}

    async def scenario(pilot: Any) -> None:
        await pilot.press("n")
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, NewCityScreen)
        screen.query_one("#city_id", Input).value = "montalban"
        screen.query_one("#lat", Input).value = "37.59"
        screen.query_one("#lon", Input).value = "4.75"  # the minus sign, forgotten
        screen.query_one("#polygon", Input).value = str(polygon)
        await pilot.pause()
        seen["crs"] = screen.city_crs()
        draft = screen._draft()
        seen["bbox"] = None if draft is None else draft.bbox

    drive(app, scenario)

    assert seen["crs"][0] == "EPSG:25830", "the polygon is at 4.75 W, whatever was typed"
    assert seen["bbox"][0] > 0, "a UTM easting can never be negative"


def test_a_polygon_path_that_is_not_there_says_so(workspace: Path) -> None:
    """The screen knew exactly what was wrong and reported something else.

    ``cuesta-banca.geojson`` was typed for a file called ``cuesta-blanca``, and
    all three panels said the polygon was missing: the derived one went back to
    "drop a .geojson in Descargas", the cost one to "an id and a polygon", and
    saving answered "needs an id and a polygon" -- with an id typed and a path
    typed, the one sentence that could not be true. ``read_area`` had composed
    the real message and the screen threw it away.
    """
    app = _app(workspace)
    seen: dict[str, Any] = {}

    async def scenario(pilot: Any) -> None:
        await pilot.press("n")
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, NewCityScreen)
        screen.query_one("#city_id", Input).value = "cuesta-blanca"
        screen.query_one("#polygon", Input).value = str(workspace / "cuesta-banca.geojson")
        await pilot.pause()
        seen["derived"] = str(screen.query_one("#derived", Static).render())
        screen.action_save()
        await pilot.pause()

    drive(app, scenario)

    assert "cuesta-banca.geojson" in seen["derived"], "name the file that is not there"
    assert "cannot be read" in seen["derived"]
    assert "drop a .geojson" not in seen["derived"], "a path was typed; do not pretend otherwise"
    assert not (workspace / "cities" / "cuesta-blanca.yaml").exists()


def test_a_polygon_that_is_not_json_says_which_file_and_why(workspace: Path) -> None:
    """Same swallow, second cause: an export that is not what it claims to be."""
    broken = workspace / "half-an-export.geojson"
    broken.write_text('{"type": "FeatureColl', encoding="utf-8")
    app = _app(workspace)
    seen: dict[str, Any] = {}

    async def scenario(pilot: Any) -> None:
        await pilot.press("n")
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, NewCityScreen)
        screen.query_one("#city_id", Input).value = "roto"
        screen.query_one("#polygon", Input).value = str(broken)
        await pilot.pause()
        seen["derived"] = str(screen.query_one("#derived", Static).render())

    drive(app, scenario)

    assert "half-an-export.geojson" in seen["derived"]
    assert "not valid JSON" in seen["derived"]


def test_the_reason_clears_once_the_polygon_is_good(workspace: Path) -> None:
    """Otherwise the first typo would sit on the screen for the rest of the session."""
    import json

    polygon = workspace / "montalban.geojson"
    polygon.write_text(json.dumps(MONTALBAN), encoding="utf-8")
    app = _app(workspace)
    seen: dict[str, Any] = {}

    async def scenario(pilot: Any) -> None:
        await pilot.press("n")
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, NewCityScreen)
        screen.query_one("#city_id", Input).value = "montalban"
        screen.query_one("#polygon", Input).value = str(workspace / "typo.geojson")
        await pilot.pause()
        seen["first"] = str(screen.query_one("#derived", Static).render())
        screen.query_one("#polygon", Input).value = str(polygon)
        await pilot.pause()
        seen["second"] = str(screen.query_one("#derived", Static).render())

    drive(app, scenario)

    assert "typo.geojson" in seen["first"]
    assert "EPSG:25830" in seen["second"]
    assert "typo.geojson" not in seen["second"]


def test_without_a_polygon_the_point_is_only_a_map_centre(workspace: Path) -> None:
    """It proposes a CRS to look at, but there is nothing to write yet."""
    app = _app(workspace)
    seen: dict[str, Any] = {}

    async def scenario(pilot: Any) -> None:
        await pilot.press("n")
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, NewCityScreen)
        screen.query_one("#city_id", Input).value = "sinpoly"
        screen.query_one("#lat", Input).value = "37.59"
        screen.query_one("#lon", Input).value = "-4.75"
        await pilot.pause()
        seen["crs"] = screen.city_crs()
        seen["draft"] = screen._draft()
        seen["derived"] = str(screen.query_one("#derived", Static).render())

    drive(app, scenario)

    assert seen["crs"][0] == "EPSG:25830"
    assert seen["draft"] is None, "no polygon, no city"
    assert "provisional" in seen["derived"]


def test_the_new_city_screen_shows_the_derived_crs(workspace: Path) -> None:
    app = _app(workspace)
    shown: dict[str, str] = {}

    async def scenario(pilot: Any) -> None:
        await pilot.press("n")
        await pilot.pause()
        screen = app.screen
        screen.query_one("#lat", Input).value = "37.88"
        screen.query_one("#lon", Input).value = "-4.78"
        await pilot.pause()
        shown["derived"] = str(screen.query_one("#derived", Static).render())

    drive(app, scenario)

    assert "EPSG:25830" in shown["derived"]
    assert "geojson.io" in shown["derived"]


# ------------------------------------------------------------------- utilities


def test_every_utility_says_what_it_is_for() -> None:
    assert {utility.key for utility in UTILITIES} >= {"verify", "canopy", "recolor", "predict"}
    for utility in UTILITIES:
        assert len(utility.summary) > 40, f"{utility.key} needs a real explanation"


def test_a_utility_becomes_the_right_command_line(tmp_path: Path) -> None:
    verify = utility_argv(
        "verify", "cube", "", cities_dir=tmp_path / "cities", output_root=tmp_path / "out"
    )
    assert verify[:2] == ["verify", "cube"]
    assert "--cities-dir" in verify

    recolor = utility_argv(
        "recolor", "cube", "light", cities_dir=tmp_path / "cities", output_root=tmp_path / "out"
    )
    assert recolor[:2] == ["recolor", "cube"]
    assert recolor[recolor.index("--palette") + 1] == "light"
    assert "--cities-dir" not in recolor, "recolor reads the tile tree, not the config"

    layer = utility_argv(
        "import-layer", "cube", "parking", cities_dir=tmp_path / "cities", output_root=tmp_path
    )
    assert layer[:3] == ["import-layer", "cube", "parking"]


def test_the_assets_utility_is_about_the_machine_and_not_the_city(tmp_path: Path) -> None:
    """Glyphs and sprites are the same bytes everywhere and live once beside the cities.

    Reached from a city screen only because that is where the utilities are.
    Handing it a city id would name a directory it is not going to touch.
    """
    argv = utility_argv(
        "assets", "cube", "", cities_dir=tmp_path / "cities", output_root=tmp_path / "out"
    )

    assert argv == ["assets", "--output-root", str(tmp_path / "out")]
    assert "cube" not in argv


# --------------------------------------------------------------------- publish


def test_the_log_can_be_copied_with_a_key(workspace: Path) -> None:
    """Mouse selection varies by terminal, multiplexer and ssh; a key does not."""
    state = _state(workspace)
    log, events = state.paths_for("build")
    state.begin("build", log=log, events=events)
    log.write_text("swept tile [12/40]\nTraceback (most recent call last):\n", encoding="utf-8")
    app = _app(workspace)
    copied: list[str] = []

    async def scenario(pilot: Any) -> None:
        app.copy_to_clipboard = copied.append  # type: ignore[assignment]
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press("y")
        await pilot.pause()

    drive(app, scenario)

    assert copied and "Traceback" in copied[0]


def test_publishing_a_city_with_nothing_built_is_refused_before_it_is_offered(
    workspace: Path,
) -> None:
    """What you are asked to confirm is only ever a publish that would work."""
    app = _app(workspace)

    result = app.publish_plan("cube")

    assert isinstance(result, str)
    assert "no artifacts" in result


def test_unpublishing_shows_what_it_would_delete_before_asking(workspace: Path) -> None:
    """Two of those commands are `rm -rf` on a production server."""
    app = _app(workspace)

    plan = app.unpublish_plan("cube")

    assert not isinstance(plan, str)
    rendered = plan.render()
    assert "rm -rf /opt/shade/data/cities/cube" in rendered
    assert "rm -rf /opt/shade/live/cities/cube.yaml" in rendered


def test_unpublish_is_offered_for_a_city_with_nothing_built(workspace: Path) -> None:
    """Unlike publish: what you are removing does not have to be fit to serve.

    Often the reason to remove it is precisely that it is not.
    """
    app = _app(workspace)

    assert isinstance(app.publish_plan("cube"), str)
    assert not isinstance(app.unpublish_plan("cube"), str)


def test_unpublish_asks_before_deleting_anything(workspace: Path) -> None:
    """`U` must reach a confirmation and nothing else; it deletes from production."""
    app = _app(workspace)
    seen: dict[str, Any] = {}

    async def scenario(pilot: Any) -> None:
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press("U")
        await pilot.pause()
        seen["screen"] = app.screen

    drive(app, scenario)

    screen = seen["screen"]
    assert isinstance(screen, ConfirmScreen)
    assert "rm -rf /opt/shade/data/cities/cube" in screen.body
    assert "rm -rf /opt/shade/live/cities/cube.yaml" in screen.body


def test_preview_is_a_toggle_and_not_a_launcher(workspace: Path) -> None:
    """Pressing v three times used to leave three previews.

    A preview does not stop by itself, and the second and third could not take
    port 5173, so vite walked forward to 5174 and 5175 while the browser stayed
    on 5173 showing the first one.
    """
    state = _state(workspace)
    log, events = state.paths_for("preview")
    state.begin("preview", log=log, events=events)
    app = _app(workspace)
    signalled: list[tuple[int, int]] = []

    async def scenario(pilot: Any) -> None:
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press("v")
        await pilot.pause()

    with patch(
        "shade_pipeline.console.city.os.kill", lambda pid, sig: signalled.append((pid, sig))
    ):
        drive(app, scenario)

    # os.kill is also how a record checks its process is alive (signal 0), so
    # only the real signal is interesting here.
    assert [call for call in signalled if call[1] != 0] == [(os.getpid(), signal.SIGTERM)]


def test_the_log_tab_follows_a_preview_too(workspace: Path) -> None:
    """It is the log you are most likely to be reading while it runs."""
    state = _state(workspace)
    log, events = state.paths_for("preview")
    state.begin("preview", log=log, events=events)
    log.write_text("vite ready in 432 ms\n", encoding="utf-8")
    app = _app(workspace)
    shown: list[str] = []

    async def scenario(pilot: Any) -> None:
        await pilot.press("enter")
        await pilot.pause()
        app.copy_to_clipboard = shown.append  # type: ignore[assignment]
        await pilot.press("y")
        await pilot.pause()

    drive(app, scenario)

    assert shown and "vite ready" in shown[0]


def test_a_running_preview_has_a_row_of_its_own(workspace: Path) -> None:
    """Five presses of v looked exactly like one, because nothing showed it."""
    state = _state(workspace)
    log, events = state.paths_for("preview")
    state.begin("preview", params={"web_port": 5173}, log=log, events=events)
    app = _app(workspace)
    rows: list[str] = []

    async def scenario(pilot: Any) -> None:
        await pilot.press("enter")
        await pilot.pause()
        table = app.screen.query_one("#steps", DataTable)
        for key in table.rows:
            rows.append(" ".join(str(cell) for cell in table.get_row(key)))

    drive(app, scenario)

    preview_row = next(row for row in rows if row.startswith("preview"))
    assert "running" in preview_row
    assert "127.0.0.1:5173" in preview_row


def test_the_log_tab_ignores_a_record_whose_file_was_never_written(workspace: Path) -> None:
    """A step that died before opening its log is still the newest record there is.

    Following it leaves the tab blank with nothing to explain why -- which is
    what a preview refused for a busy port did to every later one.
    """
    state = _state(workspace)
    real, events = state.paths_for("build")
    state.begin("build", log=real, events=events)
    real.write_text("swept tile [12/40]\n", encoding="utf-8")
    state.complete("build")
    missing, events = state.paths_for("preview")
    state.begin("preview", log=missing, events=events)
    state.fail("preview", "already listening on 127.0.0.1:5173")
    assert not missing.exists()

    app = _app(workspace)
    copied: list[str] = []

    async def scenario(pilot: Any) -> None:
        await pilot.press("enter")
        await pilot.pause()
        app.copy_to_clipboard = copied.append  # type: ignore[assignment]
        await pilot.press("y")
        await pilot.pause()

    drive(app, scenario)

    assert copied and "swept tile" in copied[0]


def test_pressing_v_twice_while_it_shuts_down_does_not_signal_twice(workspace: Path) -> None:
    """Tearing the servers down takes seconds; the record only changes at the end.

    Signalling again in the meantime cannot help -- and a second SIGTERM used to
    land inside the cleanup and abandon it half done.
    """
    state = _state(workspace)
    log, events = state.paths_for("preview")
    state.begin("preview", params={"web_port": 5173}, log=log, events=events)
    app = _app(workspace)
    signalled: list[tuple[int, int]] = []

    async def scenario(pilot: Any) -> None:
        await pilot.press("enter")
        await pilot.pause()
        for _ in range(4):
            await pilot.press("v")
            await pilot.pause()

    with patch(
        "shade_pipeline.console.city.os.kill", lambda pid, sig: signalled.append((pid, sig))
    ):
        drive(app, scenario)

    assert [call for call in signalled if call[1] != 0] == [(os.getpid(), signal.SIGTERM)]


# ------------------------------------------------------------ surviving bad data

HALF_SAVED = 'id: roto\nname: "sin cerrar\n'
"""A city file caught mid-save: the quote is open, so this is not YAML at all."""

PYDANTIC_NOISE = (
    "1 validation error for CityConfig\ntimezone\n  Value error, unknown timezone "
    "[type=value_error, input_value='Europe/Madri', input_type=str]"
)
"""A real pydantic message. The brackets are console markup to a Static."""


def test_a_city_file_caught_mid_save_does_not_stop_the_console(workspace: Path) -> None:
    """One unreadable city file and the console would not open at all.

    ``RunState.open`` fingerprints the config, so the read happens before a
    single row is painted, and nothing in between was catching it: the app died
    with a traceback in the terminal instead of starting.
    """
    (workspace / "cities" / "roto.yaml").write_text(HALF_SAVED, encoding="utf-8")
    app = _app(workspace)
    seen: dict[str, Any] = {}

    async def scenario(pilot: Any) -> None:
        table = app.screen.query_one("#cities", DataTable)
        seen["cities"] = [str(table.get_row_at(row)[0]) for row in range(table.row_count)]
        seen["cells"] = [str(cell) for cell in table.get_row_at(1)]
        seen["hint"] = str(app.screen.query_one("#hint", Static).render())

    drive(app, scenario)

    assert seen["cities"] == ["cube", "roto"], "the broken one is a row, not the end of the app"
    assert all(cell == "error" for cell in seen["cells"][1:]), seen["cells"]
    assert "roto" in seen["hint"], "say which city, and why"


def test_a_city_file_missing_fields_is_a_row_and_not_a_crash(workspace: Path) -> None:
    """Same crash, the other half of it: valid YAML that is not a city."""
    (workspace / "cities" / "roto.yaml").write_text("id: roto\n", encoding="utf-8")
    app = _app(workspace)
    seen: dict[str, Any] = {}

    async def scenario(pilot: Any) -> None:
        table = app.screen.query_one("#cities", DataTable)
        seen["cities"] = [str(table.get_row_at(row)[0]) for row in range(table.row_count)]

    drive(app, scenario)

    assert seen["cities"] == ["cube", "roto"]


def test_a_city_that_breaks_while_its_screen_is_open_survives_the_tick(workspace: Path) -> None:
    """The file can break under a screen that is already showing it."""
    app = _app(workspace)
    seen: dict[str, Any] = {}

    async def scenario(pilot: Any) -> None:
        await pilot.press("o")
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, CityScreen)
        (workspace / "cities" / "cube.yaml").write_text(HALF_SAVED, encoding="utf-8")
        screen.refresh_steps()
        await pilot.pause()
        table = screen.query_one("#steps", DataTable)
        seen["cells"] = [str(cell) for cell in table.get_row_at(0)]

    drive(app, scenario)

    assert "error" in seen["cells"][1]
    assert "cube.yaml" in seen["cells"][-1], "name the file that cannot be read"


def test_a_timezone_half_typed_does_not_take_the_form_with_it(workspace: Path) -> None:
    """``CityConfig(...)`` sat outside the try that already wrapped the polygon.

    A timezone with one letter missing is a ValidationError, it reached the key
    handler, and the whole registration form went with it.
    """
    import json

    polygon = workspace / "montalban.geojson"
    polygon.write_text(json.dumps(MONTALBAN), encoding="utf-8")
    app = _app(workspace)
    seen: dict[str, Any] = {}

    async def scenario(pilot: Any) -> None:
        await pilot.press("n")
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, NewCityScreen)
        screen.query_one("#city_id", Input).value = "montalban"
        screen.query_one("#polygon", Input).value = str(polygon)
        screen.query_one("#timezone", Input).value = "Europe/Madri"
        await pilot.pause()
        screen.action_save()
        await pilot.pause()
        seen["screen"] = type(app.screen).__name__
        seen["cost"] = str(screen.query_one("#cost-text", Static).render())

    drive(app, scenario)

    assert seen["screen"] == "NewCityScreen", "the form survives its own bad value"
    assert "Europe/Madri" in seen["cost"], "and says what is wrong with it"
    assert not (workspace / "cities" / "montalban.yaml").exists()


def test_an_error_with_brackets_is_shown_and_not_parsed(workspace: Path) -> None:
    """Pydantic writes ``[type=value_error, ...]`` and a Static reads markup.

    The panels that show text from outside -- the price of a city, the plan a
    confirmation displays -- must render it, not interpret it.
    """
    app = _app(workspace)
    seen: dict[str, Any] = {}

    async def scenario(pilot: Any) -> None:
        await pilot.press("o")
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, CityScreen)
        screen.query_one("#config-cost", CostPanel).clear(PYDANTIC_NOISE)
        await pilot.pause()
        seen["cost"] = str(screen.query_one("#cost-text", Static).render())
        app.push_screen(ConfirmScreen("Publish cube", PYDANTIC_NOISE, "Publish"))
        await pilot.pause()
        seen["body"] = str(app.screen.query_one("#body", Static).render())
        # The third route the same text takes. Checked rather than assumed:
        # `notify` renders markup too, by default.
        screen.notify(PYDANTIC_NOISE, severity="error")
        await pilot.pause()

    drive(app, scenario)

    assert "type=value_error" in seen["cost"]
    assert "type=value_error" in seen["body"]
