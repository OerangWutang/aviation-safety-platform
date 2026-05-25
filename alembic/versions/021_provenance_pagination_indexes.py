"""Add provenance pagination indexes.

Revision ID: 021
Revises: 020

The provenance endpoint now keyset-paginates high-cardinality sections.  Claim
history and conflict activity rows are denormalized with ``event_id`` so event-
level provenance can use ``(event_id, created_at, id)`` indexes directly instead
of joining through parent rows and sorting every historical record for the event.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "021"
down_revision = "020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "claim_history",
        sa.Column("event_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "conflict_activity_log",
        sa.Column("event_id", postgresql.UUID(as_uuid=True), nullable=True),
    )

    op.create_foreign_key(
        "fk_claim_history_event_id_accident_events",
        "claim_history",
        "accident_events",
        ["event_id"],
        ["id"],
    )
    op.create_foreign_key(
        "fk_conflict_activity_event_id_accident_events",
        "conflict_activity_log",
        "accident_events",
        ["event_id"],
        ["id"],
    )

    op.execute(
        """
        UPDATE claim_history AS h
        SET event_id = c.event_id
        FROM claims AS c
        WHERE h.claim_id = c.id
          AND h.event_id IS NULL
        """
    )
    op.execute(
        """
        UPDATE conflict_activity_log AS a
        SET event_id = cc.event_id
        FROM claim_conflicts AS cc
        WHERE a.conflict_id = cc.id
          AND a.event_id IS NULL
        """
    )

    op.alter_column("claim_history", "event_id", nullable=False)
    op.alter_column("conflict_activity_log", "event_id", nullable=False)

    op.create_index(
        "ix_claims_event_created_id",
        "claims",
        ["event_id", "created_at", "id"],
    )
    op.create_index(
        "ix_claim_history_event_created_id",
        "claim_history",
        ["event_id", "created_at", "id"],
    )
    op.create_index(
        "ix_claim_conflicts_event_created_id",
        "claim_conflicts",
        ["event_id", "created_at", "id"],
    )
    op.create_index(
        "ix_conflict_activity_event_created_id",
        "conflict_activity_log",
        ["event_id", "created_at", "id"],
    )
    # These single-column indexes were created by the initial schema, but the
    # composites above cover their leading-column lookups while also satisfying
    # keyset ordering. Dropping them avoids redundant B-tree maintenance on
    # high-write tables.
    op.drop_index("ix_claims_event_id", table_name="claims")
    op.drop_index("ix_claim_conflicts_event_id", table_name="claim_conflicts")

    # Replace the original unique constraint's implicit two-column B-tree plus
    # the pagination helper B-tree with one unique covering index. Uniqueness is
    # still enforced on (accident_event_id, projection_version), while id is
    # available from index leaf pages for keyset pagination.
    op.drop_constraint(
        "uq_projection_history_version",
        "accident_projection_history",
        type_="unique",
    )
    op.create_index(
        "uq_projection_history_version",
        "accident_projection_history",
        ["accident_event_id", "projection_version"],
        unique=True,
        postgresql_include=["id"],
    )


def downgrade() -> None:
    op.drop_index(
        "uq_projection_history_version",
        table_name="accident_projection_history",
    )
    op.create_unique_constraint(
        "uq_projection_history_version",
        "accident_projection_history",
        ["accident_event_id", "projection_version"],
    )
    op.drop_index("ix_conflict_activity_event_created_id", table_name="conflict_activity_log")
    op.create_index("ix_claim_conflicts_event_id", "claim_conflicts", ["event_id"])
    op.create_index("ix_claims_event_id", "claims", ["event_id"])
    op.drop_index("ix_claim_conflicts_event_created_id", table_name="claim_conflicts")
    op.drop_index("ix_claim_history_event_created_id", table_name="claim_history")
    op.drop_index("ix_claims_event_created_id", table_name="claims")

    op.drop_constraint(
        "fk_conflict_activity_event_id_accident_events",
        "conflict_activity_log",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_claim_history_event_id_accident_events",
        "claim_history",
        type_="foreignkey",
    )
    op.drop_column("conflict_activity_log", "event_id")
    op.drop_column("claim_history", "event_id")
