"""Create the parking_zones table.

Revision ID: 0001
Revises:
Create Date: 2026-07-12
"""

import sqlalchemy as sa
from alembic import op
from geoalchemy2 import Geography
from sqlalchemy.dialects import postgresql

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # The postgis/postgis image ships the extension preinstalled; IF NOT
    # EXISTS keeps this runnable on databases created outside that image
    # (requires a superuser or the postgis extension being trusted).
    op.execute("CREATE EXTENSION IF NOT EXISTS postgis")
    op.create_table(
        "parking_zones",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("city_id", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("zone_type", sa.String(), nullable=False),
        sa.Column("orientation", sa.String(), nullable=True),
        sa.Column("capacity", sa.Integer(), nullable=True),
        sa.Column("schedule", postgresql.JSONB(), nullable=False),
        sa.Column("max_minutes", sa.Integer(), nullable=True),
        sa.Column("tariff_eur_hour", sa.Double(), nullable=True),
        sa.Column("notes", sa.String(), nullable=True),
        sa.Column("source", sa.String(), nullable=True),
        sa.Column("last_verified", sa.String(), nullable=True),
        sa.Column(
            "geom",
            Geography(geometry_type="MULTILINESTRING", srid=4326, spatial_index=False),
            nullable=False,
        ),
    )
    op.create_index("ix_parking_zones_city_id", "parking_zones", ["city_id"])
    # GiST: bounding-box index that makes ST_DWithin cheap (see shade_core.db).
    op.create_index("ix_parking_zones_geom", "parking_zones", ["geom"], postgresql_using="gist")


def downgrade() -> None:
    # Indexes fall with the table. Never drop the postgis extension: other
    # databases or tables may rely on it.
    op.drop_table("parking_zones")
