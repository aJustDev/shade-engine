"""City configuration schema.

Each city is a deployment unit described by one YAML file under ``cities/``
(spec, section 4). Adding a city to the engine means adding one file and
running the pipeline; no code changes.

A note on ``crs`` and ``bbox``: the bounding box is expressed in the city's
*local projected* CRS (e.g. ``EPSG:25830``, UTM zone 30N for Cordoba), where
coordinates are meters, not degrees. All raster processing and distance math
happens in that CRS; latitude/longitude (EPSG:4326) only appears at the API
boundary. See ``docs/learning/crs.md`` for the rationale and the classic
lat/lon vs lon/lat trap.
"""

from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import BaseModel, Field, field_validator

Bbox = tuple[float, float, float, float]


class CityConfig(BaseModel):
    """Validated contents of a ``cities/<id>.yaml`` file."""

    id: str
    name: str
    country: str
    timezone: str
    crs: str
    bbox: Bbox = Field(description="(min_x, min_y, max_x, max_y) in the local CRS, meters")
    resolution_m: float = Field(default=1.0, gt=0)
    horizon_sectors: int = Field(default=64, gt=0)
    horizon_max_distance_m: float = Field(
        default=500.0, gt=0, description="Horizon sweep radius; also pads the bbox"
    )
    observer_height_m: float = Field(default=1.6, gt=0)
    sources: dict[str, str] = Field(default_factory=dict)
    layers: dict[str, str] = Field(default_factory=dict)
    attribution: list[str] = Field(default_factory=list)

    @field_validator("timezone")
    @classmethod
    def _known_iana_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"unknown IANA timezone: {value!r}") from exc
        return value

    @field_validator("bbox")
    @classmethod
    def _ordered_bbox(cls, value: Bbox) -> Bbox:
        min_x, min_y, max_x, max_y = value
        if not (min_x < max_x and min_y < max_y):
            raise ValueError("bbox must be (min_x, min_y, max_x, max_y) with min < max")
        return value


def load_city(path: str | Path) -> CityConfig:
    """Load and validate a city YAML file."""
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return CityConfig.model_validate(raw)
