"""Chronos v0.1 timeline/event sequencing engine tables.

Revision ID: 029
Revises: 028
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

revision = "029"
down_revision = "028"
branch_labels = None
depends_on = None

_UUID = PG_UUID(as_uuid=True)

_EVENT_TYPES = (
    "SCHEDULED_DEPARTURE",
    "ACTUAL_DEPARTURE",
    "TAKEOFF",
    "LAST_CONTACT",
    "EMERGENCY_DECLARED",
    "IMPACT",
    "LANDING",
    "RESCUE_STARTED",
    "INVESTIGATION_OPENED",
    "REPORT_PUBLISHED",
)

_PRECISIONS = ("EXACT", "MINUTE", "HOUR", "DAY", "APPROXIMATE", "RELATIVE", "UNKNOWN")
_REVIEW_STATUSES = ("PENDING", "CONFIRMED", "REJECTED", "AUTO_CONFIRMED")


def upgrade() -> None:
    op.create_table(
        "chronos_timeline_events",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column("accident_event_id", _UUID, nullable=False),
        sa.Column("event_type", sa.String(50), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("timestamp_precision", sa.String(20), nullable=False),
        sa.Column("sequence_index", sa.Integer, nullable=True),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("raw_value", sa.Text, nullable=True),
        sa.Column("confidence", sa.Float, nullable=False, server_default="1.0"),
        sa.Column("source_claim_id", _UUID, nullable=True),
        sa.Column("raw_snapshot_id", _UUID, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["accident_event_id"], ["accident_events.id"]),
        sa.ForeignKeyConstraint(["source_claim_id"], ["claims.id"]),
        sa.ForeignKeyConstraint(["raw_snapshot_id"], ["raw_snapshots.id"]),
        sa.CheckConstraint(
            f"event_type IN ({', '.join(repr(t) for t in _EVENT_TYPES)})",
            name="ck_chronos_timeline_events_event_type",
        ),
        sa.CheckConstraint(
            f"timestamp_precision IN ({', '.join(repr(p) for p in _PRECISIONS)})",
            name="ck_chronos_timeline_events_precision",
        ),
    )
    op.create_index(
        "ix_chronos_timeline_events_accident_event_id",
        "chronos_timeline_events",
        ["accident_event_id"],
    )
    op.create_index(
        "ix_chronos_timeline_events_event_type", "chronos_timeline_events", ["event_type"]
    )
    op.create_index(
        "ix_chronos_timeline_events_occurred_at", "chronos_timeline_events", ["occurred_at"]
    )
    op.create_index(
        "uq_chronos_timeline_events_idempotent",
        "chronos_timeline_events",
        ["accident_event_id", "event_type", "raw_value"],
        unique=True,
    )

    op.create_table(
        "chronos_event_links",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column("accident_event_id", _UUID, nullable=False),
        sa.Column("predecessor_event_id", _UUID, nullable=False),
        sa.Column("successor_event_id", _UUID, nullable=False),
        sa.Column("relationship_type", sa.String(100), nullable=False),
        sa.Column("confidence", sa.Float, nullable=False, server_default="1.0"),
        sa.Column("source_claim_id", _UUID, nullable=True),
        sa.Column("raw_snapshot_id", _UUID, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["accident_event_id"], ["accident_events.id"]),
        sa.ForeignKeyConstraint(["predecessor_event_id"], ["chronos_timeline_events.id"]),
        sa.ForeignKeyConstraint(["successor_event_id"], ["chronos_timeline_events.id"]),
        sa.ForeignKeyConstraint(["source_claim_id"], ["claims.id"]),
        sa.ForeignKeyConstraint(["raw_snapshot_id"], ["raw_snapshots.id"]),
        sa.CheckConstraint(
            "predecessor_event_id != successor_event_id", name="ck_chronos_event_links_no_self_link"
        ),
    )
    op.create_index(
        "ix_chronos_event_links_accident_event_id", "chronos_event_links", ["accident_event_id"]
    )
    op.create_index(
        "ix_chronos_event_links_predecessor_event_id",
        "chronos_event_links",
        ["predecessor_event_id"],
    )
    op.create_index(
        "ix_chronos_event_links_successor_event_id", "chronos_event_links", ["successor_event_id"]
    )
    op.create_index(
        "uq_chronos_event_links_pair",
        "chronos_event_links",
        ["accident_event_id", "predecessor_event_id", "successor_event_id", "relationship_type"],
        unique=True,
    )

    op.create_table(
        "chronos_sequence_reviews",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column("accident_event_id", _UUID, nullable=False),
        sa.Column("timeline_event_id_a", _UUID, nullable=False),
        sa.Column("timeline_event_id_b", _UUID, nullable=False),
        sa.Column("reason", sa.Text, nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="PENDING"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_by", _UUID, nullable=True),
        sa.Column("resolution_note", sa.Text, nullable=True),
        sa.ForeignKeyConstraint(["accident_event_id"], ["accident_events.id"]),
        sa.ForeignKeyConstraint(["timeline_event_id_a"], ["chronos_timeline_events.id"]),
        sa.ForeignKeyConstraint(["timeline_event_id_b"], ["chronos_timeline_events.id"]),
        sa.CheckConstraint(
            f"status IN ({', '.join(repr(s) for s in _REVIEW_STATUSES)})",
            name="ck_chronos_sequence_reviews_status",
        ),
        sa.CheckConstraint(
            "timeline_event_id_a != timeline_event_id_b",
            name="ck_chronos_sequence_reviews_no_self_pair",
        ),
    )
    op.create_index(
        "ix_chronos_sequence_reviews_accident_event_id",
        "chronos_sequence_reviews",
        ["accident_event_id"],
    )
    op.create_index("ix_chronos_sequence_reviews_status", "chronos_sequence_reviews", ["status"])
    op.execute(
        """
        CREATE UNIQUE INDEX uq_chronos_sequence_reviews_pending_pair
        ON chronos_sequence_reviews (
            LEAST(timeline_event_id_a::text, timeline_event_id_b::text),
            GREATEST(timeline_event_id_a::text, timeline_event_id_b::text)
        )
        WHERE status = 'PENDING'
        """
    )


def downgrade() -> None:
    op.drop_index("uq_chronos_sequence_reviews_pending_pair", table_name="chronos_sequence_reviews")
    op.drop_table("chronos_sequence_reviews")
    op.drop_table("chronos_event_links")
    op.drop_table("chronos_timeline_events")
