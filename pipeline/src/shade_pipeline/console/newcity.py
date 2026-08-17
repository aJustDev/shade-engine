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

from pathlib import Path
from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Grid, Horizontal, VerticalScroll
from textual.screen import Screen
from textual.widgets import Button, Footer, Header, Input, Label, Static

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
from shade_pipeline.console.cost import CostPanel

DEFAULT_WATCH = Path.home() / "Descargas"
"""Where a browser drops an export. Configurable; watched, never required."""

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


class NewCityScreen(Screen[str | None]):
    """Fill in a city, price it, and write its file."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "cancel", "Back"),
        Binding("ctrl+s", "save", "Write the city file"),
    ]
    DEFAULT_CSS = """
    NewCityScreen Grid { grid-size: 2; grid-columns: 18 1fr; grid-rows: 3; height: auto; }
    NewCityScreen Label { padding: 1 0 0 0; }
    NewCityScreen #derived { padding: 1; background: $panel; height: auto; }
    NewCityScreen Horizontal { height: auto; align: right middle; }
    """

    def __init__(self, cities_dir: Path, watch_dir: Path = DEFAULT_WATCH) -> None:
        super().__init__()
        self.cities_dir = cities_dir
        self.watch_dir = watch_dir
        self.polygon: Path | None = None
        self._seen: float = 0.0
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
                yield Input(placeholder=f"blank = watching {self.watch_dir}", id="polygon")
                for key, label, placeholder in POINT_FIELDS:
                    yield Label(label)
                    yield Input(placeholder=placeholder, id=key)
            yield Static(id="derived")
            yield CostPanel(id="cost")
            with Horizontal():
                yield Button("Back", id="cancel")
                yield Button("Write it", id="save", variant="primary")
        yield Footer()

    def on_mount(self) -> None:
        self.sub_title = "new city"
        self.refresh_derived()
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
        if event.input.id == "polygon" and event.value.strip():
            self.polygon = Path(event.value.strip())
            self.refresh_derived()

    def refresh_derived(self) -> None:
        """Show what is known so far: the CRS, and where to go and draw."""
        # Cleared here because this is where every recompute starts; each panel
        # below reads it straight after the call that could have set it.
        self._why = None
        panel = self.query_one("#derived", Static)
        chosen = self.city_crs()
        if chosen is None:
            panel.update(
                self._why
                or (
                    f"drop a .geojson in {self.watch_dir}, or type its path above.\n"
                    "A map centre below is optional: it only opens geojson.io in the right place."
                )
            )
            return
        code, description = chosen
        source = "from the polygon" if self.polygon is not None else "from the point, provisional"
        lines = [f"crs: [b]{code}[/b]  ({description})  [dim]{source}[/dim]"]
        point = self._point()
        if point is not None:
            lines.append(f"draw the area here: {drawing_url(*point)}")
        lines.append(
            f"picked up: {self.polygon}"
            if self.polygon is not None
            else f"export it and this screen picks it up from {self.watch_dir}"
        )
        panel.update("\n".join(lines))
        self.refresh_cost()

    def look_for_polygon(self) -> None:
        """Adopt a freshly exported polygon without being asked."""
        if self.query_one("#polygon", Input).value.strip():
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
        except (AreaError, OSError, ValueError) as error:
            # Reachable where city_crs was not: the polygon reads in WGS84 and
            # still does not belong in the projected CRS it implies.
            self._why = str(error)
            return None
        return CityConfig(
            id=city_id,
            name=self.query_one("#name", Input).value.strip() or city_id,
            country=self.query_one("#country", Input).value.strip() or "ES",
            timezone=self.query_one("#timezone", Input).value.strip() or "Europe/Madrid",
            crs=code,
            bbox=snap_bbox(drawn.projected.bounds, 1.0),
            area=str(self.polygon),
        )

    def refresh_cost(self) -> None:
        draft = self._draft()
        panel = self.query_one("#cost", CostPanel)
        if draft is None:
            panel.clear(self._why or "an id and a polygon, and the price appears here")
            return
        panel.show(draft, config_path=self.cities_dir / f"{draft.id}.yaml")

    def action_cancel(self) -> None:
        self.dismiss(None)

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
