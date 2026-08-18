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
import subprocess
import sys
import time
from collections.abc import Awaitable, Callable
from datetime import date
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import yaml

from conftest import CUBE_CITY
from shade_pipeline.events import JsonlSink, emit
from shade_pipeline.runstate import LOG_STEPS, RunState, StepStatus

# Everything below this line needs the optional extra, imports included.
pytest.importorskip("textual", reason="the console needs the 'tui' extra")

from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Input,
    Static,
    Switch,
    TabbedContent,
    TextArea,
)
from textual.widgets._footer import FooterKey

from shade_pipeline.area import utm_crs
from shade_pipeline.console.app import ConsoleApp
from shade_pipeline.console.cities import SERVED_STYLE, CitiesScreen
from shade_pipeline.console.city import CityScreen
from shade_pipeline.console.confirm import ConfirmScreen, EditScreen
from shade_pipeline.console.cost import CostPanel
from shade_pipeline.console.jobs import progress_of
from shade_pipeline.console.launch import (
    LaunchScreen,
    defaults_for,
    suggested_workers,
    to_argv,
)
from shade_pipeline.console.newcity import (
    NewCityScreen,
    PasteScreen,
    default_watch_dir,
    drawing_url,
    newest_geojson,
)
from shade_pipeline.console.utilities import UTILITIES, UtilitiesScreen
from shade_pipeline.console.utilities import to_argv as utility_argv
from shade_pipeline.deployed import Comparison, Verdict


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


def drive(
    app: ConsoleApp,
    scenario: Callable[[Any], Awaitable[None]],
    size: tuple[int, int] = (80, 24),
) -> None:
    """Run one scenario against a live app and shut it down again.

    ``size`` is what ``run_test`` uses anyway; naming it here is what lets a
    test say which terminal it is talking about, because layout that only
    works in a wide window is layout that does not work.
    """

    async def main() -> None:
        async with app.run_test(size=size) as pilot:
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
        "served",
    ]


# ------------------------------------------------------------- and the server


def test_every_verdict_has_a_style() -> None:
    """The style map is keyed by strings to keep rasterio off the startup path.

    Which means nothing but this holds the two together.
    """
    assert {verdict.value for verdict in Verdict} == set(SERVED_STYLE)


def test_the_served_column_says_nothing_until_it_is_asked(workspace: Path) -> None:
    """The refresh loop runs twice a second and must never make a request."""
    app = _app(workspace)
    seen: dict[str, Any] = {}

    async def scenario(pilot: Any) -> None:
        table = app.screen.query_one("#cities", DataTable)
        seen["cell"] = str(table.get_row_at(0)[-1])

    drive(app, scenario)

    assert seen["cell"] == "?"


def test_pressing_s_fills_the_served_column(workspace: Path, monkeypatch: Any) -> None:
    app = _app(workspace)
    asked: list[str] = []
    seen: dict[str, Any] = {}

    def fake_survey(cities: list[str], **kwargs: Any) -> list[Comparison]:
        asked.extend(cities)
        return [Comparison(city, Verdict.BEHIND, ["served yesterday"]) for city in cities]

    monkeypatch.setattr("shade_pipeline.deployed.survey", fake_survey)

    async def scenario(pilot: Any) -> None:
        await pilot.press("s")
        await app.workers.wait_for_complete()
        await pilot.pause()
        table = app.screen.query_one("#cities", DataTable)
        seen["cell"] = str(table.get_row_at(0)[-1])

    drive(app, scenario)

    assert asked == ["cube"]
    assert seen["cell"] == "behind"


def test_the_server_tab_reports_the_comparison(workspace: Path, monkeypatch: Any) -> None:
    """Opening the tab is what asks: a city screen otherwise touches no network."""
    app = _app(workspace)
    seen: dict[str, Any] = {}

    def fake_survey(cities: list[str], **kwargs: Any) -> list[Comparison]:
        return [Comparison(cities[0], Verdict.NOT_PUBLISHED, ["the server does not list it"])]

    monkeypatch.setattr("shade_pipeline.deployed.survey", fake_survey)

    async def scenario(pilot: Any) -> None:
        await pilot.press("enter")
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, CityScreen)
        seen["before"] = str(screen.query_one("#server", Static).render())
        await pilot.press("s")
        await app.workers.wait_for_complete()
        await pilot.pause()
        seen["after"] = str(screen.query_one("#server", Static).render())

    drive(app, scenario)

    assert "press" in seen["before"]
    assert "unpublished" in seen["after"]
    assert "does not list it" in seen["after"]


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
        await pilot.pause(0.4)
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
        await pilot.pause(0.4)
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
        await pilot.pause(0.4)
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
        await pilot.pause(0.4)
        seen["first"] = str(screen.query_one("#derived", Static).render())
        screen.query_one("#polygon", Input).value = str(polygon)
        await pilot.pause(0.4)
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


def test_clearing_the_polygon_field_clears_the_polygon(workspace: Path) -> None:
    """`if value.strip():` had no else, so the last path typed survived the delete.

    Emptying the field left the screen pricing a polygon that was no longer
    named anywhere on it.
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
        await pilot.pause(0.4)
        seen["with"] = screen.polygon
        screen.query_one("#polygon", Input).value = ""
        await pilot.pause(0.4)
        seen["without"] = screen.polygon
        await app.workers.wait_for_complete()
        seen["cost"] = str(screen.query_one("#cost-text", Static).render())

    drive(app, scenario)

    assert seen["with"] == polygon
    assert seen["without"] is None, "an emptied field means no polygon"
    assert "an id and a polygon" in seen["cost"]


def test_a_pasted_polygon_needs_neither_a_file_nor_a_path(workspace: Path) -> None:
    """The drawing is copied from geojson.io, so let it be pasted.

    Writing it to a file by hand and then typing that file's path is the step
    this removes; the draft it leaves behind is what `plan_city` prices, since
    pricing reads the area from disk.
    """
    import json

    app = _app(workspace)
    seen: dict[str, Any] = {}

    async def scenario(pilot: Any) -> None:
        await pilot.press("n")
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, NewCityScreen)
        screen.query_one("#city_id", Input).value = "montalban"
        await pilot.press("p")
        await pilot.pause()
        modal = app.screen
        assert isinstance(modal, PasteScreen)
        modal.query_one("#pasted", TextArea).text = json.dumps(MONTALBAN)
        await pilot.press("ctrl+s")
        await pilot.pause(0.4)
        await app.workers.wait_for_complete()
        seen["derived"] = str(screen.query_one("#derived", Static).render())
        seen["cost"] = str(screen.query_one("#cost-text", Static).render())
        seen["path"] = screen.query_one("#polygon", Input).value

    drive(app, scenario)

    assert "EPSG:25830" in seen["derived"], "the polygon decides the CRS, pasted or not"
    assert "cannot price" not in seen["cost"]
    assert seen["path"], "the pasted draft names itself in the path field"
    assert (workspace / "data" / "drafts" / "pasted.geojson").exists()


def test_pasting_something_that_is_not_json_says_so_and_keeps_the_form(
    workspace: Path,
) -> None:
    app = _app(workspace)
    seen: dict[str, Any] = {}

    async def scenario(pilot: Any) -> None:
        await pilot.press("n")
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, NewCityScreen)
        await pilot.press("p")
        await pilot.pause()
        modal = app.screen
        assert isinstance(modal, PasteScreen)
        modal.query_one("#pasted", TextArea).text = "{not json at all"
        await pilot.press("ctrl+s")
        await pilot.pause(0.4)
        seen["screen"] = type(app.screen).__name__
        seen["derived"] = str(screen.query_one("#derived", Static).render())

    drive(app, scenario)

    assert seen["screen"] == "NewCityScreen"
    assert "not valid JSON" in seen["derived"]
    assert not (workspace / "data" / "drafts" / "pasted.geojson").exists(), "nothing written"


def test_q_asks_before_throwing_away_a_form_with_something_in_it(workspace: Path) -> None:
    """`q` quit the whole app from any non-modal screen once focus left an Input.

    With the button focused -- which is one tab away -- it took the entire
    registration form with it, without asking.
    """
    app = _app(workspace)
    seen: dict[str, Any] = {}

    async def scenario(pilot: Any) -> None:
        await pilot.press("n")
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, NewCityScreen)
        screen.query_one("#city_id", Input).value = "montalban"
        screen.query_one("#save", Button).focus()
        await pilot.press("q")
        await pilot.pause()
        seen["screen"] = type(app.screen).__name__
        seen["running"] = app.is_running

    drive(app, scenario)

    assert seen["running"], "the app is still up"
    assert seen["screen"] == "ConfirmScreen", "and it asked"


def test_q_on_an_empty_form_just_goes_back(workspace: Path) -> None:
    """Nothing to lose, nothing to ask."""
    app = _app(workspace)
    seen: dict[str, Any] = {}

    async def scenario(pilot: Any) -> None:
        await pilot.press("n")
        await pilot.pause()
        app.screen.query_one("#save", Button).focus()
        await pilot.press("q")
        await pilot.pause()
        seen["screen"] = type(app.screen).__name__
        seen["running"] = app.is_running

    drive(app, scenario)

    assert seen["running"]
    assert seen["screen"] == "CitiesScreen"


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


def test_predict_runs_for_a_day_that_is_asked_for_and_not_a_fixed_one(tmp_path: Path) -> None:
    """It carried `--day 2026-08-16`, so it predicted a day already gone, always.

    `predict` is the tool for checking the model against somebody standing in
    the street, and that person is standing there today.
    """
    chosen = utility_argv(
        "predict",
        "cube",
        "points.csv",
        cities_dir=tmp_path / "cities",
        output_root=tmp_path / "out",
        day=date(2026, 9, 1),
    )
    assert chosen[chosen.index("--day") + 1] == "2026-09-01"

    today = utility_argv(
        "predict",
        "cube",
        "points.csv",
        cities_dir=tmp_path / "cities",
        output_root=tmp_path / "out",
    )
    assert today[today.index("--day") + 1] == date.today().isoformat()


def test_a_palette_is_never_filtered_out_of_the_command_line(tmp_path: Path) -> None:
    """`recolor` took no --cities-dir, and the way it took none was to remove it.

    Filtering the argv by value removes whatever equals the directory -- so a
    palette that happens to be that string disappears and recolor is left
    without the argument it needs.
    """
    cities_dir = tmp_path / "cities"
    argv = utility_argv(
        "recolor", "cube", str(cities_dir), cities_dir=cities_dir, output_root=tmp_path / "out"
    )

    assert argv[argv.index("--palette") + 1] == str(cities_dir), "a palette is not a flag"
    assert "--cities-dir" not in argv


def test_q_in_the_utilities_screen_goes_back_instead_of_quitting(workspace: Path) -> None:
    app = _app(workspace)
    seen: dict[str, Any] = {}

    async def scenario(pilot: Any) -> None:
        await pilot.press("o")
        await pilot.pause()
        await pilot.press("u")
        await pilot.pause()
        assert isinstance(app.screen, UtilitiesScreen)
        await pilot.press("q")
        await pilot.pause()
        seen["screen"] = type(app.screen).__name__
        seen["running"] = app.is_running

    drive(app, scenario)

    assert seen["running"]
    assert seen["screen"] == "CityScreen"


def test_the_watched_directory_is_one_that_exists_or_none(tmp_path: Path) -> None:
    """`~/Descargas` is not on this machine, and the screen promised it anyway.

    Nothing is watched when none of the candidates is there, and the screen
    stops telling people to drop a file into a directory that does not exist.
    """
    there = tmp_path / "Downloads"
    there.mkdir()

    with patch(
        "shade_pipeline.console.newcity.watch_candidates",
        lambda: (tmp_path / "Descargas", there),
    ):
        assert default_watch_dir() == there

    with patch(
        "shade_pipeline.console.newcity.watch_candidates", lambda: (tmp_path / "Descargas",)
    ):
        assert default_watch_dir() is None


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
    # Every step cell, and not the served one: a YAML that cannot be parsed says
    # nothing at all about what the server is holding.
    assert all(cell == "error" for cell in seen["cells"][1:-1]), seen["cells"]
    assert seen["cells"][-1] == "?"
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
        await pilot.pause(0.4)
        screen.action_save()
        await pilot.pause()
        await app.workers.wait_for_complete()
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


# -------------------------------------------------------------- fits on screen


async def open_config_tab(pilot: Any, app: ConsoleApp) -> CityScreen:
    """The Config tab of the first city, which is where the price lives."""
    await pilot.press("o")
    await pilot.pause()
    screen = app.screen
    assert isinstance(screen, CityScreen)
    screen.query_one(TabbedContent).active = "config-pane"
    await pilot.pause()
    return screen


def test_the_price_of_a_city_is_on_screen_in_an_80x24_terminal(workspace: Path) -> None:
    """The panel that makes the point of the whole console was off the screen.

    Measured before the fix: region y=25, height=1, on a screen 24 rows tall.
    The settings table is `height: auto`, so it grew with the number of fields
    and pushed the price out of the bottom.
    """
    app = _app(workspace)
    seen: dict[str, Any] = {}

    async def scenario(pilot: Any) -> None:
        screen = await open_config_tab(pilot, app)
        seen["cost"] = screen.query_one("#config-cost", CostPanel).region
        seen["config"] = screen.query_one("#config", DataTable).region
        seen["screen"] = screen.region

    drive(app, scenario, size=(80, 24))

    assert seen["cost"].bottom <= seen["screen"].bottom, "the price is inside the terminal"
    assert seen["cost"].height >= 6, f"and readable, not a sliver: {seen['cost']}"
    assert seen["config"].height >= 5, f"the settings stay navigable: {seen['config']}"


def test_a_wider_terminal_gives_the_price_more_room(workspace: Path) -> None:
    app = _app(workspace)
    seen: dict[str, Any] = {}

    async def scenario(pilot: Any) -> None:
        screen = await open_config_tab(pilot, app)
        seen["cost"] = screen.query_one("#config-cost", CostPanel).region

    drive(app, scenario, size=(120, 30))

    assert seen["cost"].height >= 10, seen["cost"]


def test_no_shortcut_falls_off_the_footer_at_80_columns(workspace: Path) -> None:
    """Eight shortcuts summed 85 columns, so the last one hung off the edge.

    The command palette's own key made it worse: it is painted on the right of
    the footer and landed on top of the shortcut before it.
    """
    app = _app(workspace)
    seen: dict[str, list[tuple[str, int]]] = {}

    def overflowing(screen: Any) -> list[tuple[str, int]]:
        return [
            (key.key, key.region.right)
            for key in screen.query(FooterKey)
            if key.region.right > screen.region.width
        ]

    async def scenario(pilot: Any) -> None:
        seen["cities"] = overflowing(app.screen)
        await pilot.press("o")
        await pilot.pause()
        seen["city"] = overflowing(app.screen)

    drive(app, scenario, size=(80, 24))

    assert seen["cities"] == []
    assert seen["city"] == []


def test_every_modal_announces_its_keys(workspace: Path) -> None:
    """`ctrl+r` launches a build from the modal that offers it, and was invisible."""
    app = _app(workspace)
    seen: dict[str, bool] = {}

    async def scenario(pilot: Any) -> None:
        await pilot.press("o")
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, CityScreen)
        app.push_screen(ConfirmScreen("Publish cube", "a plan", "Publish"))
        await pilot.pause()
        seen["confirm"] = bool(app.screen.query(Footer))
        app.pop_screen()
        await pilot.pause()
        app.push_screen(EditScreen("horizon_sectors", "64", "doubles the sweep"))
        await pilot.pause()
        seen["edit"] = bool(app.screen.query(Footer))
        app.pop_screen()
        await pilot.pause()
        await pilot.press("r")
        await pilot.pause()
        assert isinstance(app.screen, LaunchScreen)
        seen["launch"] = bool(app.screen.query(Footer))

    drive(app, scenario)

    assert seen == {"confirm": True, "edit": True, "launch": True}


def test_the_whole_reason_a_step_failed_can_be_read(workspace: Path) -> None:
    """The detail column truncates at 70 characters, silently.

    A traceback tail or a coverage error naming every missing tile is longer
    than that, and the rest of it had nowhere to go.
    """
    reason = (
        "CoverageError: 7 lidar tiles missing for the requested bbox: "
        "PNOA-2019-AND-338-4200, PNOA-2019-AND-340-4200, PNOA-2019-AND-342-4200, "
        "PNOA-2019-AND-344-4200 and 3 more; download them or narrow the area"
    )
    state = _state(workspace)
    log, events = state.paths_for("build")
    state.begin("build", log=log, events=events)
    state.fail("build", reason)
    app = _app(workspace)
    seen: dict[str, Any] = {}

    async def scenario(pilot: Any) -> None:
        await pilot.press("o")
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, CityScreen)
        table = screen.query_one("#steps", DataTable)
        row = [str(cell) for cell in table.get_row_at(LOG_STEPS.index("build"))]
        seen["cell"] = row[-1]
        table.move_cursor(row=LOG_STEPS.index("build"))
        await pilot.press("enter")
        await pilot.pause()
        seen["modal"] = type(app.screen).__name__
        seen["shown"] = str(app.screen.query_one("#detail-body", Static).render())

    drive(app, scenario)

    assert len(seen["cell"]) < len(reason), "the cell still truncates; a table cell must"
    assert seen["modal"] == "DetailScreen"
    assert "and 3 more" in seen["shown"], "the end of the message is the part that was lost"


# ------------------------------------------------------------ off the event loop


def test_opening_the_console_does_not_import_the_geospatial_stack() -> None:
    """Startup was 1.604 ms of imports before a single row was painted.

    Around 950 of those were pyproj, shapely and rasterio, arriving through
    `area`, `build`, `publish` and `runner` -- none of which the opening table
    needs. This test is what keeps them from creeping back in unnoticed.
    """
    probe = (
        "import sys, shade_pipeline.console.app; "
        "print(sorted(m for m in ('rasterio', 'pyproj', 'shapely', 'laspy') if m in sys.modules))"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True
    )

    assert result.stdout.strip() == "[]", f"the console is importing the engine again: {result}"


def test_pricing_a_city_does_not_block_the_interface(workspace: Path) -> None:
    """191 ms for Cordoba, measured, and every one of them was the UI not answering."""
    import shade_pipeline.area as area_module

    def slow_and_broken(*args: Any, **kwargs: Any) -> Any:
        time.sleep(0.3)
        raise ValueError("slow on purpose")

    app = _app(workspace)
    seen: dict[str, Any] = {}

    async def scenario(pilot: Any) -> None:
        screen = await open_config_tab(pilot, app)
        await app.workers.wait_for_complete()
        panel = screen.query_one("#config-cost", CostPanel)
        with patch.object(area_module, "plan_city", slow_and_broken):
            started = time.perf_counter()
            worker = panel.show(app.config_of("cube"))
            seen["returned_ms"] = (time.perf_counter() - started) * 1000
            # Right here, with the thread still sleeping: the panel says it is
            # working before anything has come back.
            seen["pricing"] = panel.has_class("pricing")
            await worker.wait()
        await pilot.pause()
        seen["text"] = str(screen.query_one("#cost-text", Static).render())

    drive(app, scenario)

    assert seen["returned_ms"] < 50, (
        f"show() waited for the arithmetic: {seen['returned_ms']:.0f}ms"
    )
    assert seen["pricing"], "and says it is working while it does"
    assert "slow on purpose" in seen["text"]


def test_the_last_price_asked_for_is_the_one_shown(workspace: Path) -> None:
    """Typing 128 one digit at a time used to leave several prices racing.

    `exclusive` cancels the previous run of the group; a thread cannot be
    interrupted, so the loser finishes and drops its answer instead of painting
    a price for a configuration nobody asked about any more.
    """
    app = _app(workspace)
    seen: dict[str, Any] = {}
    first = CUBE_CITY.model_copy(update={"id": "stale-answer"})
    last = CUBE_CITY.model_copy(update={"id": "the-one-asked-for"})

    async def scenario(pilot: Any) -> None:
        screen = await open_config_tab(pilot, app)
        await app.workers.wait_for_complete()
        panel = screen.query_one("#config-cost", CostPanel)
        panel.show(first)
        # Waiting on this one only: the first is cancelled on purpose, and
        # `wait_for_complete` would raise on its behalf.
        await panel.show(last).wait()
        await pilot.pause()
        seen["text"] = str(screen.query_one("#cost-text", Static).render())

    drive(app, scenario)

    assert "the-one-asked-for" in seen["text"]
    assert "stale-answer" not in seen["text"]


def test_copying_the_log_goes_through_the_clipboard_the_terminal_cannot_refuse(
    workspace: Path,
) -> None:
    """OSC 52 is a request the terminal may decline in silence, and used to say `copied`.

    Under WSL there is a route that does not go through the terminal at all.
    Where there is none, the message names the file instead of pretending.
    """
    state = _state(workspace)
    log, events = state.paths_for("build")
    log.write_text("one\ntwo\nthree\n", encoding="utf-8")
    state.begin("build", log=log, events=events)
    copied: list[bytes] = []

    class FakeClip:
        @staticmethod
        def run(argv: list[str], **kwargs: Any) -> None:
            assert argv == ["/mnt/c/WINDOWS/system32/clip.exe"]
            copied.append(kwargs["input"])

    app = _app(workspace)
    seen: dict[str, Any] = {}

    async def scenario(pilot: Any) -> None:
        await pilot.press("o")
        await pilot.pause()
        with (
            patch("shade_pipeline.console.jobs.shutil.which", return_value=None),
            patch.object(app, "notify", lambda message, **kwargs: seen.update(without=message)),
        ):
            await pilot.press("y")
            await pilot.pause()
        with (
            patch(
                "shade_pipeline.console.jobs.shutil.which",
                return_value="/mnt/c/WINDOWS/system32/clip.exe",
            ),
            patch("shade_pipeline.console.jobs.subprocess", FakeClip),
            patch.object(app, "notify", lambda message, **kwargs: seen.update(with_clip=message)),
        ):
            await pilot.press("y")
            await pilot.pause()

    drive(app, scenario)

    assert copied == [b"one\ntwo\nthree\n"], "the log went out through clip.exe"
    assert "3 lines" in seen["with_clip"]
    assert str(log) in seen["without"], "with no helper, say where the file is"


def test_a_step_that_finishes_says_so_once(workspace: Path) -> None:
    """The list is the screen left open for hours, and it announced nothing.

    A build that failed at four in the morning was visible only to whoever
    happened to be looking at the table.
    """
    app = _app(workspace)
    said: list[str] = []

    async def scenario(pilot: Any) -> None:
        screen = app.screen
        assert isinstance(screen, CitiesScreen)
        state = _state(workspace)
        log, events = state.paths_for("build")
        state.begin("build", log=log, events=events)
        screen.refresh_rows()
        with patch.object(app, "notify", lambda message, **kwargs: said.append(message)):
            state.fail("build", "CoverageError: 3 lidar tiles missing")
            screen.refresh_rows()
            screen.refresh_rows()  # the same state again: nothing new to say
        await pilot.pause()

    drive(app, scenario)

    assert said == ["cube: build failed"]


def test_with_no_cities_the_screen_says_how_to_make_one(tmp_path: Path) -> None:
    """An empty table and "nothing running" is true and useless."""
    cities = tmp_path / "cities"
    cities.mkdir()
    app = ConsoleApp(
        cities_dir=cities,
        output_root=tmp_path / "out",
        data_root=tmp_path / "data",
        watch_dir=tmp_path / "downloads",
    )
    seen: dict[str, Any] = {}

    async def scenario(pilot: Any) -> None:
        seen["hint"] = str(app.screen.query_one("#hint", Static).render())

    drive(app, scenario)

    assert "n" in seen["hint"]
    assert "no cities" in seen["hint"].lower()
