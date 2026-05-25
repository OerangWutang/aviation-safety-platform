"""raw snapshot submission audit and replay result

Separate the raw-payload hash from the full-submission idempotency hash and
persist the completed ingestion result used for exact idempotent replay.

Revision ID: 016
Revises: 015
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "016"
down_revision = "015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "raw_snapshots",
        sa.Column("raw_payload_hash", sa.String(256), nullable=True),
    )
    op.add_column(
        "raw_snapshots",
        sa.Column("submission_hash", sa.String(256), nullable=True),
    )
    op.add_column(
        "raw_snapshots",
        sa.Column("submission_fingerprint_json", postgresql.JSONB(), nullable=True),
    )
    op.add_column(
        "raw_snapshots",
        sa.Column("ingestion_result_json", postgresql.JSONB(), nullable=True),
    )
    op.create_index(
        "ix_raw_snapshots_raw_payload_hash",
        "raw_snapshots",
        ["raw_payload_hash"],
    )
    op.create_index(
        "ix_raw_snapshots_submission_hash",
        "raw_snapshots",
        ["submission_hash"],
    )

    # Backfill the explicit submission_hash from the legacy payload_hash column.
    # Rows from the immediately previous application version already used
    # payload_hash as the full-submission idempotency hash.  Older rows may have
    # stored only the raw-payload hash there; application replay logic accepts
    # that legacy shape only when the new audit columns are absent.  Raw payload
    # hashes cannot be safely reconstructed in a portable migration without
    # relying on PostgreSQL extensions, so raw_payload_hash remains NULL for old
    # rows and is populated by application code for new rows.
    op.execute(
        sa.text(
            "UPDATE raw_snapshots SET submission_hash = payload_hash WHERE submission_hash IS NULL"
        )
    )


def downgrade() -> None:
    op.drop_index("ix_raw_snapshots_submission_hash", table_name="raw_snapshots")
    op.drop_index("ix_raw_snapshots_raw_payload_hash", table_name="raw_snapshots")
    op.drop_column("raw_snapshots", "ingestion_result_json")
    op.drop_column("raw_snapshots", "submission_fingerprint_json")
    op.drop_column("raw_snapshots", "submission_hash")
    op.drop_column("raw_snapshots", "raw_payload_hash")
