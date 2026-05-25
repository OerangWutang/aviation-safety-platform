"""Add explicit action to claim history.

Revision ID: 004
Revises: 003
"""

import sqlalchemy as sa
from alembic import op

revision = "004"
down_revision = "003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "claim_history",
        sa.Column("action", sa.String(50), nullable=False, server_default="updated"),
    )


def downgrade() -> None:
    op.drop_column("claim_history", "action")
