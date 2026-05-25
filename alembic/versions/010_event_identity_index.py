"""Add event_identity_index: synchronous identity substrate for event matching.

Why this migration is needed
-----------------------------
Migration 009 added event matching against ``projected_accident_records``, but
projections are built asynchronously by the outbox worker.  Two rapid ingestions
of the same accident - before the first projection is built - both find an empty
projection table and both create new events without any duplicate review.

``event_identity_index`` is written in the same transaction as ingestion, so it
is immediately visible to the next ingestion.  A transaction-scoped advisory lock
on ``(event_date_norm, registration_norm)`` further closes the race window for
truly concurrent ingestions.

Schema
------
``event_id`` PK (FK accident_events.id)
  One row per event; updated/merged on re-ingestion.

Normalised string fields (lowercase, stripped):
  ``event_date_norm`` VARCHAR(10)   - YYYY-MM-DD
  ``registration_norm`` VARCHAR(50) - no hyphens/spaces
  ``operator_norm`` VARCHAR(255)
  ``location_norm`` VARCHAR(255)
  ``aircraft_type_norm`` VARCHAR(255)

``source_record_ids`` JSONB         - accumulated array of source record IDs
``updated_at`` TIMESTAMPTZ

Indexes
-------
``ix_identity_date_reg (event_date_norm, registration_norm)``
  Primary fast path: date range pre-filter + registration lookup.

``ix_identity_date (event_date_norm)``
  Date-only scan for payloads that do not supply a registration.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "010"
down_revision = "009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "event_identity_index",
        sa.Column(
            "event_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("accident_events.id"),
            primary_key=True,
            nullable=False,
        ),
        sa.Column("event_date_norm", sa.String(10), nullable=True),
        sa.Column("registration_norm", sa.String(50), nullable=True),
        sa.Column("operator_norm", sa.String(255), nullable=True),
        sa.Column("location_norm", sa.String(255), nullable=True),
        sa.Column("aircraft_type_norm", sa.String(255), nullable=True),
        sa.Column(
            "source_record_ids",
            sa.dialects.postgresql.JSONB,
            nullable=False,
            server_default="[]",
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_identity_date_reg",
        "event_identity_index",
        ["event_date_norm", "registration_norm"],
    )
    op.create_index(
        "ix_identity_date",
        "event_identity_index",
        ["event_date_norm"],
    )


def downgrade() -> None:
    op.drop_index("ix_identity_date", table_name="event_identity_index")
    op.drop_index("ix_identity_date_reg", table_name="event_identity_index")
    op.drop_table("event_identity_index")
