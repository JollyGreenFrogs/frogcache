"""Database connection for the frogcache store (Postgres on the LAN).

`connect()` runs `alembic upgrade head` (schema versioning is owned by
Alembic — table definitions live in database_models/schema.py) and returns a
psycopg connection with dict rows. DSN: DATABASE_URL env (secrets file).

Governing ADRs: docs/decisions/ADR-003 §C, ADR-004.
"""

import os
from pathlib import Path

import psycopg
from alembic import command
from alembic.config import Config
from psycopg.rows import dict_row


def connect(dsn: str | None = None):
    """Apply migrations and open a connection to the frogcache Postgres DB.

    Args:
        dsn: Postgres DSN; defaults to the DATABASE_URL env var.
    Returns:
        psycopg connection with dict rows.
    Raises:
        RuntimeError: if DATABASE_URL is not set.
    """
    dsn = dsn or os.environ.get("DATABASE_URL", "")
    if not dsn:
        raise RuntimeError("DATABASE_URL not set (secrets file / env)")
    here = Path(__file__).resolve()
    # repo checkout: <root>/alembic.ini; installed wheel: site-packages/alembic.ini
    # (packaged via hatch force-include, see pyproject.toml)
    candidates = (here.parents[2] / "alembic.ini", here.parents[1] / "alembic.ini")
    ini = next((p for p in candidates if p.is_file()), None)
    if ini is None:
        raise RuntimeError("alembic.ini not found (repo root or package install)")
    cfg = Config(str(ini))
    # SQLAlchemy 2.0 defaults to psycopg2 for postgresql:// — select psycopg3.
    # Escape % for ConfigParser interpolation; psycopg gets the original DSN.
    sa_dsn = dsn.replace("postgresql://", "postgresql+psycopg://", 1)
    cfg.set_main_option("sqlalchemy.url", sa_dsn.replace("%", "%%"))
    # serialize concurrent migrators (updater + fetch threads) with a PG advisory lock
    lock_conn = psycopg.connect(dsn)
    lock_conn.execute("SELECT pg_advisory_lock(hashtext('frogcache_migrations'))")
    try:
        command.upgrade(cfg, "head")
    finally:
        lock_conn.execute("SELECT pg_advisory_unlock(hashtext('frogcache_migrations'))")
        lock_conn.close()
    conn = psycopg.connect(dsn, row_factory=dict_row)
    return conn
