"""Add unique ingestion key for raw snapshots.

Revision ID: 005
Revises: 004
"""

from alembic import op

revision = "005"
down_revision = "004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_raw_snapshot_ingestion_key",
        "raw_snapshots",
        ["source_id", "ingestion_run_id", "payload_hash"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_raw_snapshot_ingestion_key",
        "raw_snapshots",
        type_="unique",
    )
