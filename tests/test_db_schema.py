"""PostGIS schema smoke tests (skipped without a reachable server).

The ``parking_db`` fixture applies the Alembic migrations to a scratch
database, so passing here also proves ``alembic upgrade head`` runs.
"""

import json

from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from shade_core.db import ParkingZone

# EWKT is lon-lat order, like GeoJSON. ~88 m of east-west street.
ZONE_EWKT = "SRID=4326;MULTILINESTRING((-4.7800 37.8850, -4.7790 37.8850))"
# ~11 m north of the segment (0.0001 deg of latitude).
NEAR_POINT = "SRID=4326;POINT(-4.7795 37.8851)"


def _zone(city_id: str) -> ParkingZone:
    return ParkingZone(
        city_id=city_id,
        name="TEST STREET",
        zone_type="blue",
        orientation="cordon",
        capacity=10,
        schedule=[{"days": "mo-fr", "from": "09:00", "to": "14:00"}],
        max_minutes=120,
        tariff_eur_hour=0.9,
        notes=None,
        source="synthetic",
        last_verified="2026-07-12",
        geom=ZONE_EWKT,
    )


def test_dwithin_filters_in_meters(parking_db: Engine) -> None:
    with Session(parking_db) as session:
        session.add(_zone("dwithin_city"))
        session.commit()
        point = func.ST_GeogFromText(NEAR_POINT)
        near = session.scalars(
            select(ParkingZone).where(
                ParkingZone.city_id == "dwithin_city",
                func.ST_DWithin(ParkingZone.geom, point, 50.0),
            )
        ).all()
        assert [zone.name for zone in near] == ["TEST STREET"]
        # The point sits ~11 m away: a 5 m radius must exclude it. Were the
        # column plain geometry in 4326, DWithin would filter by DEGREES and
        # 5 "meters" would swallow the whole city.
        far = session.scalars(
            select(ParkingZone).where(
                ParkingZone.city_id == "dwithin_city",
                func.ST_DWithin(ParkingZone.geom, point, 5.0),
            )
        ).all()
        assert far == []


def test_geojson_roundtrip(parking_db: Engine) -> None:
    with Session(parking_db) as session:
        session.add(_zone("roundtrip_city"))
        session.commit()
        geojson = session.scalar(
            select(func.ST_AsGeoJSON(ParkingZone.geom)).where(
                ParkingZone.city_id == "roundtrip_city"
            )
        )
        assert geojson is not None
        shape = json.loads(geojson)
        assert shape["type"] == "MultiLineString"
        assert shape["coordinates"][0][0] == [-4.78, 37.885]
        row = session.scalars(
            select(ParkingZone).where(ParkingZone.city_id == "roundtrip_city")
        ).one()
        assert row.schedule == [{"days": "mo-fr", "from": "09:00", "to": "14:00"}]
