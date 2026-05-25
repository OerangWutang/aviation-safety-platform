"""Release hygiene indexes and outbox worker heartbeats.

Revision ID: 025
Revises: 024

Adds a tiny heartbeat table so API metrics can distinguish a growing outbox
backlog from a worker that is not looping. Also aligns the FAILED retry polling
index with the query's NULLS FIRST ordering and adds a partial index for the
oldest-unprocessed-outbox metric.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "025"
down_revision = "024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_outbox_events_failed_retry_created")
    op.execute(
        """
        CREATE INDEX ix_outbox_events_failed_retry_created
        ON outbox_events (next_attempt_at ASC NULLS FIRST, created_at, id)
        WHERE status = 'FAILED'
        """
    )
    op.create_index(
        "ix_outbox_events_unprocessed_created",
        "outbox_events",
        ["created_at", "id"],
        postgresql_where=sa.text("status IN ('PENDING', 'PROCESSING', 'FAILED')"),
    )
    op.create_table(
        "outbox_worker_heartbeats",
        sa.Column("worker_id", sa.String(length=255), primary_key=True, nullable=False),
        sa.Column("last_loop_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_successful_batch_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_outbox_worker_heartbeats_last_loop",
        "outbox_worker_heartbeats",
        ["last_loop_at"],
    )
    op.create_index(
        "ix_outbox_worker_heartbeats_last_success",
        "outbox_worker_heartbeats",
        ["last_successful_batch_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_outbox_worker_heartbeats_last_success", table_name="outbox_worker_heartbeats")
    op.drop_index("ix_outbox_worker_heartbeats_last_loop", table_name="outbox_worker_heartbeats")
    op.drop_table("outbox_worker_heartbeats")
    op.drop_index("ix_outbox_events_unprocessed_created", table_name="outbox_events")
    op.execute("DROP INDEX IF EXISTS ix_outbox_events_failed_retry_created")
    # op.create_index cannot express NULLS FIRST; raw SQL preserves the exact
    # pre-025 index definition that the FAILED-retry query relies on.
    op.execute(
        """
        CREATE INDEX ix_outbox_events_failed_retry_created
        ON outbox_events (next_attempt_at ASC NULLS FIRST, created_at, id)
        WHERE status = 'FAILED'
        """
    )
