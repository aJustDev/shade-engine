"""Import vector layers from GeoJSON files into PostGIS.

The GeoJSON in the repo stays the editable source of truth
(``cities/<city>/parking.geojson``); this module loads it into the
``parking_zones`` table so the API can answer spatial queries. Re-importing
replaces the city's rows inside one transaction: readers keep seeing the
old rows until commit (MVCC), and running the import twice is idempotent.
"""

import json
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, delete
from sqlalchemy.orm import Session

from shade_core.config import CityConfig
from shade_core.db import ParkingZone


def _ewkt(geometry: dict[str, Any]) -> str:
    """GeoJSON geometry -> EWKT MultiLineString.

    EWKT (``SRID=4326;MULTILINESTRING(...)``) is the text format geography
    columns ingest natively: geoalchemy2 wraps bound values in
    ``ST_GeogFromText``, so a raw GeoJSON string would fail at INSERT time.
    A bare LineString is wrapped into a single-part MultiLineString so the
    table holds one geometry type. Axis order stays lon-lat, same as
    GeoJSON.
    """
    kind = geometry.get("type")
    if kind == "LineString":
        parts = [geometry["coordinates"]]
    elif kind == "MultiLineString":
        parts = geometry["coordinates"]
    else:
        raise ValueError(f"parking geometry {kind!r} not supported yet (spec 5.1 allows Polygon)")
    lines = ", ".join("(" + ", ".join(f"{lon} {lat}" for lon, lat in part) + ")" for part in parts)
    return f"SRID=4326;MULTILINESTRING({lines})"


def import_parking_layer(config: CityConfig, layer_path: Path, engine: Engine) -> int:
    """Replace the city's parking zones with the layer file's features.

    Required feature properties fail loudly (KeyError) instead of inserting
    half-described zones; optional ones (spec 5.1) default to NULL.
    Returns the number of zones imported.
    """
    collection = json.loads(layer_path.read_text(encoding="utf-8"))
    if collection.get("type") != "FeatureCollection":
        raise ValueError(f"{layer_path} is not a GeoJSON FeatureCollection")
    zones = [
        ParkingZone(
            city_id=config.id,
            name=feature["properties"]["name"],
            zone_type=feature["properties"]["zone_type"],
            orientation=feature["properties"].get("orientation"),
            capacity=feature["properties"].get("capacity"),
            schedule=feature["properties"]["schedule"],
            max_minutes=feature["properties"].get("max_minutes"),
            tariff_eur_hour=feature["properties"].get("tariff_eur_hour"),
            notes=feature["properties"].get("notes"),
            source=feature["properties"].get("source"),
            last_verified=feature["properties"].get("last_verified"),
            geom=_ewkt(feature["geometry"]),
        )
        for feature in collection["features"]
    ]
    with Session(engine) as session:
        session.execute(delete(ParkingZone).where(ParkingZone.city_id == config.id))
        session.add_all(zones)
        session.commit()
    return len(zones)
