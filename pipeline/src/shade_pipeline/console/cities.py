"""The opening screen: where every city stands, in one glance.

This is the question the console exists to answer. Before it, the only way to
know whether a city had been built, whether its tiles matched that build, or
whether anything was running at all, was to remember -- and the state of five
cities across four steps is not something worth carrying in a head.
"""

from pathlib import Path
from typing import ClassVar

from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header, Static

from shade_pipeline.console.city import CityScreen, error_cell, first_line, status_cell
from shade_pipeline.runstate import CHAIN, StepStatus

REFRESH_SECONDS = 2.0

SERVED_STYLE: dict[str, str] = {
    "live": "green",
    "behind": "bold yellow",
    "ahead": "yellow",
    "unpublished": "dim",
    "unbuilt": "dim",
    "unknown": "dim",
}
"""Keyed by ``Verdict``'s values as strings, not by the enum.

Importing :mod:`shade_pipeline.deployed` costs 835 ms because it reaches
``shade_core.artifacts`` and therefore rasterio, and this module is on the path
to the first table. The keys are held to the enum by a test rather than by an
import.
"""


class CitiesScreen(Screen[None]):
    """One row per city, one column per step."""

    BINDINGS: ClassVar[list[BindingType]] = [
        # Not `enter`: a row-cursor DataTable consumes that key and turns it
        # into RowSelected, so a screen binding on it would never fire.
        Binding("o", "open", "Open"),
        Binding("n", "new_city", "New city"),
        Binding("g", "refresh_now", "Refresh"),
        Binding("s", "check_server", "Server"),
    ]
    DEFAULT_CSS = """
    CitiesScreen DataTable { height: 1fr; }
    CitiesScreen #hint { height: auto; padding: 0 1; color: $text-muted; }
    """

    def __init__(self) -> None:
        super().__init__()
        self._seen: dict[tuple[str, str], StepStatus] = {}
        """Last status seen per city and step, to notice what changes.

        This screen is the one left open for hours while something builds, and
        it announced nothing: a step that failed at four in the morning was
        visible only to whoever happened to look at the table.
        """
        self._served: dict[str, str] = {}
        """Last verdict heard from the public API, per city.

        Empty until somebody presses `s`. The refresh loop runs every two
        seconds and must never make a request: a table that polls production
        four hundred times an hour is a denial of service against yourself.
        """

    def compose(self) -> ComposeResult:
        yield Header()
        yield DataTable(id="cities", cursor_type="row")
        yield Static(id="hint")
        yield Footer(show_command_palette=False)

    def on_mount(self) -> None:
        table = self.query_one("#cities", DataTable)
        table.add_column("city", key="city")
        for step in CHAIN:
            table.add_column(step, key=step)
        table.add_column("served", key="served")
        self.refresh_rows()
        self.set_interval(REFRESH_SECONDS, self.refresh_rows)

    def refresh_rows(self) -> None:
        from shade_pipeline.console.app import ConsoleApp

        app = self.app
        assert isinstance(app, ConsoleApp)
        table = self.query_one("#cities", DataTable)
        cursor = table.cursor_row
        table.clear()
        running: list[str] = []
        broken: list[str] = []
        for city in app.cities():
            try:
                state = app.state_of(city)
            except (OSError, ValueError) as error:
                # A city file being edited is an ordinary state, and one of
                # them halfway through a save used to stop the console from
                # opening at all. It gets a row saying so, and the other six
                # cities stay readable.
                broken.append(f"{city}: {first_line(error)}")
                table.add_row(
                    city, *(error_cell() for _ in CHAIN), self.served_cell(city), key=city
                )
                continue
            statuses = [state.status(step) for step in CHAIN]
            table.add_row(
                city,
                *(status_cell(status) for status in statuses),
                self.served_cell(city),
                key=city,
            )
            if StepStatus.RUNNING in statuses:
                running.append(city)
            self.announce(city, dict(zip(CHAIN, statuses, strict=True)))
        if 0 <= cursor < table.row_count:
            table.move_cursor(row=cursor)
        if not table.row_count:
            # The first screen of a fresh checkout, and it used to say "nothing
            # running" over an empty table: true, and no help at all.
            self.query_one("#hint", Static).update(
                f"no cities in {app.cities_dir}: press [b]n[/b] to register one"
            )
            return
        lines = [f"running: {', '.join(running)}" if running else "nothing running", *broken]
        self.query_one("#hint", Static).update("\n".join(lines))

    def announce(self, city: str, statuses: dict[str, StepStatus]) -> None:
        """Say out loud what a step just became, once, when it becomes it.

        Only the transition, and only into an outcome: a step that is still
        running has the bar for that, and repeating "done" every two seconds
        would train anybody to ignore the toasts. Nothing is announced on the
        first pass either -- what was already finished before the console
        opened is not news.
        """
        for step, status in statuses.items():
            before = self._seen.get((city, step))
            self._seen[city, step] = status
            if before is None or before == status:
                continue
            if status is StepStatus.FAILED:
                self.notify(f"{city}: {step} failed", severity="error", timeout=30)
            elif status is StepStatus.DONE and before is StepStatus.RUNNING:
                self.notify(f"{city}: {step} done")

    def served_cell(self, city: str) -> Text:
        verdict = self._served.get(city)
        if verdict is None:
            return Text("?", style="dim")
        return Text(verdict, style=SERVED_STYLE.get(verdict, ""))

    def action_refresh_now(self) -> None:
        self.refresh_rows()

    def action_check_server(self) -> None:
        """Ask the public API what it is serving, once, because somebody asked."""
        from shade_pipeline.console.app import ConsoleApp

        app = self.app
        assert isinstance(app, ConsoleApp)
        self.notify(f"asking {app.base_url}")
        self.check_server(app.cities(), app.output_root, app.base_url)

    @work(thread=True, group="server", exclusive=True)
    def check_server(self, cities: list[str], output_root: Path, base_url: str) -> None:
        """One request per city, off the event loop.

        In a thread and not in the refresh: reading a state file is microseconds
        and a round trip to a VPS is not, and this screen repaints itself twice
        a second. `exclusive` so holding `s` down cannot stack up surveys.
        """
        from shade_pipeline.deployed import survey

        for comparison in survey(list(cities), output_root=output_root, base_url=base_url):
            self._served[comparison.city] = comparison.verdict.value
        self.app.call_from_thread(self.refresh_rows)

    def selected(self) -> str | None:
        table = self.query_one("#cities", DataTable)
        if table.row_count == 0:
            return None
        return str(table.get_row_at(table.cursor_row)[0])

    def action_open(self) -> None:
        city = self.selected()
        if city is not None:
            self.app.push_screen(CityScreen(city))

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.data_table.id == "cities":
            self.action_open()

    def action_new_city(self) -> None:
        # Imported on the keypress, not at the top: registering a city needs
        # pyproj and shapely to work out the CRS, and opening the console does
        # not.
        from shade_pipeline.console.app import ConsoleApp
        from shade_pipeline.console.newcity import NewCityScreen

        app = self.app
        assert isinstance(app, ConsoleApp)
        self.app.push_screen(
            NewCityScreen(app.cities_dir, app.data_root, app.watch_dir), self.city_registered
        )

    def city_registered(self, city: str | None) -> None:
        self.refresh_rows()
        if city is not None:
            self.app.push_screen(CityScreen(city))
