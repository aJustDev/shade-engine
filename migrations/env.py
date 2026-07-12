"""Alembic environment: online-only, URL from SHADE_DATABASE_URL.

Revisions are hand-written, so no model metadata is wired here: with
geoalchemy2 columns, autogenerate emits broken spatial-index DDL unless
patched with helper hooks -- more machinery than a handful of explicit
revisions is worth.
"""

import os

from alembic import context
from sqlalchemy import create_engine

if context.is_offline_mode():
    raise RuntimeError("offline (--sql) migrations are not supported")

url = context.config.get_main_option("sqlalchemy.url") or os.environ.get("SHADE_DATABASE_URL", "")
if not url:
    raise RuntimeError("set SHADE_DATABASE_URL (or sqlalchemy.url) to run migrations")

engine = create_engine(url)
with engine.connect() as connection:
    context.configure(connection=connection, target_metadata=None)
    with context.begin_transaction():
        context.run_migrations()
engine.dispose()
