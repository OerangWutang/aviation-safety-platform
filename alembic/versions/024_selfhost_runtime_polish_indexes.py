"""Add pending review pagination index and drop redundant conflict indexes.

Revision ID: 024
Revises: 023
Create Date: 2026-05-13
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "024"
down_revision = "023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_pending_duplicate_reviews_pending_created_id",
        "pending_duplicate_reviews",
        [sa.text("created_at DESC"), sa.text("id DESC")],
        postgresql_where=sa.text("status = 'PENDING'"),
    )

    # These single-column indexes are covered by unique B-trees whose leading
    # columns are the same:
    #   uq_conflict_claim(conflict_id, claim_id)
    #   uq_conflict_activity_sequence(conflict_id, sequence)
    # Dropping them reduces write amplification on hot conflict/audit paths.
    # Use IF EXISTS because earlier migration revisions created the redundant
    # indexes via ORM metadata in some deployed databases, but the canonical
    # migration history for a fresh database may never have created them.
    # Plain op.drop_index would make `alembic upgrade head` fail from scratch.
    op.execute("DROP INDEX IF EXISTS ix_claim_conflict_claims_conflict_id")
    op.execute("DROP INDEX IF EXISTS ix_conflict_activity_log_conflict_id")


def downgrade() -> None:
    op.create_index(
        "ix_conflict_activity_log_conflict_id",
        "conflict_activity_log",
        ["conflict_id"],
    )
    op.create_index(
        "ix_claim_conflict_claims_conflict_id",
        "claim_conflict_claims",
        ["conflict_id"],
    )
    op.drop_index(
        "ix_pending_duplicate_reviews_pending_created_id",
        table_name="pending_duplicate_reviews",
    )
