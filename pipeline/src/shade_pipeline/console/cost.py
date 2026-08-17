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

The pricing itself runs in a thread. It is 191 ms for Cordoba, measured, and
every one of those milliseconds used to be the interface not answering -- while
typing into the very field whose effect the panel exists to show.
"""

from pathlib import Path

from textual import work
from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import Static
from textual.worker import Worker, get_current_worker

from shade_core.config import CityConfig


class CostPanel(VerticalScroll):
    """The price of a city as configured right now."""

    DEFAULT_CSS = """
    CostPanel { height: 1fr; padding: 0 1; background: $panel; }
    CostPanel > Static { width: auto; }
    CostPanel.pricing #cost-text { color: $text-muted; }
    """

    def __init__(self, *, id: str | None = None) -> None:
        super().__init__(id=id)
        self._priced = False
        """Whether a real report has been shown yet, so the first wait says so."""

    def compose(self) -> ComposeResult:
        # markup=False, and declared once here rather than escaped at every
        # call site: everything this panel ever shows comes from outside --
        # the report, or the reason there is none -- and a pydantic message
        # carrying `[type=float_parsing, input_value='x']` is markup with an
        # unclosed tag, which used to raise MarkupError while painting and
        # take the app down.
        yield Static(id="cost-text", markup=False)

    def show(
        self,
        config: CityConfig,
        *,
        tile_size: int = 512,
        workers: int = 1,
        cache_dir: Path | None = None,
        config_path: Path | None = None,
    ) -> Worker[None]:
        """Price ``config`` in a thread and display the report when it lands.

        The previous figures stay on screen, dimmed, while the new ones are
        computed: what makes this panel worth having is comparing the price
        before a change with the price after it, and a spinner in place of the
        old number takes away half of that.

        Returns the worker doing the arithmetic, which is what lets a caller --
        a test, mostly -- wait for this particular price rather than for every
        worker in the app.
        """
        if not self._priced:
            self.query_one("#cost-text", Static).update("pricing this city...")
        self.add_class("pricing")
        return self.price(
            config,
            tile_size=tile_size,
            workers=workers,
            cache_dir=cache_dir,
            config_path=config_path,
        )

    @work(thread=True, exclusive=True, group="cost")
    def price(
        self,
        config: CityConfig,
        *,
        tile_size: int,
        workers: int,
        cache_dir: Path | None,
        config_path: Path | None,
    ) -> None:
        """Do the arithmetic off the event loop, and drop it if it is stale.

        ``exclusive`` cancels the previous run of this group, which is what
        stops eight prices being computed while somebody types ``128`` one
        digit at a time. A thread cannot be interrupted, so the cancelled one
        runs to the end and then throws its answer away rather than painting a
        price for a configuration nobody is looking at any more.
        """
        # Imported here, not at the top of the module: it brings pyproj,
        # shapely and rasterio with it, ~950 ms that the console would
        # otherwise pay at startup, before painting its first table.
        from shade_pipeline.area import AreaError, format_plan, plan_city

        try:
            plan = plan_city(
                config,
                tile_size=tile_size,
                workers=workers,
                cache_dir=cache_dir,
                config_path=config_path,
            )
            text = format_plan(plan, config)
        except (AreaError, OSError, ValueError) as error:
            # A city whose polygon is missing or unreadable is a normal state
            # while it is being set up, and saying so beats an empty panel.
            text = f"cannot price this city yet: {error}"
        if get_current_worker().is_cancelled:
            return
        # From a thread, the widget is only ever touched through the app's own
        # loop; calling update() here would be writing to the UI from outside.
        self.app.call_from_thread(self.settle, text)

    def settle(self, text: str) -> None:
        """Adopt a finished price. Called on the event loop, never from the thread."""
        self._priced = True
        self.remove_class("pricing")
        self.query_one("#cost-text", Static).update(text)

    def clear(self, message: str = "") -> None:
        """Say why there is no price, and abandon any that was on its way.

        Without the cancel, deleting the polygon while its price was being
        computed put the figures back a moment later, under a message saying
        there was nothing to price.
        """
        self.workers.cancel_group(self, "cost")
        self._priced = False
        self.remove_class("pricing")
        self.query_one("#cost-text", Static).update(message)
