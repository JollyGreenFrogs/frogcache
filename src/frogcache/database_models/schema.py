"""Database schema: table definitions as SQLAlchemy metadata — the single source
of truth for the frogcache store, imported by alembic/env.py as target_metadata.

Tables: `fetches` (fetch state — replaced manifest.json), `events` (audit log,
ADR-004), `meta` (key/value flags, e.g. legacy import marker). Created by
alembic/versions/0001_initial.py. Lives in database_models/ per the house rule
("DB models live in database_models/"); alembic/ holds only env.py + versions/.

Governing ADRs: docs/decisions/ADR-003 §C, ADR-004.
"""

import sqlalchemy as sa

metadata = sa.MetaData()

fetches = sa.Table(
    "fetches",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("artifact", sa.Text, nullable=False),
    sa.Column("version", sa.Text, nullable=False),
    sa.Column("filename", sa.Text, nullable=False),
    sa.Column("sha256", sa.Text, nullable=False),
    sa.Column("source", sa.Text, server_default="", nullable=False),
    sa.Column("url", sa.Text, server_default="", nullable=False),
    sa.Column("fetched_at", sa.Text, server_default="", nullable=False),
    sa.Column("digest_verified", sa.Boolean, server_default=sa.false(), nullable=False),
    sa.Index("idx_fetches_artifact", "artifact", "version"),
)

events = sa.Table(
    "events",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("ts", sa.Text, nullable=False),
    sa.Column("event", sa.Text, nullable=False),
    sa.Column("artifact", sa.Text, server_default="", nullable=False),
    sa.Column("version", sa.Text, server_default="", nullable=False),
    sa.Column("detail", sa.Text, server_default="{}", nullable=False),
)

meta = sa.Table(
    "meta",
    metadata,
    sa.Column("key", sa.Text, primary_key=True),
    sa.Column("value", sa.Text, nullable=False),
)

FETCHES_COLUMNS = (
    "id, artifact, version, filename, sha256, source, url, fetched_at, digest_verified"
)
