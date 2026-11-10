"""Initial schema: fetches, events, meta.

Revision ID: 0001
Revises:
Create Date: 2026-11-09

Creates the frogcache store tables from database_models.schema metadata.
Plain public schema — frogcache is a single-tenant LAN service, so no
per-tenant schemas/roles/RLS (unlike multi-tenant frogsense).
"""

import sqlalchemy as sa
from alembic import op

from frogcache.database_models import schema

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Create the frogcache tables from the schema metadata."""
    schema.metadata.create_all(op.get_bind())


def downgrade() -> None:
    """Drop all frogcache tables."""
    schema.metadata.drop_all(op.get_bind())
