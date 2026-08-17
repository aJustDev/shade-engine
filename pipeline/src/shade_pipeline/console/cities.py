"""The opening screen: where every city stands, in one glance.

This is the question the console exists to answer. Before it, the only way to
know whether a city had been built, whether its tiles matched that build, or
whether anything was running at all, was to remember -- and the state of five
cities across four steps is not something worth carrying in a head.
"""

from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header, Static

from shade_pipeline.console.city import CityScreen, error_cell, first_line, status_cell
from shade_pipeline.console.newcity import NewCityScreen
from shade_pipeline.runner import CHAIN
from shade_pipeline.runstate import StepStatus

REFRESH_SECONDS = 2.0


class CitiesScreen(Screen[None]):
    """One row per city, one column per step."""

    BINDINGS: ClassVar[list[BindingType]] = [
        # Not `enter`: a row-cursor DataTable consumes that key and turns it
        # into RowSelected, so a screen binding on it would never fire.
        Binding("o", "open", "Open"),
        Binding("n", "new_city", "New city"),
        Binding("g", "refresh_now", "Refresh"),
    ]
    DEFAULT_CSS = """
    CitiesScreen DataTable { height: 1fr; }
    CitiesScreen #hint { height: auto; padding: 0 1; color: $text-muted; }
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
        self.refresh_rows()
        self.set_interval(REFRESH_SECONDS, self.refresh_rows)

    @property
    def console_app(self) -> object:
        from shade_pipeline.console.app import ConsoleApp

        assert isinstance(self.app, ConsoleApp)
        return self.app

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
                table.add_row(city, *(error_cell() for _ in CHAIN), key=city)
                continue
            statuses = [state.status(step) for step in CHAIN]
            table.add_row(city, *(status_cell(status) for status in statuses), key=city)
            if StepStatus.RUNNING in statuses:
                running.append(city)
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

    def action_refresh_now(self) -> None:
        self.refresh_rows()

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
        from shade_pipeline.console.app import ConsoleApp

        app = self.app
        assert isinstance(app, ConsoleApp)
        self.app.push_screen(
            NewCityScreen(app.cities_dir, app.data_root, app.watch_dir), self.city_registered
        )

    def city_registered(self, city: str | None) -> None:
        self.refresh_rows()
        if city is not None:
            self.app.push_screen(CityScreen(city))
