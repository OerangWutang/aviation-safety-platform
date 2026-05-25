"""source-specific field mapping configuration

Persist per-source raw-field -> canonical-field mapping so ingestion workers
normalise the same feed consistently across restarts and processes.

Revision ID: 017
Revises: 016
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "017"
down_revision = "016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "sources",
        sa.Column(
            "field_mapping_json",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )


def downgrade() -> None:
    op.drop_column("sources", "field_mapping_json")
