"""Add unordered uniqueness for pending duplicate reviews.

Revision ID: 023
Revises: 022
Create Date: 2026-05-13
"""

from __future__ import annotations

from alembic import op

# revision identifiers, used by Alembic.
revision = "023"
down_revision = "022"
branch_labels = None
depends_on = None


UNIQUE_INDEX_NAME = "uq_pending_duplicate_reviews_pending_pair"


def upgrade() -> None:
    # Existing databases might already contain duplicate PENDING review tasks
    # for the same unordered event pair. Keep the oldest task as the curator's
    # queue item and mark the rest rejected so the unique partial index can be
    # created without failing the migration.
    op.execute(
        """
        WITH ranked AS (
            SELECT
                id,
                ROW_NUMBER() OVER (
                    PARTITION BY LEAST(event_id_a, event_id_b),
                                 GREATEST(event_id_a, event_id_b)
                    ORDER BY created_at ASC, id ASC
                ) AS rn
            FROM pending_duplicate_reviews
            WHERE status = 'PENDING'
        )
        UPDATE pending_duplicate_reviews AS review
        SET status = 'REJECTED',
            resolved_at = NOW(),
            resolution_note = CASE
                WHEN review.resolution_note IS NULL OR review.resolution_note = ''
                THEN 'Auto-rejected duplicate pending review during migration 023.'
                ELSE review.resolution_note || ' Auto-rejected duplicate pending review during migration 023.'
            END
        FROM ranked
        WHERE review.id = ranked.id
          AND ranked.rn > 1
        """
    )
    op.execute(
        f"""
        CREATE UNIQUE INDEX {UNIQUE_INDEX_NAME}
        ON pending_duplicate_reviews (
            LEAST(event_id_a, event_id_b),
            GREATEST(event_id_a, event_id_b)
        )
        WHERE status = 'PENDING'
        """
    )


def downgrade() -> None:
    op.execute(f"DROP INDEX IF EXISTS {UNIQUE_INDEX_NAME}")
