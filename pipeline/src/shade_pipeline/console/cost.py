"""What a city costs, recomputed while you change the settings that decide it.

This is the didactic half of the console. Reading in an ADR that doubling the
azimuth sectors doubles the sweep is one thing; watching the estimate move as
you type 128 is another, and it is the difference between a number you were
told and one you chose.

Nothing is computed here. ``plan_city`` already produces every figure -- pixels,
sweep tiles skipped at each tile size, minutes, memory per worker and how many
fit, disk, which PNOA tiles are still missing -- and ``format_plan`` already
renders them for the ``area`` command. Reusing both is the point: the console,
the CLI report and the chain's preflight cannot drift into three different
answers if there is only one.
"""

from pathlib import Path

from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import Static

from shade_core.config import CityConfig
from shade_pipeline.area import AreaError, format_plan, plan_city


class CostPanel(VerticalScroll):
    """The price of a city as configured right now."""

    DEFAULT_CSS = """
    CostPanel { height: 1fr; padding: 0 1; background: $panel; }
    CostPanel > Static { width: auto; }
    """

    def compose(self) -> ComposeResult:
        yield Static(id="cost-text")

    def show(
        self,
        config: CityConfig,
        *,
        tile_size: int = 512,
        workers: int = 1,
        cache_dir: Path | None = None,
        config_path: Path | None = None,
    ) -> None:
        """Price ``config`` and display the report, or say why it cannot be priced."""
        try:
            plan = plan_city(
                config,
                tile_size=tile_size,
                workers=workers,
                cache_dir=cache_dir,
                config_path=config_path,
            )
        except (AreaError, OSError, ValueError) as error:
            # A city whose polygon is missing or unreadable is a normal state
            # while it is being set up, and saying so beats an empty panel.
            self.query_one("#cost-text", Static).update(f"cannot price this city yet: {error}")
            return
        self.query_one("#cost-text", Static).update(format_plan(plan, config))

    def clear(self, message: str = "") -> None:
        self.query_one("#cost-text", Static).update(message)
