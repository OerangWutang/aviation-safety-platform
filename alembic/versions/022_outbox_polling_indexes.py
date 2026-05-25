"""Add outbox polling indexes.

Revision ID: 022
Revises: 021

Outbox workers poll PENDING rows ordered by created_at/id and retry FAILED rows
whose next_attempt_at is due. Partial indexes keep these hot-path scans small
even when the historical outbox table grows. A PROCESSING/locked_at index makes
stale-lock sweeps bounded too.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "022"
down_revision = "021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_outbox_events_pending_created",
        "outbox_events",
        ["created_at", "id"],
        postgresql_where=sa.text("status = 'PENDING'"),
    )
    op.execute(
        """
        CREATE INDEX ix_outbox_events_failed_retry_created
        ON outbox_events (next_attempt_at ASC NULLS FIRST, created_at, id)
        WHERE status = 'FAILED'
        """
    )
    op.create_index(
        "ix_outbox_events_processing_locked",
        "outbox_events",
        ["locked_at", "id"],
        postgresql_where=sa.text("status = 'PROCESSING'"),
    )


def downgrade() -> None:
    op.drop_index("ix_outbox_events_processing_locked", table_name="outbox_events")
    op.drop_index("ix_outbox_events_failed_retry_created", table_name="outbox_events")
    op.drop_index("ix_outbox_events_pending_created", table_name="outbox_events")
