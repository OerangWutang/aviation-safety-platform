"""Canonicalize raw snapshot idempotency key.

Revision ID: 011
Revises: 010
"""

import sqlalchemy as sa
from alembic import op

revision = "011"
down_revision = "010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # The idempotency key / ingestion run identifies exactly one request.
    # The previous constraint included payload_hash, which allowed the same
    # idempotency key to be reused with different payloads and recorded as a
    # second snapshot.  The application now reports that case as a 409-style
    # idempotency mismatch; this constraint closes the concurrent race too.
    duplicates = (
        op.get_bind()
        .execute(
            sa.text(
                """
            SELECT source_id, ingestion_run_id, COUNT(*) AS snapshot_count
            FROM raw_snapshots
            GROUP BY source_id, ingestion_run_id
            HAVING COUNT(*) > 1
            LIMIT 10
            """
            )
        )
        .fetchall()
    )
    if duplicates:
        examples = ", ".join(
            f"source_id={row.source_id}, ingestion_run_id={row.ingestion_run_id}, "
            f"count={row.snapshot_count}"
            for row in duplicates
        )
        raise RuntimeError(
            "Cannot apply migration 011: raw_snapshots contains duplicate "
            "(source_id, ingestion_run_id) rows that were allowed by the old "
            "payload-hash-scoped constraint. Resolve or archive those rows "
            "manually before applying this migration. Examples: " + examples
        )

    op.drop_constraint(
        "uq_raw_snapshot_ingestion_key",
        "raw_snapshots",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_raw_snapshot_ingestion_key",
        "raw_snapshots",
        ["source_id", "ingestion_run_id"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_raw_snapshot_ingestion_key",
        "raw_snapshots",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_raw_snapshot_ingestion_key",
        "raw_snapshots",
        ["source_id", "ingestion_run_id", "payload_hash"],
    )
