"""add conflict modified note and normalize source tier

Revision ID: 006
Revises: 005
"""

import sqlalchemy as sa
from alembic import op

revision = "006"
down_revision = "005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "claim_conflicts",
        sa.Column("last_modified_note", sa.String(length=255), nullable=True),
    )
    op.execute(
        sa.text(
            """
            UPDATE sources
            SET reliability_tier = 1
            WHERE reliability_tier < 1
            """
        )
    )


def downgrade() -> None:
    op.drop_column("claim_conflicts", "last_modified_note")
