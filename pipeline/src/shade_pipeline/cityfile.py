"""Writing a city YAML without destroying it.

Every file under ``cities/`` is annotated by hand -- what the CRS is, why the
sweep radius is 500, which PNOA series the tiles came from -- and those comments
are the only place some of that reasoning lives. A parse-and-dump round trip
deletes all of it silently, which is why :func:`shade_pipeline.area.rewrite_config`
already edits the ``bbox:`` and ``area:`` lines textually. This is the same
approach widened to any top-level scalar, so the console can offer to edit a
setting without costing the file its history.

Two rules keep that safe:

- **Only top-level scalars.** Nested blocks (``tree_inventory``, ``layers``) and
  the two lines ``shade-engine area`` owns are left alone.
- **Nothing is written that does not parse.** Every edit is validated with
  ``load_city`` before it reaches disk; a rejected edit leaves the file exactly
  as it was. A hand-annotated city config is far too expensive to lose to a
  regex that matched one line too many.
"""

import re
import tempfile
from pathlib import Path
from typing import Any

from shade_core.config import CityConfig, load_city

PROTECTED: frozenset[str] = frozenset({"id", "bbox", "area"})
"""Keys this module refuses to touch.

``bbox`` and ``area`` belong to ``shade-engine area``, which is the only thing
that knows how to snap a box to whole pixels and keep the polygon consistent
with it. ``id`` names the file, the artifact directory and the URL: changing it
in place would orphan everything already built.
"""


class CityFileError(ValueError):
    """The edit was refused, and the file was not touched."""


def _scalar_line(key: str) -> re.Pattern[str]:
    # The value runs to the first ` #`, so a trailing comment survives and a
    # `#` inside a value (a URL fragment) does not falsely start one.
    return re.compile(rf"^{re.escape(key)}:[ \t]*(?P<value>[^#\n]*?)[ \t]*(?P<comment>#.*)?$")


def _format(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        # 1.0 has to stay 1.0: bare `1` still loads as a float here, but the
        # file is read by people too and the unit matters.
        return f"{value:g}" if value != int(value) else f"{value:.1f}"
    return str(value)


def rewrite_scalar(text: str, key: str, value: Any) -> str:
    """Set one top-level scalar, keeping the layout and the trailing comment.

    Raises :class:`CityFileError` if the key is protected, missing, or appears
    more than once at the top level.
    """
    if key in PROTECTED:
        raise CityFileError(
            f"{key} is not editable here; bbox and area go through "
            "`shade-engine area`, and id names everything already built"
        )
    pattern = _scalar_line(key)
    lines = text.split("\n")
    matches = [index for index, line in enumerate(lines) if pattern.match(line)]
    if len(matches) != 1:
        raise CityFileError(f"expected exactly one top-level '{key}:' line, found {len(matches)}")
    index = matches[0]
    found = pattern.match(lines[index])
    assert found is not None
    comment = found.group("comment")
    lines[index] = f"{key}: {_format(value)}" + (f" {comment}" if comment else "")
    return "\n".join(lines)


def edit_city(path: Path, key: str, value: Any) -> CityConfig:
    """Rewrite one setting of a city file, validating before anything is saved.

    The new text is parsed from a scratch file rather than from a string,
    because that is the code path the engine really uses -- ``load_city`` reads
    a path -- and a config that only validates in memory is not validated.
    """
    original = path.read_text(encoding="utf-8")
    updated = rewrite_scalar(original, key, value)
    config = _validated(updated, path)
    path.write_text(updated, encoding="utf-8")
    return config


def _validated(text: str, like: Path) -> CityConfig:
    with tempfile.TemporaryDirectory() as scratch:
        candidate = Path(scratch) / like.name
        candidate.write_text(text, encoding="utf-8")
        try:
            return load_city(candidate)
        except Exception as error:
            raise CityFileError(f"the edit does not produce a valid city: {error}") from error


def new_city_yaml(
    *,
    city_id: str,
    name: str,
    country: str,
    timezone: str,
    crs: str,
    crs_note: str,
    bbox: tuple[float, float, float, float],
    area: str | None,
    resolution_m: float = 1.0,
    horizon_sectors: int = 64,
    horizon_max_distance_m: float = 500.0,
    pnoa_series: str = "LIDA3",
    attribution: str = "Obra derivada de PNOA-cob3 2022-2025 CC-BY 4.0 scne.es",
) -> str:
    """A new city file, annotated the way the hand-written ones are.

    The comments are the point. A file generated bare would be correct and
    would teach nobody why the bbox is in meters or what ``pnoa_series``
    selects, which is exactly the knowledge these files carry today.
    """
    min_x, min_y, max_x, max_y = (round(value) for value in bbox)
    lines = [
        f"id: {city_id}",
        f"name: {name}",
        f"country: {country}",
        f"timezone: {timezone} # IANA; se valida al cargar",
        f"crs: {crs} # {crs_note}",
        f"bbox: [{min_x}, {min_y}, {max_x}, {max_y}] # en CRS local (metros), no lat/lon",
    ]
    if area is not None:
        lines.append(f"area: {area} # area de computo dentro del bbox (ADR-020)")
    lines += [
        f"resolution_m: {_format(resolution_m)}",
        f"horizon_sectors: {horizon_sectors}",
        f"horizon_max_distance_m: {_format(horizon_max_distance_m)}"
        " # radio del barrido; tambien buffer del bbox",
        "sources:",
        "  lidar: pnoa # driver de descarga (CnigSource)",
        f"  pnoa_series: {pnoa_series} # codSerie del centro de descargas",
        "attribution:",
        f"  - {attribution}",
        "",
    ]
    return "\n".join(lines)


def write_new_city(directory: Path, text: str, city_id: str) -> Path:
    """Save a generated city file, refusing to overwrite one that exists."""
    path = directory / f"{city_id}.yaml"
    if path.exists():
        raise CityFileError(f"{path} already exists; pick another id or edit that one")
    _validated(text, path)
    directory.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path
