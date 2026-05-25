"""Argus v0.1 — Signal Detection Engine tables.

Revision ID: 031
Revises: 030
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

revision = "031"
down_revision = "030"
branch_labels = None
depends_on = None

_UUID = PG_UUID(as_uuid=True)

_SIGNAL_TYPES = (
    "NEW_SOURCE_CHANGE",
    "TIMELINE_SEQUENCE_CONFLICT",
    "HIGH_CONFLICT_ACCIDENT_RECORD",
    "REPEATED_AIRCRAFT_INVOLVEMENT",
    "REPEATED_OPERATOR_INVOLVEMENT",
    "SOURCE_FETCH_FAILURE_SPIKE",
)
_STATUSES = ("OPEN", "CONFIRMED", "DISMISSED", "NEEDS_MORE_REVIEW", "AUTO_RESOLVED")
_SEVERITIES = ("LOW", "MEDIUM", "HIGH", "CRITICAL")
_EVIDENCE_TYPES = (
    "ATLAS_CLAIM",
    "ATLAS_CONFLICT",
    "ATLAS_ACCIDENT_EVENT",
    "ORION_ENTITY",
    "ORION_RELATIONSHIP",
    "CHRONOS_TIMELINE_EVENT",
    "CHRONOS_SEQUENCE_REVIEW",
    "HERMES_SOURCE_CHANGE",
    "HERMES_FETCH_JOB",
    "HERMES_FETCHED_DOCUMENT",
)
_DECISIONS = ("CONFIRMED", "DISMISSED", "NEEDS_MORE_REVIEW")


def upgrade() -> None:
    op.create_table(
        "argus_signals",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column("signal_type", sa.String(60), nullable=False),
        sa.Column("status", sa.String(30), nullable=False, server_default="OPEN"),
        sa.Column("severity", sa.String(20), nullable=False),
        sa.Column("confidence", sa.Float, nullable=False),
        sa.Column("title", sa.Text, nullable=False),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("accident_event_id", _UUID, sa.ForeignKey("accident_events.id"), nullable=True),
        sa.Column("primary_entity_id", _UUID, nullable=True),
        sa.Column("source_engine", sa.String(50), nullable=False),
        sa.Column("dedupe_key", sa.Text, nullable=False),
        sa.Column("first_detected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_detected_at", sa.DateTime(timezone=True), nullable=False),
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
        sa.CheckConstraint(
            f"signal_type IN ({', '.join(repr(v) for v in _SIGNAL_TYPES)})",
            name="ck_argus_signals_signal_type",
        ),
        sa.CheckConstraint(
            f"status IN ({', '.join(repr(v) for v in _STATUSES)})",
            name="ck_argus_signals_status",
        ),
        sa.CheckConstraint(
            f"severity IN ({', '.join(repr(v) for v in _SEVERITIES)})",
            name="ck_argus_signals_severity",
        ),
        sa.CheckConstraint(
            "confidence >= 0 AND confidence <= 1", name="ck_argus_signals_confidence"
        ),
    )
    op.create_index("uq_argus_signals_dedupe_key", "argus_signals", ["dedupe_key"], unique=True)
    op.create_index("ix_argus_signals_signal_type", "argus_signals", ["signal_type"])
    op.create_index("ix_argus_signals_status", "argus_signals", ["status"])
    op.create_index("ix_argus_signals_severity", "argus_signals", ["severity"])
    op.create_index("ix_argus_signals_accident_event_id", "argus_signals", ["accident_event_id"])
    op.create_index("ix_argus_signals_primary_entity_id", "argus_signals", ["primary_entity_id"])
    op.create_index("ix_argus_signals_first_detected_at", "argus_signals", ["first_detected_at"])
    op.create_index("ix_argus_signals_last_detected_at", "argus_signals", ["last_detected_at"])

    op.create_table(
        "argus_signal_evidence",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column("signal_id", _UUID, sa.ForeignKey("argus_signals.id"), nullable=False),
        sa.Column("evidence_type", sa.String(40), nullable=False),
        sa.Column("evidence_id", _UUID, nullable=False),
        sa.Column("engine", sa.String(50), nullable=False),
        sa.Column("summary", sa.Text, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            f"evidence_type IN ({', '.join(repr(v) for v in _EVIDENCE_TYPES)})",
            name="ck_argus_signal_evidence_type",
        ),
        sa.UniqueConstraint(
            "signal_id", "evidence_type", "evidence_id", name="uq_argus_signal_evidence_link"
        ),
    )
    op.create_index("ix_argus_signal_evidence_signal_id", "argus_signal_evidence", ["signal_id"])
    op.create_index(
        "ix_argus_signal_evidence_evidence_type", "argus_signal_evidence", ["evidence_type"]
    )
    op.create_index(
        "ix_argus_signal_evidence_evidence_id", "argus_signal_evidence", ["evidence_id"]
    )
    op.create_index("ix_argus_signal_evidence_engine", "argus_signal_evidence", ["engine"])

    op.create_table(
        "argus_signal_reviews",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column("signal_id", _UUID, sa.ForeignKey("argus_signals.id"), nullable=False),
        sa.Column("decision", sa.String(30), nullable=False),
        sa.Column("reviewer_id", _UUID, nullable=True),
        sa.Column("note", sa.Text, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            f"decision IN ({', '.join(repr(v) for v in _DECISIONS)})",
            name="ck_argus_signal_reviews_decision",
        ),
    )
    op.create_index("ix_argus_signal_reviews_signal_id", "argus_signal_reviews", ["signal_id"])
    op.create_index("ix_argus_signal_reviews_decision", "argus_signal_reviews", ["decision"])
    op.create_index("ix_argus_signal_reviews_reviewer_id", "argus_signal_reviews", ["reviewer_id"])
    op.create_index("ix_argus_signal_reviews_created_at", "argus_signal_reviews", ["created_at"])


def downgrade() -> None:
    op.drop_table("argus_signal_reviews")
    op.drop_table("argus_signal_evidence")
    op.drop_table("argus_signals")
