"""Choosing how to build a city, instead of typing eight flags again.

The defaults are the useful part. The first time a city is launched they are
computed rather than guessed: workers is the smaller of the cores available and
the number that actually *fits in memory*, so the dialog never proposes a figure
the phase will refuse. After that they are whatever was chosen last time for
that city, kept in its state file -- because the second build of a city is
almost always the first one again with one thing changed.
"""

from pathlib import Path
from typing import Any, ClassVar

from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Grid, Horizontal, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Footer, Input, Label, Static, Switch

from shade_core.config import CityConfig
from shade_pipeline.budget import cpu_budget, estimate_tiles_worker_bytes, workers_that_fit
from shade_pipeline.grid import grid_shape
from shade_pipeline.runner import CHAIN, UNATTENDED
from shade_pipeline.tiles import DEFAULT_MAX_ZOOM, DEFAULT_MIN_ZOOM

NUMBERS = ("workers", "tile_size", "min_zoom", "max_zoom")
FLAGS = ("resume", "force")


def suggested_workers(config: CityConfig) -> int:
    """Cores worth using, capped by how many render workers this machine holds.

    The tile phase is bound by memory and not by cores -- one worker holds
    whole-raster arrays for an instant -- so proposing ``cpu_budget()`` would
    routinely propose a number ``build_tiles`` then rejects. One core is left
    free so the machine stays usable during the hours this runs.
    """
    rows, cols = grid_shape(config.bbox, config.resolution_m)
    fits = workers_that_fit(estimate_tiles_worker_bytes(rows, cols))
    cores = max(1, cpu_budget() - 1)
    return max(1, min(cores, fits)) if fits is not None else cores


def defaults_for(config: CityConfig, remembered: dict[str, Any]) -> dict[str, Any]:
    """Last time's choices for this city, or a sensible first proposal."""
    base: dict[str, Any] = {
        "workers": suggested_workers(config),
        "tile_size": 512,
        "min_zoom": DEFAULT_MIN_ZOOM,
        "max_zoom": DEFAULT_MAX_ZOOM,
        "from_step": "",
        "to_step": "",
        "cache_dir": "",
        "resume": True,
        "force": False,
    }
    base.update({key: value for key, value in remembered.items() if key in base})
    return base


class LaunchScreen(ModalScreen[dict[str, Any] | None]):
    """The options a chain run takes, on one screen."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "dismiss_none", "Cancel"),
        Binding("ctrl+r", "launch", "Launch"),
    ]
    DEFAULT_CSS = """
    LaunchScreen { align: center middle; }
    LaunchScreen > VerticalScroll {
        width: 72; height: auto; max-height: 90%;
        border: thick $accent; background: $surface; padding: 1 2;
    }
    LaunchScreen Grid { grid-size: 2; grid-columns: 22 1fr; grid-rows: 3; height: auto; }
    LaunchScreen Label { padding: 1 0 0 0; }
    LaunchScreen #hint { color: $text-muted; padding: 1 0; }
    LaunchScreen Horizontal { height: auto; align: right middle; padding-top: 1; }
    """

    def __init__(self, city: str, config: CityConfig, remembered: dict[str, Any]) -> None:
        super().__init__()
        self.city = city
        self.values = defaults_for(config, remembered)
        self.remembered = bool(remembered)

    def compose(self) -> ComposeResult:
        with VerticalScroll():
            yield Static(f"Build {self.city}", id="title")
            yield Static(
                "remembered from last time"
                if self.remembered
                else "first run: workers is what fits in memory, not just what has cores",
                id="hint",
            )
            with Grid():
                for key in NUMBERS:
                    yield Label(key.replace("_", " "))
                    yield Input(value=str(self.values[key]), id=key, type="integer")
                yield Label("from step")
                yield Input(
                    value=str(self.values["from_step"]),
                    id="from_step",
                    placeholder=f"blank = {UNATTENDED[0]}",
                )
                yield Label("to step")
                yield Input(
                    value=str(self.values["to_step"]),
                    id="to_step",
                    placeholder=f"blank = {UNATTENDED[-1]} (publish is never automatic)",
                )
                yield Label("lidar cache")
                yield Input(
                    value=str(self.values["cache_dir"]),
                    id="cache_dir",
                    placeholder="blank = data/lidar/<city>",
                )
                for flag in FLAGS:
                    yield Label(flag)
                    yield Switch(value=bool(self.values[flag]), id=flag)
            with Horizontal():
                yield Button("Cancel", id="cancel")
                yield Button("Launch", id="launch", variant="primary")
        # Without it, ctrl+r -- the key that actually starts a six-hour build
        # from this dialog -- is announced nowhere at all.
        yield Footer(show_command_palette=False)

    def action_dismiss_none(self) -> None:
        self.dismiss(None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "launch":
            self.action_launch()
        else:
            self.dismiss(None)

    def action_launch(self) -> None:
        chosen = self._collect()
        if chosen is not None:
            self.dismiss(chosen)

    def _collect(self) -> dict[str, Any] | None:
        chosen: dict[str, Any] = {}
        for key in NUMBERS:
            raw = self.query_one(f"#{key}", Input).value.strip()
            if not raw.isdigit() or int(raw) < 1:
                self.notify(f"{key} has to be a positive whole number", severity="error")
                return None
            chosen[key] = int(raw)
        if chosen["min_zoom"] > chosen["max_zoom"]:
            self.notify("min zoom is deeper than max zoom", severity="error")
            return None
        for key in ("from_step", "to_step"):
            value = self.query_one(f"#{key}", Input).value.strip()
            if value and value not in CHAIN:
                self.notify(
                    f"{value!r} is not a step; the chain is {', '.join(CHAIN)}", severity="error"
                )
                return None
            chosen[key] = value
        chosen["cache_dir"] = self.query_one("#cache_dir", Input).value.strip()
        for flag in FLAGS:
            chosen[flag] = self.query_one(f"#{flag}", Switch).value
        return chosen


def to_argv(
    city: str, options: dict[str, Any], *, cities_dir: Path, output_root: Path, data_root: Path
) -> list[str]:
    """The ``shade-engine run`` command line the chosen options describe."""
    argv = [
        "run",
        city,
        "--workers",
        str(options["workers"]),
        "--tile-size",
        str(options["tile_size"]),
        "--min-zoom",
        str(options["min_zoom"]),
        "--max-zoom",
        str(options["max_zoom"]),
        "--cities-dir",
        str(cities_dir),
        "--output-root",
        str(output_root),
        "--data-root",
        str(data_root),
    ]
    if options.get("from_step"):
        argv += ["--from", str(options["from_step"])]
    if options.get("to_step"):
        argv += ["--to", str(options["to_step"])]
    if options.get("cache_dir"):
        argv += ["--cache-dir", str(options["cache_dir"])]
    argv.append("--resume" if options.get("resume", True) else "--no-resume")
    if options.get("force"):
        argv.append("--force")
    return argv
