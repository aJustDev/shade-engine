"""Field-validation predictions: shade timelines for a CSV of points.

Mirrors the API's timeline endpoint (transform once at the boundary with
``always_xy``, nearest-pixel snap via ``SceneReader.scene_for``, the city's
timezone for wall-clock times) but reads the artifacts straight from disk,
so a printed prediction sheet needs no running server. Used by the Cordoba
field-validation protocol in docs/validacion-cordoba.md.
"""

import csv
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from pyproj import Transformer

from shade_core.artifacts import SceneReader
from shade_core.config import CityConfig
from shade_core.shade import shade_timeline


@dataclass(frozen=True)
class FieldPoint:
    """One contrast point of the field-validation walk."""

    id: str
    name: str
    lat: float
    lon: float


def read_points(path: Path) -> list[FieldPoint]:
    """Points from a CSV with an ``id,name,lat,lon`` header row."""
    with path.open(newline="") as fh:
        return [
            FieldPoint(row["id"], row["name"], float(row["lat"]), float(row["lon"]))
            for row in csv.DictReader(fh)
        ]


def prediction_table(
    config: CityConfig, artifact_dir: Path, points: Sequence[FieldPoint], day: date
) -> str:
    """Markdown table of predicted daylight intervals per point, local time."""
    to_projected = Transformer.from_crs("EPSG:4326", config.crs, always_xy=True)
    lines = [
        f"Predicciones para {config.id}, {day.isoformat()} (hora local {config.timezone})",
        "",
        "| punto | intervalo | estado | tipo |",
        "| --- | --- | --- | --- |",
    ]
    with SceneReader(artifact_dir) as reader:
        for point in points:
            x, y = to_projected.transform(point.lon, point.lat)
            if not reader.contains(x, y):
                lines.append(f"| {point.id} | - | fuera de cobertura | - |")
                continue
            scene, center_x, center_y = reader.scene_for(x, y)
            intervals = shade_timeline(
                scene, center_x, center_y, point.lat, point.lon, day, config.timezone
            )
            for interval in intervals:
                span = f"{interval.start.strftime('%H:%M')}-{interval.end.strftime('%H:%M')}"
                shade_type = interval.shade_type.value if interval.shade_type else "-"
                lines.append(f"| {point.id} | {span} | {interval.state.value} | {shade_type} |")
    return "\n".join(lines) + "\n"
