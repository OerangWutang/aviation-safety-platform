"""Add concurrency-safety constraints and outbox retry support.

Changes:
  1. Partial unique index on ``claim_conflicts (event_id, field_name) WHERE status='OPEN'``
     - prevents duplicate OPEN conflicts under concurrent ingestion. Application-level
     checks (find_open_by_event_field) are not safe under concurrent writers.

  2. ``outbox_events.next_attempt_at`` - enables exponential-backoff retry:
     ``fetch_and_lock_pending`` now also picks up FAILED events whose
     ``next_attempt_at <= now()`` and whose ``attempt_count < max_attempts``.
     Events that exhaust all attempts move to DEAD_LETTER.

Revision ID: 008
Revises: 007
"""

import sqlalchemy as sa
from alembic import op

revision = "008"
down_revision = "007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Partial unique index for open conflicts.
    #    The WHERE clause limits the uniqueness constraint to only OPEN rows so
    #    the same (event_id, field_name) pair can appear multiple times as
    #    resolved/reopened history, but never twice as OPEN simultaneously.
    op.create_index(
        "uq_open_conflict_event_field",
        "claim_conflicts",
        ["event_id", "field_name"],
        unique=True,
        postgresql_where=sa.text("status = 'OPEN'"),
    )

    # 2. Retry timestamp on outbox events.
    #    NULL means "immediately eligible" (used for first-time PENDING events).
    op.add_column(
        "outbox_events",
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("outbox_events", "next_attempt_at")
    op.drop_index("uq_open_conflict_event_field", table_name="claim_conflicts")
