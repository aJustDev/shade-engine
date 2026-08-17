"""One city: where it stands, what it costs, and what it is doing right now.

Three tabs, answering the three questions that used to need three commands and
a memory. Steps is progress -- a bar fed by the event stream, not by parsing
prose, so "running" becomes "unit 19 of 85, eta 5h 23m". Config is every setting
with what it does, what it costs and where it was decided, and it is editable:
change the azimuth sectors and the price of the whole build moves under your
hands before you save. Log is the real output of whatever is running.

Everything launched from here is detached, so this screen can be closed at any
moment without touching the work.
"""

from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical
from textual.screen import Screen
from textual.widgets import (
    DataTable,
    Footer,
    Header,
    ProgressBar,
    RichLog,
    Static,
    TabbedContent,
    TabPane,
)

from shade_core.config import CityConfig
from shade_pipeline.cityfile import PROTECTED, CityFileError, edit_city
from shade_pipeline.console.confirm import ConfirmScreen, EditScreen
from shade_pipeline.console.cost import CostPanel
from shade_pipeline.console.jobs import engine_argv, latest_phase, launch, progress_of
from shade_pipeline.console.launch import LaunchScreen, to_argv
from shade_pipeline.console.utilities import UtilitiesScreen
from shade_pipeline.console.utilities import to_argv as utility_argv
from shade_pipeline.runner import CHAIN
from shade_pipeline.runstate import RunState, StepStatus

if TYPE_CHECKING:
    from shade_pipeline.console.app import ConsoleApp

REFRESH_SECONDS = 2.0

STATUS_STYLE: dict[StepStatus, str] = {
    StepStatus.PENDING: "dim",
    StepStatus.RUNNING: "bold cyan",
    StepStatus.DONE: "green",
    StepStatus.FAILED: "bold red",
    StepStatus.STALE: "yellow",
}

EDITABLE: tuple[str, ...] = (
    "name",
    "country",
    "timezone",
    "crs",
    "resolution_m",
    "horizon_sectors",
    "horizon_max_distance_m",
    "observer_height_m",
)
"""Top-level scalars the console offers to change.

``bbox`` and ``area`` are missing on purpose: they belong to ``shade-engine
area``, which is the only thing that knows how to snap a box to whole pixels and
keep the polygon consistent with it. ``id`` names everything already built.
"""

CASTS: dict[str, type] = {
    "resolution_m": float,
    "horizon_sectors": int,
    "horizon_max_distance_m": float,
    "observer_height_m": float,
}


def status_cell(status: StepStatus) -> Text:
    return Text(status.value, style=STATUS_STYLE.get(status, ""))


class CityScreen(Screen[None]):
    """The steps, the settings and the live output of one city."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "app.pop_screen", "Back"),
        Binding("r", "run", "Run"),
        Binding("v", "preview", "Preview"),
        Binding("p", "publish", "Publish"),
        Binding("u", "utilities", "Utilities"),
        Binding("y", "copy_log", "Copy log"),
    ]
    DEFAULT_CSS = """
    CityScreen #progress-row { height: auto; padding: 0 1; }
    CityScreen #progress-label { height: 1; color: $text-muted; }
    CityScreen #explain { height: auto; min-height: 5; padding: 1; background: $panel; }
    CityScreen #config-cost { height: 1fr; }
    """

    def __init__(self, city: str) -> None:
        super().__init__()
        self.city = city
        self._log_path: Path | None = None
        self._offset = 0

    # ---------------------------------------------------------------- layout

    def compose(self) -> ComposeResult:
        yield Header()
        with TabbedContent():
            with TabPane("Steps", id="steps-pane"):
                yield Vertical(
                    DataTable(id="steps", cursor_type="row"),
                    Vertical(
                        Static(id="progress-label"),
                        ProgressBar(id="progress", show_eta=False),
                        id="progress-row",
                    ),
                )
            with TabPane("Config", id="config-pane"):
                yield Vertical(
                    DataTable(id="config", cursor_type="row"),
                    Static(id="explain"),
                    CostPanel(id="config-cost"),
                )
            with TabPane("Log", id="log-pane"):
                yield RichLog(id="log", wrap=True, markup=False)
        yield Footer()

    def on_mount(self) -> None:
        self.sub_title = self.city
        self.query_one("#steps", DataTable).add_columns("step", "status", "when", "took", "detail")
        self.query_one("#config", DataTable).add_columns("setting", "value", "")
        self.fill_config()
        self.refresh_steps()
        self.set_interval(REFRESH_SECONDS, self.refresh_steps)
        self.set_interval(1.0, self.tail_log)

    @property
    def console_app(self) -> ConsoleApp:
        from shade_pipeline.console.app import ConsoleApp

        assert isinstance(self.app, ConsoleApp)
        return self.app

    def state(self) -> RunState:
        return self.console_app.state_of(self.city)

    def config(self) -> CityConfig:
        return self.console_app.config_of(self.city)

    # ----------------------------------------------------------------- steps

    def refresh_steps(self) -> None:
        state = self.state()
        table = self.query_one("#steps", DataTable)
        cursor = table.cursor_row
        table.clear()
        for step in CHAIN:
            record = state.record(step)
            status = state.status(step)
            detail = record.error or (state.stale_reason(step) or "")
            table.add_row(
                step,
                status_cell(status),
                "" if record.finished_at is None else record.finished_at.strftime("%d %b %H:%M"),
                "" if record.duration_s is None else f"{record.duration_s:.0f}s",
                Text(detail[:70]),
            )
        if 0 <= cursor < table.row_count:
            table.move_cursor(row=cursor)
        self.refresh_progress(state)
        self.follow_newest_log(state)

    def refresh_progress(self, state: RunState) -> None:
        """Turn "running" into a position and an estimate, from the event stream."""
        label = self.query_one("#progress-label", Static)
        bar = self.query_one("#progress", ProgressBar)
        running = [step for step in CHAIN if state.status(step) is StepStatus.RUNNING]
        if not running:
            label.update("nothing running")
            bar.update(total=100, progress=0)
            return
        step = running[0]
        progress = progress_of(state, step)
        if progress is None:
            phase = latest_phase(state, step)
            label.update(f"{step}: {phase or 'starting'}")
            bar.update(total=100, progress=0)
            return
        phase = latest_phase(state, step)
        prefix = f"{step} ({phase})" if phase else step
        label.update(f"{prefix}: {progress.describe()}")
        bar.update(total=progress.total, progress=progress.done)

    # ---------------------------------------------------------------- config

    def fill_config(self) -> None:
        table = self.query_one("#config", DataTable)
        table.clear()
        try:
            config = self.config()
        except (OSError, ValueError) as error:
            table.add_row("error", Text(str(error)), "")
            return
        for name in CityConfig.model_fields:
            value = getattr(config, name, None)
            table.add_row(
                name,
                Text("-" if value is None else str(value)),
                "edit" if name in EDITABLE else Text("locked", style="dim"),
                key=name,
            )
        self.query_one("#config-cost", CostPanel).show(
            config, config_path=self.console_app.cities_dir / f"{self.city}.yaml"
        )

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.data_table.id != "config" or event.row_key.value is None:
            return
        self.explain(str(event.row_key.value))

    def explain(self, name: str) -> None:
        if name not in CityConfig.model_fields:
            return
        explanation = CityConfig.explain(name)
        lines = [f"[b]{name}[/b]  {explanation.get('description', '')}"]
        if "default" in explanation:
            lines.append(f"default: {explanation['default']}")
        if "cost" in explanation:
            lines.append(f"cost: {explanation['cost']}")
        if "doc" in explanation:
            lines.append(f"decided in: shade-docs {explanation['doc']}")
        if name in PROTECTED:
            lines.append("[dim]not editable here: it belongs to `shade-engine area`[/dim]")
        self.query_one("#explain", Static).update("\n".join(lines))

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.data_table.id != "config" or event.row_key.value is None:
            return
        name = str(event.row_key.value)
        if name not in EDITABLE:
            self.notify(f"{name} is not editable here", severity="warning")
            return
        current = getattr(self.config(), name, "")
        explanation = CityConfig.explain(name)
        self.app.push_screen(
            EditScreen(
                name, str(current), explanation.get("cost", explanation.get("description", ""))
            ),
            lambda value: self.save_setting(name, value),
        )

    def save_setting(self, name: str, value: str | None) -> None:
        if value is None:
            return
        cast = CASTS.get(name, str)
        try:
            typed: Any = cast(value)
        except ValueError:
            self.notify(f"{value!r} is not a {cast.__name__}", severity="error")
            return
        path = self.console_app.cities_dir / f"{self.city}.yaml"
        try:
            edit_city(path, name, typed)
        except (CityFileError, OSError) as error:
            self.notify(str(error), severity="error")
            return
        # The digest moves with the file, so anything built from the old value
        # turns stale by itself -- no one has to remember that it did.
        self.notify(f"{name} = {typed}")
        self.fill_config()
        self.refresh_steps()

    # ------------------------------------------------------------------- log

    def follow_newest_log(self, state: RunState) -> None:
        candidates = [
            (record.started_at, Path(record.log))
            for step in CHAIN
            if (record := state.record(step)).log and record.started_at
        ]
        if not candidates:
            return
        newest = max(candidates)[1]
        if newest != self._log_path:
            self._log_path = newest
            self._offset = 0
            self.query_one("#log", RichLog).clear()

    def tail_log(self) -> None:
        if self._log_path is None or not self._log_path.exists():
            return
        with self._log_path.open(encoding="utf-8", errors="replace") as handle:
            handle.seek(self._offset)
            fresh = handle.read()
            self._offset = handle.tell()
        if not fresh:
            return
        pane = self.query_one("#log", RichLog)
        for line in fresh.splitlines():
            pane.write(line)

    # --------------------------------------------------------------- actions

    def action_run(self) -> None:
        app = self.console_app
        if app.is_busy(self.city):
            self.notify(f"{self.city} already has a step running", severity="warning")
            return
        self.app.push_screen(
            LaunchScreen(self.city, self.config(), self.state().preferences), self.start_chain
        )

    def start_chain(self, options: dict[str, Any] | None) -> None:
        if options is None:
            return
        app = self.console_app
        self.state().remember(options)
        pid = launch(
            engine_argv(
                *to_argv(
                    self.city,
                    options,
                    cities_dir=app.cities_dir,
                    output_root=app.output_root,
                    data_root=app.data_root,
                )
            )
        )
        self.notify(f"{self.city}: building as pid {pid}")
        self.refresh_steps()

    def action_preview(self) -> None:
        app = self.console_app
        pid = launch(
            engine_argv(
                "preview",
                self.city,
                "--cities-dir",
                str(app.cities_dir),
                "--output-root",
                str(app.output_root),
            )
        )
        self.notify(f"preview on http://127.0.0.1:5173 (pid {pid}); it keeps running until killed")

    def action_publish(self) -> None:
        """Show the plan in full, then ask. Publishing is never automatic."""
        app = self.console_app
        plan = app.publish_plan(self.city)
        if isinstance(plan, str):
            self.notify(plan, severity="error")
            return
        self.app.push_screen(
            ConfirmScreen(f"Publish {self.city}", plan.render(), "Publish"),
            self.do_publish,
        )

    def do_publish(self, confirmed: bool | None) -> None:
        if not confirmed:
            return
        app = self.console_app
        pid = launch(
            engine_argv(
                "publish",
                self.city,
                "--cities-dir",
                str(app.cities_dir),
                "--output-root",
                str(app.output_root),
                "--data-root",
                str(app.data_root),
            )
        )
        self.notify(f"{self.city}: publishing as pid {pid}")

    def action_copy_log(self) -> None:
        """Put the whole current log on the clipboard.

        Dragging with the mouse selects too, and Textual copies that -- but over
        ssh, in a multiplexer, or in a terminal that grabs the mouse itself,
        which of those works varies. A key that copies the file outright does
        not, and a traceback is exactly the thing you want to paste somewhere
        else.
        """
        if self._log_path is None or not self._log_path.exists():
            self.notify("no log to copy yet", severity="warning")
            return
        text = self._log_path.read_text(encoding="utf-8", errors="replace")
        self.app.copy_to_clipboard(text)
        self.notify(f"copied {len(text.splitlines())} lines from {self._log_path.name}")

    def action_utilities(self) -> None:
        self.app.push_screen(UtilitiesScreen(self.city), self.run_utility)

    def run_utility(self, chosen: tuple[str, str] | None) -> None:
        if chosen is None:
            return
        utility, argument = chosen
        app = self.console_app
        pid = launch(
            engine_argv(
                *utility_argv(
                    utility,
                    self.city,
                    argument,
                    cities_dir=app.cities_dir,
                    output_root=app.output_root,
                )
            )
        )
        self.notify(f"{utility} running as pid {pid}")
