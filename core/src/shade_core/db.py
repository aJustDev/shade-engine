"""Vector layer persistence: PostGIS models shared by the CLI and the API.

Rasters never live in Postgres (repo rule): the shade engine reads COG
artifacts from disk. PostGIS stores the small, editable vector layers --
parking zones today -- so the API can answer spatial queries ("zones within
300 m of here") transactionally.

Why ``geography`` and not ``geometry``
--------------------------------------
PostGIS has two spatial column families:

- ``geometry`` computes on a flat plane in the units of its SRID. With
  lon/lat data (SRID 4326) that means DEGREES: ``ST_DWithin(geom, p, 300)``
  would filter by 300 degrees, not meters -- the classic "never measure
  distances in degrees" trap.
- ``geography`` computes on the WGS84 ellipsoid and takes METERS. Slower
  and with fewer functions, but the nearby query needs exactly what it
  offers: ``ST_DWithin(geog, point, radius_m)`` and ``ST_Distance`` in
  meters.

One table serves every city. A ``geometry`` column pinned to a local
projected CRS (EPSG:25830 for Cordoba) would measure meters too, but could
not host a second city in a different UTM zone; ``geography`` in 4326 can.
The shade engine keeps doing its math in the city CRS -- this column only
answers "which zones are near this point".

The GiST index in ``__table_args__`` makes ``ST_DWithin`` cheap: a B-tree
orders scalars and cannot index 2D extents, GiST indexes bounding boxes
(coarse filter) which PostGIS refines with the exact predicate. It is
declared explicitly instead of geoalchemy2's ``spatial_index=True`` because
the implicit one duplicates index DDL under Alembic migrations.
"""

from geoalchemy2 import Geography, WKBElement
from sqlalchemy import Engine, Index, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Declarative base for every shade-engine table."""


class ParkingZone(Base):
    """One regulated parking zone: street segments sharing attributes.

    Mirrors the per-feature schema of ``cities/<city>/parking.geojson``
    (spec section 5.1). The geometry is a MultiLineString so group
    attributes such as ``capacity`` exist once per zone and are never
    double-counted per segment.
    """

    __tablename__ = "parking_zones"
    __table_args__ = (Index("ix_parking_zones_geom", "geom", postgresql_using="gist"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    city_id: Mapped[str] = mapped_column(index=True)
    name: Mapped[str]
    zone_type: Mapped[str]
    orientation: Mapped[str | None]
    capacity: Mapped[int | None]
    schedule: Mapped[list[dict[str, str]]] = mapped_column(JSONB)
    max_minutes: Mapped[int | None]
    tariff_eur_hour: Mapped[float | None]
    notes: Mapped[str | None]
    source: Mapped[str | None]
    # Provenance text from the layer file; not guaranteed to parse as a date.
    last_verified: Mapped[str | None]
    geom: Mapped[WKBElement] = mapped_column(
        Geography(geometry_type="MULTILINESTRING", srid=4326, spatial_index=False)
    )


def make_engine(url: str) -> Engine:
    """Engine for the shared PostGIS instance.

    ``pool_pre_ping`` revalidates pooled connections before each use so a
    long-lived process (the API) survives a database restart without
    serving stale-connection errors.
    """
    return create_engine(url, pool_pre_ping=True)
