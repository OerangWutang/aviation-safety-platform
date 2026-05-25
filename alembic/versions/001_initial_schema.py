"""initial schema

Revision ID: 001
Revises:
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute('CREATE EXTENSION IF NOT EXISTS "uuid-ossp"')
    op.execute('CREATE EXTENSION IF NOT EXISTS "postgis"')

    op.create_table(
        "sources",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False, unique=True),
        sa.Column("kind", sa.String(50), nullable=False),
        sa.Column("reliability_tier", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_table(
        "ingestion_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "source_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("sources.id"), nullable=False
        ),
        sa.Column("status", sa.String(50), nullable=False, server_default="running"),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table(
        "accident_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "merged_into_event_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("accident_events.id"),
            nullable=True,
        ),
    )
    op.create_table(
        "raw_snapshots",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "source_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("sources.id"), nullable=False
        ),
        sa.Column(
            "ingestion_run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("ingestion_runs.id"),
            nullable=False,
        ),
        sa.Column("payload_hash", sa.String(256), nullable=False),
        sa.Column("payload_json", postgresql.JSONB(), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index("ix_raw_snapshots_payload_hash", "raw_snapshots", ["payload_hash"])
    op.create_table(
        "claims",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "event_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("accident_events.id"),
            nullable=False,
        ),
        sa.Column(
            "source_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("sources.id"), nullable=False
        ),
        sa.Column(
            "raw_snapshot_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("raw_snapshots.id"),
            nullable=True,
        ),
        sa.Column("field_name", sa.String(255), nullable=False),
        sa.Column("field_value", postgresql.JSONB(), nullable=False),
        sa.Column("claim_type", sa.String(50), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "superseded_by_claim_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("claims.id"),
            nullable=True,
        ),
    )
    op.create_index("ix_claims_event_id", "claims", ["event_id"])
    op.create_index("ix_claims_source_id", "claims", ["source_id"])
    op.create_index("ix_claims_field_name", "claims", ["field_name"])
    op.create_table(
        "claim_history",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "claim_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("claims.id"), nullable=False
        ),
        sa.Column("from_value", postgresql.JSONB(), nullable=True),
        sa.Column("to_value", postgresql.JSONB(), nullable=True),
        sa.Column("from_claim_type", sa.String(50), nullable=True),
        sa.Column("to_claim_type", sa.String(50), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False, server_default=""),
        sa.Column("modifier_type", sa.String(50), nullable=False),
        sa.Column("modifier_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index("ix_claim_history_claim_id", "claim_history", ["claim_id"])
    op.create_table(
        "claim_conflicts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "event_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("accident_events.id"),
            nullable=False,
        ),
        sa.Column("field_name", sa.String(255), nullable=False),
        sa.Column("status", sa.String(50), nullable=False, server_default="OPEN"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("last_modified_reason", sa.String(50), nullable=False, server_default="INITIAL"),
        sa.Column(
            "winning_claim_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("claims.id"),
            nullable=True,
        ),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index("ix_claim_conflicts_event_id", "claim_conflicts", ["event_id"])
    op.create_index("ix_claim_conflicts_status", "claim_conflicts", ["status"])
    op.create_index("ix_claim_conflicts_field_name", "claim_conflicts", ["field_name"])
    op.create_table(
        "claim_conflict_claims",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "conflict_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("claim_conflicts.id"),
            nullable=False,
        ),
        sa.Column(
            "claim_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("claims.id"), nullable=False
        ),
        sa.UniqueConstraint("conflict_id", "claim_id", name="uq_conflict_claim"),
    )
    op.create_table(
        "conflict_activity_log",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "conflict_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("claim_conflicts.id"),
            nullable=False,
        ),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("from_status", sa.String(50), nullable=True),
        sa.Column("to_status", sa.String(50), nullable=False),
        sa.Column("modifier_type", sa.String(50), nullable=False),
        sa.Column("modifier_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False, server_default=""),
        sa.Column("version_at_moment", sa.Integer(), nullable=False),
        sa.Column("claims_snapshot", postgresql.JSONB(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("conflict_id", "sequence", name="uq_conflict_activity_sequence"),
    )
    op.create_table(
        "projected_accident_records",
        sa.Column(
            "event_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("accident_events.id"),
            primary_key=True,
        ),
        sa.Column("projection_version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("fields", postgresql.JSONB(), nullable=False),
        sa.Column("completeness_score", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "unresolved_conflict_fields", postgresql.JSONB(), nullable=False, server_default="[]"
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_table(
        "outbox_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("event_type", sa.String(255), nullable=False),
        sa.Column("aggregate_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("status", sa.String(50), nullable=False, server_default="PENDING"),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("locked_by", sa.String(255), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_outbox_events_status", "outbox_events", ["status"])
    op.create_table(
        "accident_projection_history",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "accident_event_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("accident_events.id"),
            nullable=False,
        ),
        sa.Column("projection_version", sa.Integer(), nullable=False),
        sa.Column(
            "caused_by_conflict_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("claim_conflicts.id"),
            nullable=True,
        ),
        sa.Column(
            "caused_by_ingestion_run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("ingestion_runs.id"),
            nullable=True,
        ),
        sa.Column("projected_record_snapshot", postgresql.JSONB(), nullable=False),
        sa.Column("projected_record_hash", sa.String(255), nullable=False),
        sa.Column("changed_fields", postgresql.JSONB(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint(
            "accident_event_id", "projection_version", name="uq_projection_history_version"
        ),
    )
    op.create_table(
        "archive_manifests",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("object_path", sa.String(1024), nullable=False),
        sa.Column("date_range_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("date_range_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source_system", sa.String(255), nullable=False),
        sa.Column("row_count", sa.Integer(), nullable=False),
        sa.Column("checksum", sa.String(255), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("created_by_process_id", sa.String(255), nullable=True),
    )


def downgrade() -> None:
    for table in [
        "archive_manifests",
        "accident_projection_history",
        "outbox_events",
        "projected_accident_records",
        "conflict_activity_log",
        "claim_conflict_claims",
        "claim_conflicts",
        "claim_history",
        "claims",
        "raw_snapshots",
        "accident_events",
        "ingestion_runs",
        "sources",
    ]:
        op.drop_table(table)
