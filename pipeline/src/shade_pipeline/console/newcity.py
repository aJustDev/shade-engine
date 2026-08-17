"""Registering a city: the parts a terminal can do, and a bridge for the one it cannot.

Most of a city file follows from the polygon. The projected CRS in particular
has always been the trap -- ``bbox`` is in meters and the wrong UTM zone
silently distorts every distance in the build -- and it needs no human at all:
``pyproj`` knows which zone a geometry falls in, and for Cordoba it returns
exactly the EPSG:25830 the hand-written file already uses.

What a terminal cannot do is draw a polygon. Rather than pretend otherwise, this
screen watches a directory for the file you export and, if you give it a point,
opens geojson.io in the right place first. Drawing stays where it always was
(``ops/anadir-ciudad.md``: "el dibujo no lo hace el motor"); what changes is that
you no longer carry the file across by hand and then run a command to find out
what it costs.

**The polygon is the only thing that matters.** It carries the location, the
extent and therefore the CRS. The latitude and longitude fields are optional and
are never read once a polygon exists -- they exist to centre a map, not to
describe a city.
"""

import json
from pathlib import Path
from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Grid, Horizontal, VerticalScroll
from textual.markup import escape
from textual.screen import ModalScreen, Screen
from textual.timer import Timer
from textual.widgets import Button, Footer, Header, Input, Label, Static, TextArea

from shade_core.config import CityConfig
from shade_pipeline.area import (
    WGS84,
    AreaError,
    area_geojson,
    check_area_of_use,
    read_area,
    snap_bbox,
    utm_crs,
)
from shade_pipeline.cityfile import CityFileError, new_city_yaml, write_new_city
from shade_pipeline.console.confirm import ConfirmScreen
from shade_pipeline.console.cost import CostPanel

DEBOUNCE_SECONDS = 0.25
"""How long a typed path settles before it is read."""

DRAFTS_DIRNAME = "drafts"
"""Where a pasted polygon is parked, under the data root that git ignores."""


def watch_candidates() -> tuple[Path, ...]:
    """Directories a browser might drop an export into, most likely first."""
    home = Path.home()
    return (home / "Descargas", home / "Downloads")


def default_watch_dir() -> Path | None:
    """The first candidate that is actually there, or nothing to watch.

    It used to be ``~/Descargas`` unconditionally, and on a machine whose home
    has no such directory the screen spent the whole session telling people to
    drop a file into a path that did not exist. Nothing to watch is an answer:
    the polygon can be pasted or typed.
    """
    return next((path for path in watch_candidates() if path.is_dir()), None)


FIELDS = (
    ("city_id", "id", "montilla"),
    ("name", "name", "Montilla"),
    ("country", "country", "ES"),
    ("timezone", "timezone", "Europe/Madrid"),
)

POINT_FIELDS = (
    ("lat", "map centre lat", "optional: 37.58"),
    ("lon", "map centre lon", "optional: -4.64"),
)
"""Optional, and only ever used to centre the map you go and draw on.

They are not data. The polygon carries the location, the extent and the CRS, so
once there is one these are ignored entirely -- and having them look like input
is what once put a town beside Montilla into the wrong UTM zone.
"""


def drawing_url(lat: float, lon: float, zoom: int = 13) -> str:
    """A geojson.io map already over the city, so the polygon starts in the right place."""
    return f"https://geojson.io/#map={zoom}/{lat:.4f}/{lon:.4f}"


def newest_geojson(directory: Path) -> Path | None:
    """The most recently modified ``.geojson`` in a directory, if any."""
    try:
        candidates = sorted(
            directory.glob("*.geojson"), key=lambda path: path.stat().st_mtime, reverse=True
        )
    except OSError:
        return None
    return candidates[0] if candidates else None


class PasteScreen(ModalScreen[str | None]):
    """Somewhere to paste the GeoJSON that geojson.io just put on the clipboard.

    The drawing is copied, not exported, in the flow this serves: writing it to
    a file by hand and then typing that file's path back in was the step that
    made registering a city feel like paperwork.
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "cancel", "Cancel"),
        Binding("ctrl+s", "accept", "Use it"),
    ]
    DEFAULT_CSS = """
    PasteScreen { align: center middle; }
    PasteScreen > VerticalScroll {
        width: 80%; height: auto; max-height: 80%;
        border: thick $accent; background: $surface; padding: 1 2;
    }
    PasteScreen TextArea { height: 12; }
    PasteScreen #paste-hint { color: $text-muted; padding: 0 0 1 0; }
    PasteScreen Horizontal { height: auto; align: right middle; padding-top: 1; }
    """

    def compose(self) -> ComposeResult:
        with VerticalScroll():
            yield Static("[b]Paste the polygon[/b]")
            yield Static(
                "Copy the GeoJSON from geojson.io and paste it here. ctrl+s to use it.",
                id="paste-hint",
            )
            # No `language=`: syntax highlighting would need textual[syntax],
            # and nobody reads this text, they paste it.
            yield TextArea(id="pasted")
            with Horizontal():
                yield Button("Cancel", id="cancel")
                yield Button("Use it", id="accept", variant="primary")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#pasted", TextArea).focus()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_accept(self) -> None:
        text = self.query_one("#pasted", TextArea).text.strip()
        self.dismiss(text or None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "accept":
            self.action_accept()
        else:
            self.action_cancel()


class NewCityScreen(Screen[str | None]):
    """Fill in a city, price it, and write its file."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "cancel", "Back"),
        Binding("p", "paste", "Paste the polygon"),
        # `q` quits the app from any non-modal screen, and with focus on a
        # button -- one tab away -- it took the whole form with it, unasked.
        Binding("q", "back", "Back"),
        Binding("ctrl+s", "save", "Write the city file"),
    ]
    DEFAULT_CSS = """
    NewCityScreen Grid { grid-size: 2; grid-columns: 18 1fr; grid-rows: 3; height: auto; }
    NewCityScreen Label { padding: 1 0 0 0; }
    NewCityScreen #derived { padding: 1; background: $panel; height: auto; }
    NewCityScreen Horizontal { height: auto; align: right middle; }
    """

    def __init__(self, cities_dir: Path, data_root: Path, watch_dir: Path | None = None) -> None:
        super().__init__()
        self.cities_dir = cities_dir
        self.data_root = data_root
        self.watch_dir = watch_dir
        self.polygon: Path | None = None
        self._seen: float = 0.0
        self._pending: Timer | None = None
        """The debounced re-read of a path being typed, if one is due.

        Every keystroke used to re-open and re-parse the file, so typing a path
        read it once per character.
        """
        self._why: str | None = None
        """Why the polygon in hand cannot be used, when there is one and it cannot.

        ``read_area`` composes an exact message -- which file, and what is wrong
        with it -- and this screen used to throw it away in three places and
        report "needs an id and a polygon" instead. With an id typed and a path
        typed that is the one sentence which cannot be true, and it sent
        somebody looking for the wrong problem: the path had a typo in it and
        nothing on screen said so.
        """

    def compose(self) -> ComposeResult:
        yield Header()
        with VerticalScroll():
            with Grid():
                for key, label, placeholder in FIELDS:
                    yield Label(label)
                    yield Input(placeholder=placeholder, id=key)
                # The polygon comes before the point on purpose: it is the one
                # that matters, and the point below it is only a map centre.
                yield Label("polygon")
                yield Input(placeholder=self.polygon_placeholder(), id="polygon")
                for key, label, placeholder in POINT_FIELDS:
                    yield Label(label)
                    yield Input(placeholder=placeholder, id=key)
            yield Static(id="derived")
            yield CostPanel(id="cost")
            with Horizontal():
                yield Button("Back", id="cancel")
                yield Button("Write it", id="save", variant="primary")
        yield Footer()

    def polygon_placeholder(self) -> str:
        if self.watch_dir is None:
            return "press p and paste it, or type a path"
        return f"blank = watching {self.watch_dir}"

    def on_mount(self) -> None:
        self.sub_title = "new city"
        self.refresh_derived()
        if self.watch_dir is not None:
            self.set_interval(2.0, self.look_for_polygon)

    def _point(self) -> tuple[float, float] | None:
        try:
            return (
                float(self.query_one("#lat", Input).value),
                float(self.query_one("#lon", Input).value),
            )
        except ValueError:
            return None

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id in {"lat", "lon"}:
            self.refresh_derived()
        if event.input.id == "polygon":
            value = event.value.strip()
            # The else branch is the point: emptying the field used to leave the
            # last path typed in place, so the screen went on pricing a polygon
            # that was no longer named anywhere on it.
            self.polygon = Path(value) if value else None
            # Debounced, because a path arrives one character at a time and
            # every one of them re-read and re-parsed the file.
            if self._pending is not None:
                self._pending.stop()
            self._pending = self.set_timer(DEBOUNCE_SECONDS, self.refresh_derived)

    def refresh_derived(self) -> None:
        """Show what is known so far: the CRS, and where to go and draw."""
        # Cleared here because this is where every recompute starts; each panel
        # below reads it straight after the call that could have set it.
        self._why = None
        panel = self.query_one("#derived", Static)
        chosen = self.city_crs()
        # This panel is the one place where text of ours and text from outside
        # share a widget, so markup stays on and the outside parts are escaped
        # one by one: a parser's message full of brackets, or an export named
        # `area[1].geojson`, is content and not a tag.
        watching = None if self.watch_dir is None else escape(str(self.watch_dir))
        if chosen is None:
            if self._why:
                panel.update(escape(self._why))
            else:
                dropping = f", or drop a .geojson in {watching}" if watching else ""
                panel.update(
                    f"press [b]p[/b] and paste the GeoJSON from geojson.io, "
                    f"or type a path above{dropping}.\n"
                    "A map centre below is optional: it only opens geojson.io in the right place."
                )
            # And the price goes with it. Returning here left the last polygon's
            # figures on screen after the polygon itself was gone, which is a
            # price for a city nothing on the screen describes any more.
            self.refresh_cost()
            return
        code, description = chosen
        source = "from the polygon" if self.polygon is not None else "from the point, provisional"
        lines = [f"crs: [b]{code}[/b]  ({description})  [dim]{source}[/dim]"]
        point = self._point()
        if point is not None:
            lines.append(f"draw the area here: {drawing_url(*point)}")
        if self.polygon is not None:
            lines.append(f"picked up: {escape(str(self.polygon))}")
        elif watching is not None:
            lines.append(f"export it and this screen picks it up from {watching}")
        panel.update("\n".join(lines))
        self.refresh_cost()

    def look_for_polygon(self) -> None:
        """Adopt a freshly exported polygon without being asked."""
        if self.watch_dir is None or self.query_one("#polygon", Input).value.strip():
            return
        found = newest_geojson(self.watch_dir)
        if found is None:
            return
        stamp = found.stat().st_mtime
        if found == self.polygon and stamp == self._seen:
            return
        self.polygon, self._seen = found, stamp
        self.notify(f"picked up {found.name}")
        self.refresh_derived()

    def city_crs(self) -> tuple[str, str] | None:
        """The CRS this city will use. Once there is a polygon, the polygon decides.

        Deriving it from the typed point instead is how a city gets registered
        in the wrong zone: a longitude typed without its minus sign put a town
        beside Montilla into UTM 31N, and because the *bbox* came from the
        polygon while the *CRS* came from the point, the two disagreed by nine
        degrees with nothing in between to notice. The point now only centres
        the drawing URL, which is all it was ever needed for.
        """
        if self.polygon is not None:
            try:
                drawn = read_area(self.polygon, WGS84)
            except (AreaError, OSError, ValueError) as error:
                # read_area's message names the file and what is wrong with it,
                # which is the whole of what is worth saying.
                self._why = str(error)
                return None
            min_lon, min_lat, max_lon, max_lat = drawn.wgs84.bounds
            point: tuple[float, float] | None = (
                (min_lat + max_lat) / 2.0,
                (min_lon + max_lon) / 2.0,
            )
        else:
            point = self._point()
        if point is None:
            return None
        try:
            return utm_crs(*point)
        except AreaError as error:
            self._why = str(error)
            return None

    def _draft(self) -> CityConfig | None:
        """A provisional config good enough to price, or None if not ready yet."""
        city_id = self.query_one("#city_id", Input).value.strip()
        chosen = self.city_crs()
        if not city_id or self.polygon is None or chosen is None:
            return None
        code, _ = chosen
        try:
            drawn = read_area(self.polygon, code)
            check_area_of_use(drawn, code)
            # Inside the try, and not after it: a ValidationError *is* a
            # ValueError, and building this outside meant that a timezone with
            # one letter missing reached the key handler and took the whole
            # form with it -- from ctrl+s, from the watcher, or from any key
            # typed into another field.
            return CityConfig(
                id=city_id,
                name=self.query_one("#name", Input).value.strip() or city_id,
                country=self.query_one("#country", Input).value.strip() or "ES",
                timezone=self.query_one("#timezone", Input).value.strip() or "Europe/Madrid",
                crs=code,
                bbox=snap_bbox(drawn.projected.bounds, 1.0),
                area=str(self.polygon),
            )
        except (AreaError, OSError, ValueError) as error:
            # Reachable where city_crs was not: the polygon reads in WGS84 and
            # still does not belong in the projected CRS it implies.
            self._why = str(error)
            return None

    def refresh_cost(self) -> None:
        draft = self._draft()
        panel = self.query_one("#cost", CostPanel)
        if draft is None:
            panel.clear(self._why or "an id and a polygon, and the price appears here")
            return
        panel.show(draft, config_path=self.cities_dir / f"{draft.id}.yaml")

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_back(self) -> None:
        """Leave, but not without asking when there is something to lose."""
        typed = any(
            self.query_one(f"#{key}", Input).value.strip()
            for key in ("city_id", "name", "country", "timezone", "polygon", "lat", "lon")
        )
        if not typed and self.polygon is None:
            self.dismiss(None)
            return
        self.app.push_screen(
            ConfirmScreen("Discard this city?", "Nothing has been written yet.", "Discard"),
            self.discarded,
        )

    def discarded(self, confirmed: bool | None) -> None:
        if confirmed:
            self.dismiss(None)

    def action_paste(self) -> None:
        """Take the drawing off the clipboard instead of off a file."""
        self.app.push_screen(PasteScreen(), self.polygon_pasted)

    def polygon_pasted(self, text: str | None) -> None:
        """Validate what was pasted, park it in a file, and treat it as a path.

        A draft on disk and not an object in memory because ``plan_city``
        prices a city by reading ``config.area``: without a file there is no
        price, and the price appearing as you paste is the point of the screen.
        Parking it in the path field also means every other method here --
        ``city_crs``, ``_draft``, saving -- keeps seeing exactly what it saw
        before, which is a path.
        """
        if text is None:
            return
        try:
            json.loads(text)
        except json.JSONDecodeError as error:
            # Checked before writing: a draft that is not JSON is not worth
            # leaving on disk, and read_area would only say the same later.
            self.show_reason(f"what you pasted is not valid JSON ({error})")
            return
        draft = self.data_root / DRAFTS_DIRNAME / "pasted.geojson"
        try:
            draft.parent.mkdir(parents=True, exist_ok=True)
            draft.write_text(text, encoding="utf-8")
        except OSError as error:
            self.show_reason(str(error))
            return
        self.query_one("#polygon", Input).value = str(draft)

    def show_reason(self, reason: str) -> None:
        """Put a reason on screen now, rather than through a recompute.

        ``refresh_derived`` clears ``_why`` on the way in -- it is the start of
        every recompute -- so a reason set just before calling it would be
        thrown away by the call meant to display it.
        """
        self.query_one("#derived", Static).update(escape(reason))
        self.notify(reason, severity="error")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "save":
            self.action_save()
        else:
            self.action_cancel()

    def action_save(self) -> None:
        self._why = None
        draft = self._draft()
        if draft is None:
            # "needs an id and a polygon" only when that is what is missing. A
            # path was typed and the file is not there is a different sentence,
            # and it is the one that gets you to the typo.
            self.notify(self._why or "needs an id and a polygon", severity="error")
            return
        chosen = self.city_crs()
        if chosen is None:
            self.notify(self._why or "cannot work out the CRS for this area", severity="error")
            return
        code, description = chosen
        try:
            area_path = self.cities_dir / draft.id / "area.geojson"
            text = new_city_yaml(
                city_id=draft.id,
                name=draft.name,
                country=draft.country,
                timezone=draft.timezone,
                crs=code,
                crs_note=description,
                bbox=draft.bbox,
                area=str(area_path),
            )
            written = write_new_city(self.cities_dir, text, draft.id)
            # The polygon is normalised through the tool's own writer, so the
            # file on disk is the same shape `shade-engine area --write` leaves.
            assert self.polygon is not None
            area_path.parent.mkdir(parents=True, exist_ok=True)
            area_path.write_text(
                area_geojson(read_area(self.polygon, code), draft.id), encoding="utf-8"
            )
        except (CityFileError, AreaError, OSError, ValueError) as error:
            self.notify(str(error), severity="error")
            return
        self.notify(f"wrote {written} and {area_path}")
        self.dismiss(draft.id)
