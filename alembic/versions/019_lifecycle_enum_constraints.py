"""lifecycle enum check constraints

Add database-level CHECK constraints for lifecycle/status columns that are
represented by domain enums but were previously stored as unconstrained strings.
The constraints are added NOT VALID so existing legacy data does not block a
schema upgrade; PostgreSQL still enforces them for new/updated rows. Operators
can validate them explicitly after auditing old rows.

Revision ID: 019
Revises: 018
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "019"
down_revision = "018"
branch_labels = None
depends_on = None

_CONSTRAINTS: tuple[tuple[str, str, str], ...] = (
    ("sources", "ck_sources_kind", "kind IN ('EXTERNAL', 'INTERNAL')"),
    (
        "ingestion_runs",
        "ck_ingestion_runs_status",
        "status IN ('running', 'finished', 'failed', 'completed')",
    ),
    (
        "claims",
        "ck_claims_claim_type",
        "claim_type IN ('RAW', 'CONFIRMED', 'MANUAL_OVERRIDE', 'SUPERSEDED')",
    ),
    (
        "claim_history",
        "ck_claim_history_action",
        "action IN ('updated', 'created', 'superseded', 'merged', 'reactivated')",
    ),
    (
        "claim_history",
        "ck_claim_history_from_claim_type",
        "from_claim_type IS NULL OR from_claim_type IN "
        "('RAW', 'CONFIRMED', 'MANUAL_OVERRIDE', 'SUPERSEDED')",
    ),
    (
        "claim_history",
        "ck_claim_history_to_claim_type",
        "to_claim_type IN ('RAW', 'CONFIRMED', 'MANUAL_OVERRIDE', 'SUPERSEDED')",
    ),
    (
        "claim_history",
        "ck_claim_history_modifier_type",
        "modifier_type IN ('USER', 'INGESTION', 'SYSTEM')",
    ),
    ("claim_conflicts", "ck_claim_conflicts_status", "status IN ('OPEN', 'RESOLVED')"),
    (
        "claim_conflicts",
        "ck_claim_conflicts_last_modified_reason",
        "last_modified_reason IN "
        "('INITIAL', 'NEW_EVIDENCE', 'EVIDENCE_UPDATED', "
        "'USER_RESOLVED', 'USER_REOPENED', 'SYSTEM_AUTO_CLOSED')",
    ),
    (
        "conflict_activity_log",
        "ck_conflict_activity_from_status",
        "from_status IS NULL OR from_status IN ('OPEN', 'RESOLVED')",
    ),
    (
        "conflict_activity_log",
        "ck_conflict_activity_to_status",
        "to_status IN ('OPEN', 'RESOLVED')",
    ),
    (
        "conflict_activity_log",
        "ck_conflict_activity_modifier_type",
        "modifier_type IN ('USER', 'INGESTION', 'SYSTEM')",
    ),
    (
        "outbox_events",
        "ck_outbox_events_status",
        "status IN ('PENDING', 'PROCESSING', 'PROCESSED', 'FAILED', 'DEAD_LETTER')",
    ),
    ("outbox_events", "ck_outbox_events_event_type", "event_type IN ('CLAIMS_UPDATED')"),
    (
        "pending_duplicate_reviews",
        "ck_pending_duplicate_reviews_status",
        "status IN ('PENDING', 'REJECTED', 'MERGED', 'AUTO_MERGED', 'CONFIRMED_DUPLICATE')",
    ),
)


def upgrade() -> None:
    for table, name, expression in _CONSTRAINTS:
        op.execute(
            sa.text(f"ALTER TABLE {table} ADD CONSTRAINT {name} CHECK ({expression}) NOT VALID")
        )


def downgrade() -> None:
    for table, name, _expression in reversed(_CONSTRAINTS):
        op.execute(sa.text(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {name}"))
