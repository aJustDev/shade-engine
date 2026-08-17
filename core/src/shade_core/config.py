"""City configuration schema.

Each city is a deployment unit described by one YAML file under ``cities/``
(spec, section 4). Adding a city to the engine means adding one file and
running the pipeline; no code changes.

``bbox`` sets the georeference and the shape of every raster; the optional
``area`` polygon says which pixels inside it are worth computing, so a city
whose shape is nothing like a rectangle stops paying for the corners. Without
it, the whole bbox is the computation area, which is how every city built
before it worked.

A note on ``crs`` and ``bbox``: the bounding box is expressed in the city's
*local projected* CRS (e.g. ``EPSG:25830``, UTM zone 30N for Cordoba), where
coordinates are meters, not degrees. All raster processing and distance math
happens in that CRS; latitude/longitude (EPSG:4326) only appears at the API
boundary. See ``shade-docs: learning/crs.md`` for the rationale and the classic
lat/lon vs lon/lat trap.
"""

from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import BaseModel, Field, field_validator
from pydantic_core import PydanticUndefined

Bbox = tuple[float, float, float, float]


class TreeInventoryConfig(BaseModel):
    """Where the city publishes its tree inventory, if it publishes one.

    Optional like ``area``: a city without it builds exactly as before, minus
    the corroboration check. See :mod:`shade_pipeline.trees` for what the
    inventory is used for (auditing the canopy mask, never painting it).
    """

    wfs: str = Field(description="OGC WFS 2.0 endpoint URL")
    layers: list[str] = Field(min_length=1, description="Point layers to fetch, e.g. city:Trees")


class CityConfig(BaseModel):
    """Validated contents of a ``cities/<id>.yaml`` file.

    Every field carries what it costs and where it was decided, in
    ``json_schema_extra``. That is not decoration: it is the single source the
    console reads to explain a knob, so the explanation cannot drift away from
    the field the way a separate document would.
    """

    id: str = Field(description="Identifier: the filename, the URL segment and the artifact folder")
    name: str = Field(description="Display name, shown in the client")
    country: str = Field(description="ISO country code, informational")
    timezone: str = Field(
        description="IANA zone; naive instants in the API and the CLI mean this clock",
        json_schema_extra={"doc": "learning/solar-geometry.md"},
    )
    crs: str = Field(
        description="Local PROJECTED CRS (a UTM zone): every distance is computed in it, in meters",
        json_schema_extra={
            "cost": "none, but the wrong zone distorts every distance",
            "doc": "learning/crs.md",
        },
    )
    bbox: Bbox = Field(
        description="(min_x, min_y, max_x, max_y) in the local CRS, meters -- never lat/lon",
        json_schema_extra={
            "cost": (
                "sets the shape of every raster. The tile phase holds ~26 bytes per pixel of it "
                "for one instant, which puts the practical ceiling near 200 Mpx"
            )
        },
    )
    area: str | None = Field(
        default=None,
        description=(
            "GeoJSON (EPSG:4326) of the computation area inside the bbox; None means the whole bbox"
        ),
        json_schema_extra={
            "cost": (
                "saves sweep time, but quantised by sweep tile: a tile the polygon merely "
                "grazes is swept whole. Does not change the georeference"
            ),
            "doc": "decisions/ADR-020-area-de-computo.md",
        },
    )
    resolution_m: float = Field(
        default=1.0,
        gt=0,
        description="Pixel size of every artifact, in meters",
        json_schema_extra={
            "cost": "quadratic: halving it multiplies pixels, memory and sweep time by four"
        },
    )
    horizon_sectors: int = Field(
        default=64,
        gt=0,
        description="Azimuth sectors the horizon is sampled in",
        json_schema_extra={
            "cost": (
                "doubling it doubles both the sweep and the artifacts on disk. Measured verdict "
                "error against a ray-cast reference: 32 -> 0.94%, 64 -> 0.51%, 128 -> 0.35%, "
                "256 -> 0.22%, and 99.8% of the disagreements sit within one pixel of a boundary"
            ),
            "doc": "decisions/ADR-001-horizonte-precomputado.md",
        },
    )
    horizon_max_distance_m: float = Field(
        default=500.0,
        gt=0,
        description="Horizon sweep radius; also how far the bbox is padded before binning",
        json_schema_extra={
            "cost": (
                "raising it costs sweep time and a wider padded bbox to bin. Measured: going "
                "from 500 m to 2000 m changed no verdict across 14 instants, and even at the "
                "lowest sun the ladder renders only 0.011% of pixels moved"
            ),
            "doc": "learning/horizon-algorithm.md",
        },
    )
    observer_height_m: float = Field(
        default=1.6,
        gt=0,
        description="Eye height above the terrain (DTM), where the horizon is measured from",
        json_schema_extra={
            "cost": "none",
            "doc": "decisions/ADR-002-observador-dtm.md",
        },
    )
    tree_inventory: TreeInventoryConfig | None = Field(
        default=None,
        description="Municipal tree inventory WFS, used to audit the canopy mask",
        json_schema_extra={
            "cost": "one WFS call per build; it audits the mask and never paints it",
            "doc": "decisions/ADR-021-inventario-de-arbolado.md",
        },
    )
    sources: dict[str, str] = Field(
        default_factory=dict,
        description="LiDAR driver and its options, e.g. lidar: pnoa and pnoa_series: LIDA3",
    )
    layers: dict[str, str] = Field(
        default_factory=dict,
        description="Vector layers to import into PostGIS, as name -> GeoJSON path",
    )
    attribution: list[str] = Field(
        default_factory=list,
        description="Credit lines the API returns with every answer",
        json_schema_extra={
            "cost": "none, and required: PNOA-derived artifacts are CC-BY and must say so"
        },
    )

    @classmethod
    def explain(cls, field_name: str) -> dict[str, str]:
        """What a field is, what it costs and where it was decided.

        Read off the model rather than written out again anywhere, so the
        console and the schema can never disagree.
        """
        info = cls.model_fields[field_name]
        extra = info.json_schema_extra if isinstance(info.json_schema_extra, dict) else {}
        explanation = {"description": info.description or ""}
        for key in ("cost", "doc"):
            value = extra.get(key)
            if isinstance(value, str):
                explanation[key] = value
        if info.default is not PydanticUndefined:
            explanation["default"] = str(info.default)
        return explanation

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
    """Load and validate a city YAML file.

    A parser error becomes a ``ValueError``. A file caught halfway through a
    save is not a different kind of problem from a file that says the wrong
    thing, and every caller in the engine already writes ``except (OSError,
    ValueError)`` around this call -- but ``yaml.YAMLError`` descends from
    ``Exception`` and not from ``ValueError``, so that guard was a half-truth
    and an editor mid-save took the whole console down with it.
    """
    text = Path(path).read_text(encoding="utf-8")
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as error:
        raise ValueError(f"{path} is not readable YAML: {error}") from error
    return CityConfig.model_validate(raw)
