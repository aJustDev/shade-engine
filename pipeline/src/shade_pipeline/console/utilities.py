"""The commands that are not part of the chain, with what each one is for.

``verify``, ``canopy``, ``recolor``, ``predict`` and ``import-layer`` all exist
because something went wrong once: a horizon cube shipped silently corrupt, a
build predated the canopy mask, a theme needed rebuilding without recomputing
shade. They are rare, which is exactly why nobody remembers they are there --
so they get a screen that says what each does and runs it, instead of a line in
a runbook.

Everything runs detached, and -- unlike a chain step -- none of these records a
run or keeps a log: ``launch`` sends their output to ``/dev/null``. They are
minutes, not hours, and the way to see what they said is to run them from a
terminal. The Log tab shows the chain.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, VerticalScroll
from textual.screen import Screen
from textual.widgets import Button, Footer, Header, Input, Label, ListItem, ListView, Static


@dataclass(frozen=True)
class Utility:
    """One off-chain command: what it does, and what it needs beyond the city."""

    key: str
    summary: str
    argument: str | None = None
    placeholder: str = ""


UTILITIES: tuple[Utility, ...] = (
    Utility(
        "verify",
        "Audit the artifacts: layout, value ranges and the horizon-blocker invariant. "
        "This is the check that would have caught the corrupt horizon cube Cordoba's "
        "first build shipped, with its western sectors silently zeroed.",
    ),
    Utility(
        "canopy",
        "Rebuild canopy.tif from the existing rasters, without re-sweeping the horizon. "
        "For artifact directories built before the crown mask existed.",
    ),
    Utility(
        "recolor",
        "Write the tile tree in another theme. The tiles are paletted PNGs, so this "
        "rewrites 20 bytes per tile instead of recomputing any shade: minutes, not hours.",
        argument="--palette",
        placeholder="light",
    ),
    Utility(
        "graph",
        "Build the pedestrian graph with each edge's sun fraction. Optional: without it "
        "the API simply answers 503 for routes.",
    ),
    Utility(
        "predict",
        "Print the predicted shade timeline of some field points for a day. The tool for "
        "checking the model against somebody standing in the street.",
        argument="points csv",
        placeholder="cities/cordoba-field-points.csv",
    ),
    Utility(
        "import-layer",
        "Load a vector layer declared under `layers:` into PostGIS. Re-running replaces "
        "the city's rows, so it is idempotent.",
        argument="layer",
        placeholder="parking",
    ),
    Utility(
        "assets",
        "Download the glyphs and sprites every basemap style needs. Once per MACHINE, not "
        "per city -- it ignores the city you came from. Without the glyphs the map draws no "
        "labels at all. `run` does it as part of the basemap step; this is for a fresh "
        "working copy.",
    ),
)


class UtilitiesScreen(Screen[tuple[str, str] | None]):
    """Pick an off-chain command for a city; returns (utility key, argument)."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "cancel", "Back"),
        Binding("enter", "choose", "Run"),
    ]
    DEFAULT_CSS = """
    UtilitiesScreen ListView { height: 1fr; }
    UtilitiesScreen #summary { height: auto; min-height: 5; padding: 1; background: $panel; }
    UtilitiesScreen #argument-row { height: auto; padding: 0 1; }
    UtilitiesScreen Horizontal { height: auto; align: right middle; }
    """

    def __init__(self, city: str) -> None:
        super().__init__()
        self.city = city

    def compose(self) -> ComposeResult:
        yield Header()
        with VerticalScroll():
            yield ListView(
                *(ListItem(Label(utility.key), id=f"u-{utility.key}") for utility in UTILITIES),
                id="utilities",
            )
            yield Static(id="summary")
            with Horizontal(id="argument-row"):
                yield Input(id="argument", placeholder="(no argument needed)", disabled=True)
            with Horizontal():
                yield Button("Back", id="cancel")
                yield Button("Run", id="run", variant="primary")
        yield Footer()

    def on_mount(self) -> None:
        self.sub_title = f"{self.city}: utilities"
        self._describe(UTILITIES[0])

    def _selected(self) -> Utility:
        index = self.query_one("#utilities", ListView).index or 0
        return UTILITIES[min(index, len(UTILITIES) - 1)]

    def _describe(self, utility: Utility) -> None:
        self.query_one("#summary", Static).update(f"[b]{utility.key}[/b]\n{utility.summary}")
        argument = self.query_one("#argument", Input)
        argument.disabled = utility.argument is None
        argument.placeholder = utility.placeholder or "(no argument needed)"
        if utility.argument is not None and not argument.value:
            argument.value = utility.placeholder

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        self._describe(self._selected())

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        self.action_choose()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "run":
            self.action_choose()
        else:
            self.action_cancel()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_choose(self) -> None:
        utility = self._selected()
        argument = self.query_one("#argument", Input).value.strip()
        if utility.argument is not None and not argument:
            self.notify(f"{utility.key} needs {utility.argument}", severity="error")
            return
        self.dismiss((utility.key, argument))


def to_argv(
    utility: str,
    city: str,
    argument: str,
    *,
    cities_dir: Path,
    output_root: Path,
) -> list[str]:
    """The command line for one utility, as the CLI would take it."""
    if utility == "assets":
        # Takes no city and no --cities-dir: it is the one utility about the
        # machine rather than about a city.
        return ["assets", "--output-root", str(output_root)]
    argv = [utility, city]
    if utility == "predict":
        argv += [argument, "--day", "2026-08-16"]
    elif utility == "recolor":
        argv += ["--palette", argument or "light"]
    elif utility == "import-layer":
        argv.insert(2, argument)
    argv += ["--cities-dir", str(cities_dir), "--output-root", str(output_root)]
    if utility == "recolor":
        # recolor takes no --cities-dir: it reads the tile tree, not the config.
        argv = [part for part in argv if part not in {"--cities-dir", str(cities_dir)}]
    return argv
