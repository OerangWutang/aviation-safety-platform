"""Add claim hot-path indexes.

Revision ID: 026
Revises: 025

The ingestion, conflict-reconciliation, provenance replay, and manual-reopen
paths repeatedly filter claims by active event/field, raw snapshot lineage, and
supersession parent.  PostgreSQL does not automatically index foreign keys, so
these explicit indexes keep those paths from becoming table scans as claim
history grows.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "026"
down_revision = "025"
branch_labels = None
depends_on = None

_ACTIVE_CLAIM_TYPES = "claim_type IN ('RAW', 'CONFIRMED', 'MANUAL_OVERRIDE')"


def upgrade() -> None:
    op.create_index(
        "ix_claims_active_event",
        "claims",
        ["event_id"],
        postgresql_where=sa.text(_ACTIVE_CLAIM_TYPES),
    )
    op.create_index(
        "ix_claims_active_event_field",
        "claims",
        ["event_id", "field_name"],
        postgresql_where=sa.text(_ACTIVE_CLAIM_TYPES),
    )
    op.create_index(
        "ix_claims_raw_snapshot_id",
        "claims",
        ["raw_snapshot_id"],
    )
    op.create_index(
        "ix_claims_superseded_by_claim_id",
        "claims",
        ["superseded_by_claim_id"],
        postgresql_where=sa.text("superseded_by_claim_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_claims_superseded_by_claim_id", table_name="claims")
    op.drop_index("ix_claims_raw_snapshot_id", table_name="claims")
    op.drop_index("ix_claims_active_event_field", table_name="claims")
    op.drop_index("ix_claims_active_event", table_name="claims")
