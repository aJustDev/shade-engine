"""Parse Cordoba's archived municipal traffic viewer into the parking layer.

The Ayuntamiento de Cordoba once served an official traffic viewer at
https://movilidad.cordoba.es/informaciontrafico with the regulated parking
(zona azul) drawn as OpenLayers vectors inline in the page's JavaScript.
The viewer is dead today (the municipal open data portal still links it),
but the Wayback Machine holds a full capture. This script turns that capture
into ``cities/cordoba/parking.geojson`` following the schema of the spec's
section 5.1.

Input: the capture saved locally (this script never touches the network).
Download it once with the frozen-timestamp URL in ``WAYBACK_URL`` below::

    curl -sL "<WAYBACK_URL>" -o visor.html
    python scripts/parse_cordoba_parking.py --input visor.html \
        --output cities/cordoba/parking.geojson

How the capture is structured (verified 2026-07-12): each street segment is a
``var str = [[lon,lat],...]`` array fed to an ``ol.geom.LineString`` and styled
with a stroke color -- ``#007bfe`` for zona azul, red for bike lanes. Segments
of the same parking zone appear consecutively, closed by ONE marker feature
(``ol.geom.Point`` + ``name: '...'`` + icon ``marcaZonaAzul.png``) whose popup
text carries the street name, the number of stalls (plazas), the layout
(bateria = perpendicular, cordon = parallel) and the full schedule. Grouping
is therefore by document order, no spatial join needed. Trap: the off-street
parking layer at the end of the document draws 7 access lines in the SAME
blue before its ``marcaParking.png`` markers, so the marker icon -- not the
stroke color -- decides whether an accumulated group is a zona azul zone.

Tariff attributes come from the Ordenanza Fiscal 407 (ejercicio 2026, Anexo I,
Tarifa 2): the bracket table steps every 3m43s, paying 0.90 EUR covers 63 min
(0.85 only covers 59m31s), the 2 h cap costs 1.70 EUR and 2 h is the ordinary
maximum stay -- hence ``tariff_eur_hour = 0.90`` and ``max_minutes = 120``.

The capture is frozen, so this parser is strict: any drift from the counts and
text patterns observed in it (58 blue segments, 21 markers, two schedule
variants) aborts loudly instead of guessing.
"""

import argparse
import json
import re
import unicodedata
from pathlib import Path
from typing import Any

WAYBACK_URL = (
    "https://web.archive.org/web/20240903081026/"
    "https://movilidad.cordoba.es/informaciontrafico/index.php"
    "?minusvalidos=1&bicicletas=1&zonaAzul=1&cargaDescarga=1"
    "&aparcamientoMotocicletas=1&parking=1"
)
CAPTURE_DATE = "2024-09-03"
ZONA_AZUL_COLOR = "007bfe"
ZONA_AZUL_ICON = "marcaZonaAzul"
EXPECTED_SEGMENTS = 51
EXPECTED_ZONES = 21
EXPECTED_STRAY_BLUE = 7  # off-street parking access lines, same blue, discarded

LINE_RE = re.compile(r"var str = (\[\[[-0-9., \[\]]+\]\]);")
COLOR_RE = re.compile(r"color: '#([0-9a-fA-F]{6})'")
# The page generator sprinkles stray spaces inside coordinate arrays (both
# in LineString "var str" blocks and in fromLonLat points), so every numeric
# character class below must accept them.
MARKER_RE = re.compile(
    r"geometry: new ol\.geom\.Point\(ol\.proj\.fromLonLat\(\[[-0-9., ]+\]\)\),"
    r"\s*name: '([^']*)'[\s\S]{0,900}?src: '\./marcas/(\w+)\.png'"
)
NAME_RE = re.compile(
    r"^(?P<street>.+?) (?P<plazas>\d+) PLAZAS EN (?P<orientation>BATERIA|CORDON) "
    r"HORARIO: (?P<schedule>.+)$"
)

# The capture contains exactly two schedule texts (after ASCII normalization):
# the commercial zone (mornings + evenings + Saturday mornings) and the
# administrative zone (weekday mornings only). Anything else is drift.
SCHEDULES = {
    (
        "LUNES A VIERNES DE 09:00 A 14:00 HORAS Y 17:00 A 21:00, "
        "SABADOS DE 09:00 A 14:00 HORAS, DOMINGOS Y FESTIVOS LIBRES"
    ): [
        {"days": "mo-fr", "from": "09:00", "to": "14:00"},
        {"days": "mo-fr", "from": "17:00", "to": "21:00"},
        {"days": "sa", "from": "09:00", "to": "14:00"},
    ],
    ("LUNES A VIERNES DE 09:00 A 14:00 HORAS, SABADOS, DOMINGOS Y FESTIVOS LIBRES"): [
        {"days": "mo-fr", "from": "09:00", "to": "14:00"},
    ],
}

SOURCE = (
    "Visor de trafico movilidad.cordoba.es via Wayback Machine (2024-09-03); "
    "tarifas Ordenanza Fiscal 407 ejercicio 2026"
)
TARIFF_NOTE = (
    "tariff_eur_hour = tramo de la Tarifa 2 (Anexo I, OF 407/2026) que cubre "
    "60 min (0.90 EUR llega a 1h03m; la tabla avanza en tramos de 3m43s, "
    "tope 2 h = 1.70 EUR)"
)


def normalize_popup_text(raw: str) -> str:
    """Popup text -> single ASCII line: <br> to spaces, NFKD, collapsed runs."""
    text = re.sub(r"<br\s*/?>", " ", raw)
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", text).strip()


def parse_marker(raw_name: str) -> dict[str, Any]:
    """Popup text -> spec 5.1 properties; unknown text patterns abort."""
    text = normalize_popup_text(raw_name)
    match = NAME_RE.match(text)
    if match is None:
        raise ValueError(f"unrecognized zona azul popup text: {text!r}")
    schedule = SCHEDULES.get(match["schedule"])
    if schedule is None:
        raise ValueError(f"unknown schedule text: {match['schedule']!r}")
    return {
        "name": match["street"],
        "zone_type": "blue",
        "orientation": match["orientation"].lower(),
        "capacity": int(match["plazas"]),
        "schedule": schedule,
        "max_minutes": 120,
        "tariff_eur_hour": 0.90,
        "notes": f"Texto original del visor: {text!r}. {TARIFF_NOTE}",
        "source": SOURCE,
        "last_verified": CAPTURE_DATE,
    }


def parse_viewer(html: str) -> list[dict[str, Any]]:
    """Walk the capture in document order grouping blue segments per marker.

    Events: a segment's coordinates arrive before its stroke color (same
    script block), and a zone's marker arrives after all its segments. Blue
    segments accumulate until a marcaZonaAzul marker closes the group as one
    MultiLineString feature. Every invariant observed in the frozen capture
    is asserted so silent drift cannot produce a wrong layer.
    """
    events: list[tuple[int, str, Any]] = []
    for line in LINE_RE.finditer(html):
        events.append((line.start(), "line", json.loads(line[1])))
    for color in COLOR_RE.finditer(html):
        events.append((color.start(), "color", color[1].lower()))
    for marker in MARKER_RE.finditer(html):
        events.append((marker.start(), "marker", (marker[1], marker[2])))
    events.sort(key=lambda event: event[0])

    pending: list[list[float]] | None = None
    blue_segments: list[list[list[float]]] = []
    features: list[dict[str, Any]] = []
    total_segments = 0
    stray_blue = 0
    for _, kind, payload in events:
        if kind == "line":
            if pending is not None:
                raise ValueError("two LineStrings without a stroke color between them")
            pending = payload
        elif kind == "color":
            if pending is None:
                continue  # colors also appear in marker-less styling blocks
            if payload == ZONA_AZUL_COLOR:
                blue_segments.append([[round(x, 6), round(y, 6)] for x, y in pending])
            pending = None
        else:
            # Every marker flushes the accumulator: a zona azul marker claims
            # the segments as its zone, any other icon disowns them (blue
            # off-street parking access lines).
            name, icon = payload
            if icon == ZONA_AZUL_ICON:
                if not blue_segments:
                    raise ValueError("zona azul marker without preceding blue segments")
                total_segments += len(blue_segments)
                features.append(
                    {
                        "type": "Feature",
                        "properties": parse_marker(name),
                        "geometry": {"type": "MultiLineString", "coordinates": blue_segments},
                    }
                )
            else:
                stray_blue += len(blue_segments)
            blue_segments = []
    if blue_segments:
        raise ValueError(f"{len(blue_segments)} blue segments left without a closing marker")
    if (total_segments, len(features), stray_blue) != (
        EXPECTED_SEGMENTS,
        EXPECTED_ZONES,
        EXPECTED_STRAY_BLUE,
    ):
        raise ValueError(
            f"capture drift: {total_segments} segments in {len(features)} zones "
            f"plus {stray_blue} stray blue lines, expected "
            f"{EXPECTED_SEGMENTS}/{EXPECTED_ZONES}/{EXPECTED_STRAY_BLUE}"
        )
    return features


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--input", type=Path, required=True, help="saved Wayback capture (HTML)")
    parser.add_argument("--output", type=Path, required=True, help="GeoJSON path to write")
    args = parser.parse_args()

    features = parse_viewer(args.input.read_text(encoding="utf-8"))
    collection = {"type": "FeatureCollection", "features": features}
    args.output.write_text(json.dumps(collection, indent=2, ensure_ascii=True) + "\n")
    stalls = sum(feature["properties"]["capacity"] for feature in features)
    print(f"{len(features)} zones, {stalls} stalls -> {args.output}")


if __name__ == "__main__":
    main()
