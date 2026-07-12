"""import-layer tests against the real Cordoba layer (need a PostGIS server).

The committed ``cities/cordoba/parking.geojson`` is the input on purpose:
these tests complement ``test_cities_parking_layer.py`` (which validates
the file itself) by proving the same file survives the trip into PostGIS.
"""

import json
from pathlib import Path

import pytest
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session
from typer.testing import CliRunner

from shade_core.config import load_city
from shade_core.db import ParkingZone
from shade_pipeline.cli import app
from shade_pipeline.layers import _ewkt, import_parking_layer

REPO_ROOT = Path(__file__).resolve().parents[1]
CORDOBA_YAML = REPO_ROOT / "cities" / "cordoba.yaml"
PARKING_GEOJSON = REPO_ROOT / "cities" / "cordoba" / "parking.geojson"


def test_ewkt_wraps_linestring() -> None:
    ewkt = _ewkt({"type": "LineString", "coordinates": [[-4.78, 37.885], [-4.779, 37.885]]})
    assert ewkt == "SRID=4326;MULTILINESTRING((-4.78 37.885, -4.779 37.885))"


def test_ewkt_rejects_polygons() -> None:
    with pytest.raises(ValueError, match="not supported yet"):
        _ewkt({"type": "Polygon", "coordinates": []})


def test_import_real_layer_is_idempotent(parking_db: Engine) -> None:
    config = load_city(CORDOBA_YAML)
    count = import_parking_layer(config, PARKING_GEOJSON, parking_db)
    count_again = import_parking_layer(config, PARKING_GEOJSON, parking_db)
    assert count == count_again == 21
    with Session(parking_db) as session:
        rows = session.scalars(select(ParkingZone).where(ParkingZone.city_id == "cordoba")).all()
        assert len(rows) == 21
        assert sum(row.capacity or 0 for row in rows) == 1152
        # Spatial sanity: the first vertex of the first feature must find
        # its own zone within a tight radius (meters, thanks to geography).
        first = json.loads(PARKING_GEOJSON.read_text())["features"][0]
        lon, lat = first["geometry"]["coordinates"][0][0]
        point = func.ST_GeogFromText(f"SRID=4326;POINT({lon} {lat})")
        near = session.scalars(
            select(ParkingZone).where(
                ParkingZone.city_id == "cordoba",
                func.ST_DWithin(ParkingZone.geom, point, 25.0),
            )
        ).all()
        assert first["properties"]["name"] in {zone.name for zone in near}


def test_cli_import_layer(parking_db: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(REPO_ROOT)
    url = parking_db.url.render_as_string(hide_password=False)
    result = CliRunner().invoke(app, ["import-layer", "cordoba", "parking", "--database-url", url])
    assert result.exit_code == 0, result.output
    assert "imported 21 parking zones for cordoba" in result.output


def test_cli_rejects_undeclared_layer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(REPO_ROOT)
    result = CliRunner().invoke(
        app, ["import-layer", "cordoba", "trees", "--database-url", "postgresql+psycopg://x/x"]
    )
    assert result.exit_code == 1
    assert "unsupported layer" in result.output
