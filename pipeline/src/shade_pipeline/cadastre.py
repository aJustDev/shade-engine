"""Building footprints from the Spanish cadastre, as a second opinion.

This engine draws buildings from its own LiDAR (:mod:`shade_pipeline.outlines`),
and that drawing carries the LiDAR's own reading of the town: what the aircraft
saw in 2024, eaves included, sheds and all. The cadastre carries a different
one -- what is registered, wall by wall, kept up to date by an administration
whose job it is. Neither is the other's correction, and the viewer offers both
because they disagree in ways that are worth seeing.

**It is a drawing and only a drawing.** The shade keeps coming from the 1 m
LiDAR raster and nothing here ever touches it. In particular this is *not* the
footprint source of :mod:`shade_pipeline.footprints`, which uses OSM to fix
misclassified roofs before the sweep; swapping that would change every shadow
in the city and belongs to its own decision. See
``shade-docs: decisions/ADR-030-dos-fuentes-de-edificio.md``.

**The service.** ``BU.Building`` over the INSPIRE WFS 2.0 the Direccion General
del Catastro runs. Two things about it decide the shape of this module:

- **There is a cap on the bbox.** Asking for Montalban whole (1048 x 1954 m)
  comes back as ``Area of extension out of limits``; a 500 x 500 m window
  answers with 263 buildings and 1.2 MB. So a city is fetched as a grid of
  windows and the pieces are joined by ``gml:id``, which is what makes a
  building straddling two windows arrive once.
- **Axis order.** A WFS 2.0 honours the *authority* order of the CRS it is
  given, which for EPSG:4326 is (lat, lon) and not the (lon, lat) GeoJSON
  promises. Asking in the city's projected CRS sidesteps the whole argument:
  EPSG:25830 is (easting, northing) either way. Same rule and same reason as
  :class:`shade_pipeline.trees.WfsTreeSource`.

Attribution is not optional: the layer is published under the cadastre's terms
and the string travels in the manifest so the viewer shows it.

See ``shade-docs: learning/catastro-inspire.md``.
"""

import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final
from xml.etree import ElementTree

import httpx
from shapely.geometry import Polygon

from shade_core.config import Bbox

CATASTRO_WFS_URL: Final = "http://ovc.catastro.meh.es/INSPIRE/wfsBU.aspx"

CATASTRO_ATTRIBUTION: Final = "Direccion General del Catastro"
"""What the viewer has to say when it draws this layer."""

DEFAULT_CADASTRE_CACHE: Final = Path("data/cache/cadastre")

WINDOW_M: Final = 500.0
"""Side of the request window, in metres.

The server refuses a bbox above some undocumented area with
``Area of extension out of limits``. 500 m is comfortably under it -- measured:
a 500 m window over Montalban's centre answers with 263 buildings and 1.2 MB --
and large enough that a city is tens of requests, not thousands.
"""

HTTP_TIMEOUT_S: Final = 180.0
"""Same as the tree inventory's: a public administration WFS is not fast."""

MIN_AREA_M2: Final = 5.0
"""Below this a cadastral polygon is a porch or a slip, not a building to draw."""

_GML_ID: Final = "{http://www.opengis.net/gml/3.2}id"


@dataclass(frozen=True)
class CadastreSource:
    """``BU.Building`` polygons over a projected bbox, cached on disk.

    ``client`` exists so tests can answer without a network; left unset the
    module makes its own requests, like every other source here.
    """

    url: str = CATASTRO_WFS_URL
    cache_dir: Path = DEFAULT_CADASTRE_CACHE
    window_m: float = WINDOW_M
    client: httpx.Client | None = field(default=None, compare=False)

    def fetch(self, bbox: Bbox, crs: str) -> list[Polygon]:
        """Every registered building over ``bbox``, in ``crs``, deduplicated.

        A window that fails -- the service is down, or refuses that particular
        rectangle -- takes its own window with it and not the layer: this is an
        aid, and half a city drawn is better than none. What is *not* tolerated
        is a malformed answer parsed into silence, so a body that is neither a
        feature collection nor a recognisable exception still raises.
        """
        found: dict[str, Polygon] = {}
        for window in _windows(bbox, self.window_m):
            try:
                payload = self._payload(window, crs)
            except httpx.HTTPError, CadastreError:
                continue
            found.update(_buildings(payload))
        return [polygon for polygon in found.values() if polygon.area >= MIN_AREA_M2]

    def _payload(self, window: Bbox, crs: str) -> bytes:
        path = self.cache_dir / _cache_name(window, crs)
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(self._download(window, crs))
        return path.read_bytes()

    def _download(self, window: Bbox, crs: str) -> bytes:
        urn = _urn(crs)
        min_x, min_y, max_x, max_y = window
        params = {
            "service": "WFS",
            "version": "2.0.0",
            "request": "GetFeature",
            "TYPENAMES": "BU.Building",
            "SRSNAME": urn,
            "bbox": f"{min_x},{min_y},{max_x},{max_y},{urn}",
        }
        get = self.client.get if self.client is not None else httpx.get
        response = get(self.url, params=params, timeout=HTTP_TIMEOUT_S, follow_redirects=True)
        response.raise_for_status()
        return bytes(response.content)


class CadastreError(RuntimeError):
    """The service answered, and what it said was not buildings."""


def _urn(crs: str) -> str:
    """``EPSG:25830`` -> the URN form the service wants in both places.

    The bbox parameter carries its own CRS, and it has to be the same one the
    features come back in or the numbers are read against the wrong axes.
    """
    match = re.fullmatch(r"EPSG:(\d+)", crs.strip(), flags=re.IGNORECASE)
    if match is None:
        raise CadastreError(f"{crs!r} is not an EPSG code this service can be asked for")
    return f"urn:ogc:def:crs:EPSG::{match.group(1)}"


def _cache_name(window: Bbox, crs: str) -> str:
    """A cache filename that says what is in it: CRS and rounded window."""
    safe = re.sub(r"[^A-Za-z0-9]+", "-", crs).strip("-")
    return f"bu-building-{safe}-" + "-".join(str(round(value)) for value in window) + ".gml"


def _windows(bbox: Bbox, window_m: float) -> Iterator[Bbox]:
    """``bbox`` cut into request-sized rectangles, covering it entirely.

    No overlap is added: a WFS bbox filter selects features that *intersect*
    the rectangle, so a building on the seam comes back from both windows and
    the ``gml:id`` join drops the copy.
    """
    min_x, min_y, max_x, max_y = bbox
    x = min_x
    while x < max_x:
        y = min_y
        while y < max_y:
            yield (x, y, min(x + window_m, max_x), min(y + window_m, max_y))
            y += window_m
        x += window_m


def _local(tag: str) -> str:
    """The tag without its namespace: this document has six of them."""
    return tag.rsplit("}", 1)[-1]


def _buildings(payload: bytes) -> dict[str, Polygon]:
    """``{gml:id: polygon}`` for every building in one WFS response."""
    try:
        root = ElementTree.fromstring(payload)
    except ElementTree.ParseError as error:
        raise CadastreError(f"the service did not answer with XML: {error}") from error
    if _local(root.tag) == "ExceptionReport":
        text = " ".join(
            node.text or "" for node in root.iter() if _local(node.tag).endswith("Text")
        )
        raise CadastreError(text.strip() or "the service returned an exception with no text")

    found: dict[str, Polygon] = {}
    for element in root.iter():
        if _local(element.tag) != "Building":
            continue
        identifier = element.get(_GML_ID)
        if identifier is None:
            continue
        parts = [patch for patch in _patches(element) if not patch.is_empty]
        if not parts:
            continue
        # A building registered as several patches is one entry with one id;
        # the largest is the one that carries its shape.
        found[identifier] = max(parts, key=lambda patch: patch.area)
    return found


def _patches(building: ElementTree.Element) -> Iterator[Polygon]:
    """Each ``PolygonPatch`` under a building, holes included."""
    for patch in building.iter():
        if _local(patch.tag) != "PolygonPatch":
            continue
        shell: list[tuple[float, float]] | None = None
        holes: list[list[tuple[float, float]]] = []
        for boundary in patch:
            coordinates = _positions(boundary)
            if coordinates is None:
                continue
            if _local(boundary.tag) == "exterior":
                shell = coordinates
            elif _local(boundary.tag) == "interior":
                holes.append(coordinates)
        if shell is None or len(shell) < 4:
            continue
        polygon = Polygon(shell, [hole for hole in holes if len(hole) >= 4])
        if not polygon.is_valid:
            polygon = polygon.buffer(0)
        if polygon.geom_type == "Polygon" and not polygon.is_empty:
            yield polygon


def _positions(boundary: ElementTree.Element) -> list[tuple[float, float]] | None:
    """The ring under ``boundary`` as (x, y) pairs, in the CRS it was asked in.

    ``srsDimension`` is read rather than assumed: the cadastre ships 2D today,
    and a 3D ring parsed two-at-a-time would come out as a spiral of nonsense
    rather than as an error.
    """
    for node in boundary.iter():
        if _local(node.tag) != "posList" or not node.text:
            continue
        stride = int(node.get("srsDimension", "2"))
        values = [float(value) for value in node.text.split()]
        return [(values[index], values[index + 1]) for index in range(0, len(values), stride)]
    return None
