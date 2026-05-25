"""uppercase outbox status values

Revision ID: 007
Revises: 006
"""

import sqlalchemy as sa
from alembic import op

revision = "007"
down_revision = "006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("UPDATE outbox_events SET status = UPPER(status)")
    op.alter_column(
        "outbox_events",
        "status",
        existing_type=sa.String(length=50),
        server_default="PENDING",
        existing_nullable=False,
    )


def downgrade() -> None:
    op.execute("UPDATE outbox_events SET status = LOWER(status)")
    op.alter_column(
        "outbox_events",
        "status",
        existing_type=sa.String(length=50),
        server_default="pending",
        existing_nullable=False,
    )
