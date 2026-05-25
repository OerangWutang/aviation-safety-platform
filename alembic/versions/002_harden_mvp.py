"""harden MVP outbox idempotency + score type + api keys

Revision ID: 002
Revises: 001
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "002"
down_revision = "001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "projected_accident_records",
        "completeness_score",
        existing_type=sa.Integer(),
        type_=sa.Float(),
        existing_server_default=sa.text("0"),
    )
    op.add_column(
        "accident_projection_history",
        sa.Column(
            "caused_by_outbox_event_id",
            UUID(as_uuid=True),
            sa.ForeignKey("outbox_events.id"),
            nullable=True,
        ),
    )
    op.create_index(
        "uq_projection_history_outbox_event",
        "accident_projection_history",
        ["caused_by_outbox_event_id"],
        unique=True,
        postgresql_where=sa.text("caused_by_outbox_event_id IS NOT NULL"),
    )
    op.create_table(
        "api_keys",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("key_hash", sa.String(255), nullable=False, unique=True),
        sa.Column("user_id", UUID(as_uuid=True), nullable=False),
        sa.Column("role", sa.String(50), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_api_keys_key_hash", "api_keys", ["key_hash"], unique=True)
    op.create_index("ix_api_keys_user_id", "api_keys", ["user_id"])
    op.create_index("ix_api_keys_is_active", "api_keys", ["is_active"])


def downgrade() -> None:
    op.drop_index("ix_api_keys_is_active", table_name="api_keys")
    op.drop_index("ix_api_keys_user_id", table_name="api_keys")
    op.drop_index("ix_api_keys_key_hash", table_name="api_keys")
    op.drop_table("api_keys")
    op.drop_index("uq_projection_history_outbox_event", table_name="accident_projection_history")
    op.drop_column("accident_projection_history", "caused_by_outbox_event_id")
    op.alter_column(
        "projected_accident_records",
        "completeness_score",
        existing_type=sa.Float(),
        type_=sa.Integer(),
        existing_server_default=sa.text("0"),
    )
