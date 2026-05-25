"""Hermes v0.1 — Source Registry & Fetch Queue tables.

Revision ID: 030
Revises: 029
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

revision = "030"
down_revision = "029"
branch_labels = None
depends_on = None

_UUID = PG_UUID(as_uuid=True)

_SOURCE_TYPES = ("OFFICIAL_AGENCY", "NEWS", "DATABASE", "ARCHIVE", "OTHER")
_TARGET_STATUSES = ("ACTIVE", "PAUSED", "DISABLED")
_JOB_STATUSES = ("QUEUED", "RUNNING", "SUCCEEDED", "FAILED", "CANCELLED")
_CONTENT_TYPES = ("HTML", "PDF", "TEXT", "JSON", "XML", "BINARY", "UNKNOWN")
_CHANGE_TYPES = ("FIRST_SEEN", "CONTENT_CHANGED", "CONTENT_UNCHANGED", "FETCH_FAILED")


def upgrade() -> None:
    op.create_table(
        "hermes_sources",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("source_type", sa.String(50), nullable=False),
        sa.Column("base_url", sa.Text, nullable=True),
        sa.Column("reliability_tier", sa.String(50), nullable=True),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.true()),
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
            f"source_type IN ({', '.join(repr(v) for v in _SOURCE_TYPES)})",
            name="ck_hermes_sources_source_type",
        ),
    )
    op.create_index("ix_hermes_sources_source_type", "hermes_sources", ["source_type"])
    op.create_index("ix_hermes_sources_is_active", "hermes_sources", ["is_active"])
    op.execute("CREATE UNIQUE INDEX uq_hermes_sources_name_lower ON hermes_sources (lower(name))")

    op.create_table(
        "hermes_crawl_targets",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column("source_id", _UUID, sa.ForeignKey("hermes_sources.id"), nullable=False),
        sa.Column("url", sa.Text, nullable=False),
        sa.Column("normalized_url", sa.Text, nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="ACTIVE"),
        sa.Column("label", sa.Text, nullable=True),
        sa.Column("last_fetch_job_id", _UUID, nullable=True),
        sa.Column("last_fetched_document_id", _UUID, nullable=True),
        sa.Column("last_content_sha256", sa.String(64), nullable=True),
        sa.Column("last_http_status", sa.Integer, nullable=True),
        sa.Column("last_fetched_at", sa.DateTime(timezone=True), nullable=True),
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
            f"status IN ({', '.join(repr(v) for v in _TARGET_STATUSES)})",
            name="ck_hermes_crawl_targets_status",
        ),
    )
    op.create_unique_constraint(
        "uq_hermes_crawl_targets_normalized_url", "hermes_crawl_targets", ["normalized_url"]
    )
    op.create_index("ix_hermes_crawl_targets_source_id", "hermes_crawl_targets", ["source_id"])
    op.create_index("ix_hermes_crawl_targets_status", "hermes_crawl_targets", ["status"])
    op.create_index(
        "ix_hermes_crawl_targets_last_fetched_at", "hermes_crawl_targets", ["last_fetched_at"]
    )

    op.create_table(
        "hermes_fetch_jobs",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column("target_id", _UUID, sa.ForeignKey("hermes_crawl_targets.id"), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="QUEUED"),
        sa.Column("priority", sa.Integer, nullable=False, server_default="100"),
        sa.Column("attempt_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer, nullable=False, server_default="3"),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_message", sa.Text, nullable=True),
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
            f"status IN ({', '.join(repr(v) for v in _JOB_STATUSES)})",
            name="ck_hermes_fetch_jobs_status",
        ),
        sa.CheckConstraint("attempt_count >= 0", name="ck_hermes_fetch_jobs_attempt_count"),
        sa.CheckConstraint("max_attempts >= 1", name="ck_hermes_fetch_jobs_max_attempts"),
    )
    op.create_index("ix_hermes_fetch_jobs_target_id", "hermes_fetch_jobs", ["target_id"])
    op.create_index("ix_hermes_fetch_jobs_status", "hermes_fetch_jobs", ["status"])
    op.create_index("ix_hermes_fetch_jobs_priority", "hermes_fetch_jobs", ["priority"])
    op.create_index("ix_hermes_fetch_jobs_scheduled_at", "hermes_fetch_jobs", ["scheduled_at"])
    op.execute(
        "CREATE UNIQUE INDEX uq_hermes_fetch_jobs_one_active_per_target "
        "ON hermes_fetch_jobs (target_id) "
        "WHERE status IN ('QUEUED', 'RUNNING')"
    )

    op.create_table(
        "hermes_fetched_documents",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column("target_id", _UUID, sa.ForeignKey("hermes_crawl_targets.id"), nullable=False),
        sa.Column("fetch_job_id", _UUID, sa.ForeignKey("hermes_fetch_jobs.id"), nullable=False),
        sa.Column("url", sa.Text, nullable=False),
        sa.Column("final_url", sa.Text, nullable=True),
        sa.Column("http_status", sa.Integer, nullable=True),
        sa.Column("content_type", sa.String(20), nullable=False),
        sa.Column("content_sha256", sa.String(64), nullable=False),
        sa.Column("content_length", sa.Integer, nullable=False),
        sa.Column("title", sa.Text, nullable=True),
        sa.Column("storage_path", sa.Text, nullable=True),
        sa.Column("raw_text_preview", sa.Text, nullable=True),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            f"content_type IN ({', '.join(repr(v) for v in _CONTENT_TYPES)})",
            name="ck_hermes_fetched_documents_content_type",
        ),
        sa.CheckConstraint(
            "content_length >= 0", name="ck_hermes_fetched_documents_content_length"
        ),
    )
    op.create_index(
        "ix_hermes_fetched_documents_target_id", "hermes_fetched_documents", ["target_id"]
    )
    op.create_index(
        "ix_hermes_fetched_documents_fetch_job_id", "hermes_fetched_documents", ["fetch_job_id"]
    )
    op.create_index(
        "ix_hermes_fetched_documents_content_sha256", "hermes_fetched_documents", ["content_sha256"]
    )
    op.create_unique_constraint(
        "uq_hermes_fetched_documents_target_hash",
        "hermes_fetched_documents",
        ["target_id", "content_sha256"],
    )

    op.create_table(
        "hermes_source_changes",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column("target_id", _UUID, sa.ForeignKey("hermes_crawl_targets.id"), nullable=False),
        sa.Column("fetch_job_id", _UUID, sa.ForeignKey("hermes_fetch_jobs.id"), nullable=True),
        sa.Column(
            "previous_document_id",
            _UUID,
            sa.ForeignKey("hermes_fetched_documents.id"),
            nullable=True,
        ),
        sa.Column(
            "new_document_id", _UUID, sa.ForeignKey("hermes_fetched_documents.id"), nullable=True
        ),
        sa.Column("change_type", sa.String(30), nullable=False),
        sa.Column("previous_sha256", sa.String(64), nullable=True),
        sa.Column("new_sha256", sa.String(64), nullable=True),
        sa.Column("detected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            f"change_type IN ({', '.join(repr(v) for v in _CHANGE_TYPES)})",
            name="ck_hermes_source_changes_change_type",
        ),
    )
    op.create_index("ix_hermes_source_changes_target_id", "hermes_source_changes", ["target_id"])
    op.create_index(
        "ix_hermes_source_changes_fetch_job_id", "hermes_source_changes", ["fetch_job_id"]
    )
    op.create_index(
        "ix_hermes_source_changes_change_type", "hermes_source_changes", ["change_type"]
    )
    op.create_index(
        "ix_hermes_source_changes_detected_at", "hermes_source_changes", ["detected_at"]
    )


def downgrade() -> None:
    op.drop_table("hermes_source_changes")
    op.drop_table("hermes_fetched_documents")
    op.drop_table("hermes_fetch_jobs")
    op.drop_table("hermes_crawl_targets")
    op.drop_table("hermes_sources")
