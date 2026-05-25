"""Ingestion identity: source_record_id and pending duplicate reviews.

Changes
-------
1. ``raw_snapshots.source_record_id VARCHAR(255) NULL``
   Stable identifier assigned by the source system (e.g. NTSB accession number,
   IATA incident ID).  When provided, enables the ingestion pipeline to detect
   re-submissions of updated data for the same record and to route them to the
   original event rather than creating a new one.

   Indexed by ``(source_id, source_record_id) WHERE source_record_id IS NOT NULL``
   so the lookup is efficient.  NULL for sources that don't supply stable IDs.

2. ``pending_duplicate_reviews`` table
   Tracks (event_id_a, event_id_b) pairs where the event-matching service
   produced a medium-confidence score (0.40-0.75).  Curators resolve each
   review as CONFIRMED (-> triggers merge; stored as MERGED status) or REJECTED
   (distinct accidents).  High-confidence auto-merges are also recorded here
   with status=AUTO_MERGED for audit purposes.

   Review lifecycle (current):
     PENDING  -> MERGED   (curator confirms duplicate, merge executes)
     PENDING  -> REJECTED (curator rejects, events are distinct)

   Note: an earlier design used CONFIRMED_DUPLICATE as the confirm terminal
   state.  The current implementation goes directly to MERGED when the curator
   confirms, because the merge always executes atomically in the same call.
   The CONFIRMED_DUPLICATE value is kept in the enum for backwards-compatibility
   with any already-stored rows, but new confirmations write MERGED.

Rollback
--------
down() drops the review table and the source_record_id column.  Existing
snapshots all have NULL for source_record_id so no data is lost on downgrade.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "009"
down_revision = "008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Add source_record_id to raw_snapshots
    op.add_column(
        "raw_snapshots",
        sa.Column("source_record_id", sa.String(255), nullable=True),
    )
    op.create_index(
        "ix_raw_snapshot_source_record",
        "raw_snapshots",
        ["source_id", "source_record_id"],
        unique=False,
        postgresql_where=sa.text("source_record_id IS NOT NULL"),
    )

    # 2. Create pending_duplicate_reviews table
    op.create_table(
        "pending_duplicate_reviews",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "event_id_a",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("accident_events.id"),
            nullable=False,
        ),
        sa.Column(
            "event_id_b",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("accident_events.id"),
            nullable=False,
        ),
        sa.Column("status", sa.String(50), nullable=False, server_default="PENDING"),
        sa.Column("match_score", sa.Float, nullable=False),
        sa.Column(
            "matched_fields", sa.dialects.postgresql.JSONB, nullable=False, server_default="[]"
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_by", sa.dialects.postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("resolution_note", sa.String(500), nullable=True),
    )
    op.create_index(
        "ix_pending_dup_review_event_a",
        "pending_duplicate_reviews",
        ["event_id_a"],
    )
    op.create_index(
        "ix_pending_dup_review_event_b",
        "pending_duplicate_reviews",
        ["event_id_b"],
    )
    op.create_index(
        "ix_pending_dup_review_status",
        "pending_duplicate_reviews",
        ["status"],
        postgresql_where=sa.text("status = 'PENDING'"),
    )


def downgrade() -> None:
    op.drop_table("pending_duplicate_reviews")
    op.drop_index("ix_raw_snapshot_source_record", table_name="raw_snapshots")
    op.drop_column("raw_snapshots", "source_record_id")
