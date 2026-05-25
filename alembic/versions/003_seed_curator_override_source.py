"""Seed the CuratorOverride internal source required by ResolveConflict.

Revision ID: 003
Revises: 002
"""

import sqlalchemy as sa
from alembic import op

revision = "003"
down_revision = "002"
branch_labels = None
depends_on = None

CURATOR_OVERRIDE_SOURCE_ID = "00000000-0000-0000-0000-000000000001"


def upgrade() -> None:
    op.execute(
        sa.text(
            """
            INSERT INTO sources (id, name, kind, reliability_tier, created_at)
            VALUES (:id, 'CuratorOverride', 'INTERNAL', 1, now())
            ON CONFLICT (id) DO NOTHING
            """
        ).bindparams(id=CURATOR_OVERRIDE_SOURCE_ID)
    )


def downgrade() -> None:
    op.execute(
        sa.text("DELETE FROM sources WHERE id = :id").bindparams(id=CURATOR_OVERRIDE_SOURCE_ID)
    )
