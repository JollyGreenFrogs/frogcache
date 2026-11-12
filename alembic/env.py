"""Alembic environment for the frogcache store (sync SQLAlchemy + psycopg).

DSN comes from DATABASE_URL (env / secrets file). Table definitions live in
frogcache.database_models.schema (imported here as target_metadata).
Pattern follows frogsense backend/alembic/env.py, minus the multi-tenant
credential-broker session vars.
"""

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine

from frogcache.database_models import schema

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = schema.metadata

# DSN precedence: value set programmatically (db.connect) > DATABASE_URL env
URL = (config.get_main_option("sqlalchemy.url") or os.environ["DATABASE_URL"]).replace("%%", "%")
if URL.startswith("postgresql://"):
    URL = URL.replace("postgresql://", "postgresql+psycopg://", 1)  # SQLAlchemy dialect -> psycopg3


def run_migrations_online() -> None:
    """Run migrations against the live database."""
    connectable = create_engine(URL)
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()
    connectable.dispose()


def run_migrations_offline() -> None:
    """Emit SQL without a live database."""
    context.configure(url=URL, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
