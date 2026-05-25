import uuid
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def gen_uuid() -> uuid.UUID:
    return uuid.uuid4()


def now_utc() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class SourceModel(Base):
    __tablename__ = "sources"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=gen_uuid)
    name: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    kind: Mapped[str] = mapped_column(String(50), nullable=False)
    reliability_tier: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    field_mapping_json: Mapped[dict[str, str]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)

    __table_args__ = (
        CheckConstraint("reliability_tier >= 1", name="ck_sources_reliability_tier_ge_1"),
        CheckConstraint("kind IN ('EXTERNAL', 'INTERNAL')", name="ck_sources_kind"),
    )


class IngestionRunModel(Base):
    __tablename__ = "ingestion_runs"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=gen_uuid)
    source_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sources.id"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(50), default="running")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    __table_args__ = (
        CheckConstraint(
            "status IN ('running', 'finished', 'failed', 'completed')",
            name="ck_ingestion_runs_status",
        ),
    )


class RawSnapshotModel(Base):
    __tablename__ = "raw_snapshots"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=gen_uuid)
    source_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sources.id"), nullable=False
    )
    ingestion_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ingestion_runs.id"), nullable=False
    )
    payload_hash: Mapped[str] = mapped_column(String(256), nullable=False, index=True)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    # Optional stable identifier assigned by the source system (e.g. NTSB
    # accident number). NULL for sources that don't provide stable record IDs.
    # See migration 009.
    source_record_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Explicit hash/audit/result columns.  payload_hash remains for backward
    # compatibility with older code/migrations, but new code writes the same
    # value to submission_hash and stores raw_payload_hash separately.
    raw_payload_hash: Mapped[str | None] = mapped_column(String(256), nullable=True, index=True)
    submission_hash: Mapped[str | None] = mapped_column(String(256), nullable=True, index=True)
    submission_fingerprint_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    ingestion_result_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    __table_args__ = (
        UniqueConstraint(
            "source_id",
            "ingestion_run_id",
            name="uq_raw_snapshot_ingestion_key",
        ),
        Index(
            "ix_raw_snapshot_source_record",
            "source_id",
            "source_record_id",
            postgresql_where=text("source_record_id IS NOT NULL"),
        ),
        # Migration 018: audit-column pair must be populated together.  Legacy
        # rows pre-016 have both NULL; new ingestions always set both.  This
        # constraint prevents a future patch from inadvertently re-creating
        # the ambiguous "submission_hash set but audit columns NULL" state
        # that the legacy-fallback path in IngestionIdempotencyService would
        # otherwise treat as a legacy row.
        CheckConstraint(
            "(raw_payload_hash IS NULL) = (submission_fingerprint_json IS NULL)",
            name="ck_raw_snapshots_audit_pair_consistent",
        ),
    )


class AccidentEventModel(Base):
    __tablename__ = "accident_events"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=gen_uuid)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    merged_into_event_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("accident_events.id"), nullable=True
    )


class ClaimModel(Base):
    __tablename__ = "claims"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=gen_uuid)
    event_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("accident_events.id"), nullable=False
    )
    source_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sources.id"), nullable=False, index=True
    )
    raw_snapshot_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("raw_snapshots.id"), nullable=True
    )
    field_name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    field_value: Mapped[Any] = mapped_column(JSONB, nullable=False)
    claim_type: Mapped[str] = mapped_column(String(50), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    superseded_by_claim_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("claims.id"), nullable=True
    )
    __table_args__ = (
        CheckConstraint(
            "claim_type IN ('RAW', 'CONFIRMED', 'MANUAL_OVERRIDE', 'SUPERSEDED')",
            name="ck_claims_claim_type",
        ),
        Index("ix_claims_event_created_id", "event_id", "created_at", "id"),
        Index(
            "ix_claims_active_event",
            "event_id",
            postgresql_where=text("claim_type IN ('RAW', 'CONFIRMED', 'MANUAL_OVERRIDE')"),
        ),
        Index(
            "ix_claims_active_event_field",
            "event_id",
            "field_name",
            postgresql_where=text("claim_type IN ('RAW', 'CONFIRMED', 'MANUAL_OVERRIDE')"),
        ),
        Index("ix_claims_raw_snapshot_id", "raw_snapshot_id"),
        Index(
            "ix_claims_superseded_by_claim_id",
            "superseded_by_claim_id",
            postgresql_where=text("superseded_by_claim_id IS NOT NULL"),
        ),
    )


class ClaimHistoryModel(Base):
    __tablename__ = "claim_history"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=gen_uuid)
    claim_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("claims.id"), nullable=False, index=True
    )
    event_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("accident_events.id"), nullable=False
    )
    from_value: Mapped[Any | None] = mapped_column(JSONB, nullable=True)
    to_value: Mapped[Any | None] = mapped_column(JSONB, nullable=True)
    from_claim_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    to_claim_type: Mapped[str] = mapped_column(String(50), nullable=False)
    action: Mapped[str] = mapped_column(String(50), nullable=False, default="updated")
    reason: Mapped[str] = mapped_column(Text, default="")
    modifier_type: Mapped[str] = mapped_column(String(50), nullable=False)
    modifier_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    __table_args__ = (
        CheckConstraint(
            "action IN ('updated', 'created', 'superseded', 'merged', 'reactivated')",
            name="ck_claim_history_action",
        ),
        CheckConstraint(
            "from_claim_type IS NULL OR from_claim_type IN "
            "('RAW', 'CONFIRMED', 'MANUAL_OVERRIDE', 'SUPERSEDED')",
            name="ck_claim_history_from_claim_type",
        ),
        CheckConstraint(
            "to_claim_type IN ('RAW', 'CONFIRMED', 'MANUAL_OVERRIDE', 'SUPERSEDED')",
            name="ck_claim_history_to_claim_type",
        ),
        CheckConstraint(
            "modifier_type IN ('USER', 'INGESTION', 'SYSTEM')",
            name="ck_claim_history_modifier_type",
        ),
        Index("ix_claim_history_event_created_id", "event_id", "created_at", "id"),
    )


class ClaimConflictModel(Base):
    __tablename__ = "claim_conflicts"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=gen_uuid)
    event_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("accident_events.id"), nullable=False
    )
    field_name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(50), nullable=False, default="OPEN", index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    last_modified_reason: Mapped[str] = mapped_column(String(50), default="INITIAL")
    last_modified_note: Mapped[str | None] = mapped_column(String(255), nullable=True)
    winning_claim_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("claims.id"), nullable=True
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    # Partial unique index: at most one OPEN conflict per (event_id, field_name).
    # Mirrors migration 008. Resolved/reopened history rows are unconstrained,
    # but only one OPEN row may exist at a time, even under concurrent ingestion
    # workers. Without this metadata, future Alembic autogenerate would propose
    # dropping it, and INSERT ... ON CONFLICT must target ``index_elements`` +
    # ``index_where`` (not ``constraint=``) because this is an index, not a
    # named unique constraint.
    __table_args__ = (
        CheckConstraint("status IN ('OPEN', 'RESOLVED')", name="ck_claim_conflicts_status"),
        CheckConstraint(
            "last_modified_reason IN "
            "('INITIAL', 'NEW_EVIDENCE', 'EVIDENCE_UPDATED', "
            "'USER_RESOLVED', 'USER_REOPENED', 'SYSTEM_AUTO_CLOSED')",
            name="ck_claim_conflicts_last_modified_reason",
        ),
        Index(
            "uq_open_conflict_event_field",
            "event_id",
            "field_name",
            unique=True,
            postgresql_where=text("status = 'OPEN'"),
        ),
        Index("ix_claim_conflicts_event_created_id", "event_id", "created_at", "id"),
        Index(
            "ix_claim_conflicts_resolved_winning_claim",
            "winning_claim_id",
            postgresql_where=text("status = 'RESOLVED' AND winning_claim_id IS NOT NULL"),
        ),
    )


class ClaimConflictClaimModel(Base):
    __tablename__ = "claim_conflict_claims"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=gen_uuid)
    conflict_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("claim_conflicts.id"), nullable=False
    )
    claim_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("claims.id"), nullable=False, index=True
    )
    __table_args__ = (UniqueConstraint("conflict_id", "claim_id", name="uq_conflict_claim"),)


class ConflictActivityLogModel(Base):
    __tablename__ = "conflict_activity_log"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=gen_uuid)
    conflict_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("claim_conflicts.id"), nullable=False
    )
    event_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("accident_events.id"), nullable=False
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    from_status: Mapped[str | None] = mapped_column(String(50), nullable=True)
    to_status: Mapped[str] = mapped_column(String(50), nullable=False)
    modifier_type: Mapped[str] = mapped_column(String(50), nullable=False)
    modifier_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    reason: Mapped[str] = mapped_column(Text, default="")
    version_at_moment: Mapped[int] = mapped_column(Integer, nullable=False)
    claims_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    __table_args__ = (
        CheckConstraint(
            "from_status IS NULL OR from_status IN ('OPEN', 'RESOLVED')",
            name="ck_conflict_activity_from_status",
        ),
        CheckConstraint("to_status IN ('OPEN', 'RESOLVED')", name="ck_conflict_activity_to_status"),
        CheckConstraint(
            "modifier_type IN ('USER', 'INGESTION', 'SYSTEM')",
            name="ck_conflict_activity_modifier_type",
        ),
        UniqueConstraint("conflict_id", "sequence", name="uq_conflict_activity_sequence"),
        Index("ix_conflict_activity_event_created_id", "event_id", "created_at", "id"),
    )


class ProjectedAccidentRecordModel(Base):
    __tablename__ = "projected_accident_records"
    event_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("accident_events.id"), primary_key=True
    )
    projection_version: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    fields: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    completeness_score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    unresolved_conflict_fields: Mapped[list[str]] = mapped_column(
        JSONB, default=list, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)


class OutboxEventModel(Base):
    __tablename__ = "outbox_events"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=gen_uuid)
    event_type: Mapped[str] = mapped_column(String(255), nullable=False)
    aggregate_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(50), default="PENDING", index=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    locked_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    __table_args__ = (
        CheckConstraint(
            "status IN ('PENDING', 'PROCESSING', 'PROCESSED', 'FAILED', 'DEAD_LETTER')",
            name="ck_outbox_events_status",
        ),
        CheckConstraint(
            "event_type IN ('CLAIMS_UPDATED', 'ECHO_CROSSREF_REQUESTED')",
            name="ck_outbox_events_event_type",
        ),
        Index(
            "ix_outbox_events_pending_created",
            "created_at",
            "id",
            postgresql_where=text("status = 'PENDING'"),
        ),
        Index(
            "ix_outbox_events_failed_retry_created",
            text("next_attempt_at ASC NULLS FIRST"),
            "created_at",
            "id",
            postgresql_where=text("status = 'FAILED'"),
        ),
        Index(
            "ix_outbox_events_unprocessed_created",
            "created_at",
            "id",
            postgresql_where=text("status IN ('PENDING', 'PROCESSING', 'FAILED')"),
        ),
        Index(
            "ix_outbox_events_processing_locked",
            "locked_at",
            "id",
            postgresql_where=text("status = 'PROCESSING'"),
        ),
    )


class OutboxWorkerHeartbeatModel(Base):
    __tablename__ = "outbox_worker_heartbeats"
    worker_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    last_loop_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_successful_batch_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)

    __table_args__ = (
        Index("ix_outbox_worker_heartbeats_last_loop", "last_loop_at"),
        Index("ix_outbox_worker_heartbeats_last_success", "last_successful_batch_at"),
    )


class AccidentProjectionHistoryModel(Base):
    __tablename__ = "accident_projection_history"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=gen_uuid)
    accident_event_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("accident_events.id"), nullable=False
    )
    projection_version: Mapped[int] = mapped_column(Integer, nullable=False)
    caused_by_conflict_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("claim_conflicts.id"), nullable=True
    )
    caused_by_ingestion_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ingestion_runs.id"), nullable=True
    )
    caused_by_outbox_event_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("outbox_events.id"), nullable=True
    )
    projected_record_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    projected_record_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    changed_fields: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    __table_args__ = (
        Index(
            "uq_projection_history_version",
            "accident_event_id",
            "projection_version",
            unique=True,
            postgresql_include=["id"],
        ),
        Index(
            "uq_projection_history_outbox_event",
            "caused_by_outbox_event_id",
            unique=True,
            postgresql_where=text("caused_by_outbox_event_id IS NOT NULL"),
        ),
    )


class ArchiveManifestModel(Base):
    __tablename__ = "archive_manifests"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=gen_uuid)
    object_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    date_range_start: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    date_range_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    source_system: Mapped[str] = mapped_column(String(255), nullable=False)
    row_count: Mapped[int] = mapped_column(Integer, nullable=False)
    checksum: Mapped[str] = mapped_column(String(255), nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    created_by_process_id: Mapped[str | None] = mapped_column(String(255), nullable=True)


class ApiKeyModel(Base):
    __tablename__ = "api_keys"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=gen_uuid)
    key_hash: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    role: Mapped[str] = mapped_column(String(50), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Phase 5: optional tenant binding.  When ``tenant_id`` is set the
    # caller can use ``/enterprise/tenants/{tenant_id}/*`` routes with
    # ``tenant_role``; the system-level ``role`` still governs public
    # reads.  Both columns are NULL on system-only keys.
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    tenant_role: Mapped[str | None] = mapped_column(String(20), nullable=True)
    __table_args__ = (
        CheckConstraint("role IN ('analyst', 'reviewer', 'admin')", name="ck_api_keys_role_valid"),
        CheckConstraint(
            "(tenant_id IS NULL) = (tenant_role IS NULL)",
            name="ck_api_keys_tenant_pair_consistent",
        ),
        CheckConstraint(
            "tenant_role IS NULL OR tenant_role IN ('OWNER', 'MEMBER', 'READ_ONLY')",
            name="ck_api_keys_tenant_role_valid",
        ),
    )


class PendingDuplicateReviewModel(Base):
    __tablename__ = "pending_duplicate_reviews"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=gen_uuid)
    # event_id_a is the pre-existing event; event_id_b is the newly ingested one.
    event_id_a: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("accident_events.id"), nullable=False, index=True
    )
    event_id_b: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("accident_events.id"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(String(50), nullable=False, default="PENDING", index=True)
    match_score: Mapped[float] = mapped_column(Float, nullable=False)
    matched_fields: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    resolution_note: Mapped[str | None] = mapped_column(String(500), nullable=True)
    __table_args__ = (
        CheckConstraint(
            "status IN ('PENDING', 'REJECTED', 'MERGED', 'AUTO_MERGED', 'CONFIRMED_DUPLICATE')",
            name="ck_pending_duplicate_reviews_status",
        ),
        Index(
            "uq_pending_duplicate_reviews_pending_pair",
            text("LEAST(event_id_a, event_id_b)"),
            text("GREATEST(event_id_a, event_id_b)"),
            unique=True,
            postgresql_where=text("status = 'PENDING'"),
        ),
        Index(
            "ix_pending_duplicate_reviews_pending_created_id",
            text("created_at DESC"),
            text("id DESC"),
            postgresql_where=text("status = 'PENDING'"),
        ),
    )


class EventIdentityIndexModel(Base):
    """Synchronous event identity substrate - written in the ingestion transaction.

    See ``EventIdentityIndex`` in entities.py for full rationale.
    The composite index on ``(event_date_norm, registration_norm)`` is the
    primary fast-path for matching; ``event_date_norm`` alone supports the
    date-range pre-filter.
    """

    __tablename__ = "event_identity_index"
    event_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("accident_events.id"), primary_key=True
    )
    event_date_norm: Mapped[str | None] = mapped_column(String(10), nullable=True)
    registration_norm: Mapped[str | None] = mapped_column(String(50), nullable=True)
    operator_norm: Mapped[str | None] = mapped_column(String(255), nullable=True)
    location_norm: Mapped[str | None] = mapped_column(String(255), nullable=True)
    aircraft_type_norm: Mapped[str | None] = mapped_column(String(255), nullable=True)
    source_record_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    # Accumulates every normalised registration ever asserted for this event.
    # Values are the same normalised form as registration_norm (lowercase,
    # hyphens/spaces stripped).  The upsert unions this array so no known alias
    # is ever lost, making future ingestions for any historical registration
    # find the existing event rather than silently creating a duplicate.
    registration_norms: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    __table_args__ = (
        # Primary fast-path: date range pre-filter + registration exact match
        Index("ix_identity_date_reg", "event_date_norm", "registration_norm"),
        # Date-only for payloads without registration
        Index("ix_identity_date", "event_date_norm"),
    )


# ── Orion ORM Models ──────────────────────────────────────────────────────────

_ORION_ENTITY_TYPES = (
    "AIRCRAFT",
    "OPERATOR",
    "AIRPORT",
    "AIRCRAFT_TYPE",
    "MANUFACTURER",
    "INVESTIGATION_AGENCY",
    "COUNTRY",
)
_ORION_REL_TYPES = (
    "INVOLVED_AIRCRAFT",
    "OPERATED_BY",
    "AIRCRAFT_TYPE",
    "MANUFACTURED_BY",
    "OCCURRED_AT",
    "LOCATED_IN",
    "INVESTIGATED_BY",
)
_ORION_REVIEW_STATUSES = ("PENDING", "MERGED", "REJECTED", "AUTO_MERGED")


class OrionEntityModel(Base):
    __tablename__ = "orion_entities"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=gen_uuid)
    entity_type: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    canonical_name: Mapped[str] = mapped_column(String(500), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="ACTIVE")
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    merged_into_entity_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orion_entities.id"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('ACTIVE', 'MERGED', 'DEPRECATED')",
            name="ck_orion_entities_status",
        ),
        CheckConstraint(
            f"entity_type IN ({', '.join(repr(t) for t in _ORION_ENTITY_TYPES)})",
            name="ck_orion_entities_entity_type",
        ),
    )


class OrionEntityIdentifierModel(Base):
    __tablename__ = "orion_entity_identifiers"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=gen_uuid)
    entity_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orion_entities.id"), nullable=False, index=True
    )
    entity_type: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    identifier_type: Mapped[str] = mapped_column(String(100), nullable=False)
    identifier_value: Mapped[str] = mapped_column(String(500), nullable=False)
    normalized_value: Mapped[str] = mapped_column(String(500), nullable=False)
    source_claim_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("claims.id"), nullable=True
    )
    raw_snapshot_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("raw_snapshots.id"), nullable=True
    )
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    valid_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)

    __table_args__ = (
        Index(
            "ix_orion_entity_identifiers_type_norm",
            "identifier_type",
            "normalized_value",
        ),
        UniqueConstraint(
            "entity_id",
            "identifier_type",
            "normalized_value",
            name="uq_orion_entity_identifiers_entity_type_norm",
        ),
        Index(
            "uq_orion_entity_identifiers_active_strong_identity",
            "entity_type",
            "identifier_type",
            "normalized_value",
            unique=True,
            postgresql_where=text("valid_to IS NULL"),
        ),
    )


class OrionRelationshipModel(Base):
    __tablename__ = "orion_relationships"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=gen_uuid)
    subject_entity_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orion_entities.id"), nullable=True, index=True
    )
    relationship_type: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    object_entity_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orion_entities.id"), nullable=False, index=True
    )
    accident_event_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("accident_events.id"), nullable=False, index=True
    )
    source_claim_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("claims.id"), nullable=True
    )
    raw_snapshot_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("raw_snapshots.id"), nullable=True
    )
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    valid_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)

    __table_args__ = (
        CheckConstraint(
            f"relationship_type IN ({', '.join(repr(t) for t in _ORION_REL_TYPES)})",
            name="ck_orion_relationships_type",
        ),
        CheckConstraint(
            "accident_event_id IS NOT NULL",
            name="ck_orion_relationships_event_required",
        ),
        Index(
            "uq_orion_relationships_event_level",
            "relationship_type",
            "object_entity_id",
            "accident_event_id",
            unique=True,
            postgresql_where=text("subject_entity_id IS NULL"),
        ),
        Index(
            "uq_orion_relationships_entity_level",
            "subject_entity_id",
            "relationship_type",
            "object_entity_id",
            "accident_event_id",
            unique=True,
            postgresql_where=text("subject_entity_id IS NOT NULL"),
        ),
    )


class OrionEntityClaimLinkModel(Base):
    __tablename__ = "orion_entity_claim_links"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=gen_uuid)
    entity_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orion_entities.id"), nullable=False, index=True
    )
    claim_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("claims.id"), nullable=False, index=True
    )
    raw_snapshot_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("raw_snapshots.id"), nullable=True
    )
    source_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sources.id"), nullable=False
    )
    accident_event_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("accident_events.id"), nullable=False, index=True
    )
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)

    __table_args__ = (
        UniqueConstraint(
            "entity_id",
            "claim_id",
            "accident_event_id",
            name="uq_orion_entity_claim_links_entity_claim_event",
        ),
    )


class OrionEntityReviewModel(Base):
    __tablename__ = "orion_entity_reviews"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=gen_uuid)
    candidate_entity_id_a: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orion_entities.id"), nullable=False
    )
    candidate_entity_id_b: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orion_entities.id"), nullable=False
    )
    entity_type: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    match_score: Mapped[float] = mapped_column(Float, nullable=False)
    matched_identifiers: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="PENDING", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    resolution_note: Mapped[str | None] = mapped_column(String(1000), nullable=True)

    __table_args__ = (
        CheckConstraint(
            f"status IN ({', '.join(repr(s) for s in _ORION_REVIEW_STATUSES)})",
            name="ck_orion_entity_reviews_status",
        ),
        CheckConstraint(
            f"entity_type IN ({', '.join(repr(t) for t in _ORION_ENTITY_TYPES)})",
            name="ck_orion_entity_reviews_entity_type",
        ),
        Index(
            "uq_orion_entity_reviews_pending_pair",
            text("LEAST(candidate_entity_id_a::text, candidate_entity_id_b::text)"),
            text("GREATEST(candidate_entity_id_a::text, candidate_entity_id_b::text)"),
            unique=True,
            postgresql_where=text("status = 'PENDING'"),
        ),
    )


# ── Chronos ORM models ────────────────────────────────────────────────────────

_CHRONOS_EVENT_TYPES = (
    "SCHEDULED_DEPARTURE",
    "ACTUAL_DEPARTURE",
    "TAKEOFF",
    "LAST_CONTACT",
    "EMERGENCY_DECLARED",
    "IMPACT",
    "LANDING",
    "RESCUE_STARTED",
    "INVESTIGATION_OPENED",
    "REPORT_PUBLISHED",
)

_CHRONOS_PRECISIONS = ("EXACT", "MINUTE", "HOUR", "DAY", "APPROXIMATE", "RELATIVE", "UNKNOWN")
_CHRONOS_REVIEW_STATUSES = ("PENDING", "CONFIRMED", "REJECTED", "AUTO_CONFIRMED")


class ChronosTimelineEventModel(Base):
    __tablename__ = "chronos_timeline_events"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=gen_uuid)
    accident_event_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("accident_events.id"), nullable=False, index=True
    )
    event_type: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    occurred_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    timestamp_precision: Mapped[str] = mapped_column(String(20), nullable=False)
    sequence_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    source_claim_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("claims.id"), nullable=True
    )
    raw_snapshot_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("raw_snapshots.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc
    )

    __table_args__ = (
        CheckConstraint(
            f"event_type IN ({', '.join(repr(t) for t in _CHRONOS_EVENT_TYPES)})",
            name="ck_chronos_timeline_events_event_type",
        ),
        CheckConstraint(
            f"timestamp_precision IN ({', '.join(repr(p) for p in _CHRONOS_PRECISIONS)})",
            name="ck_chronos_timeline_events_precision",
        ),
        Index(
            "uq_chronos_timeline_events_idempotent",
            "accident_event_id",
            "event_type",
            "raw_value",
            unique=True,
        ),
    )


class ChronosEventLinkModel(Base):
    __tablename__ = "chronos_event_links"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=gen_uuid)
    accident_event_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("accident_events.id"), nullable=False, index=True
    )
    predecessor_event_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("chronos_timeline_events.id"), nullable=False, index=True
    )
    successor_event_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("chronos_timeline_events.id"), nullable=False, index=True
    )
    relationship_type: Mapped[str] = mapped_column(String(100), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    source_claim_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("claims.id"), nullable=True
    )
    raw_snapshot_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("raw_snapshots.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)

    __table_args__ = (
        CheckConstraint(
            "predecessor_event_id != successor_event_id", name="ck_chronos_event_links_no_self_link"
        ),
        Index(
            "uq_chronos_event_links_pair",
            "accident_event_id",
            "predecessor_event_id",
            "successor_event_id",
            "relationship_type",
            unique=True,
        ),
    )


class ChronosSequenceReviewModel(Base):
    __tablename__ = "chronos_sequence_reviews"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=gen_uuid)
    accident_event_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("accident_events.id"), nullable=False, index=True
    )
    timeline_event_id_a: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("chronos_timeline_events.id"), nullable=False
    )
    timeline_event_id_b: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("chronos_timeline_events.id"), nullable=False
    )
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="PENDING", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    resolution_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        CheckConstraint(
            f"status IN ({', '.join(repr(s) for s in _CHRONOS_REVIEW_STATUSES)})",
            name="ck_chronos_sequence_reviews_status",
        ),
        CheckConstraint(
            "timeline_event_id_a != timeline_event_id_b",
            name="ck_chronos_sequence_reviews_no_self_pair",
        ),
        # Partial expression index created by migration 029 via raw SQL.
        # Prevents duplicate PENDING review pairs regardless of column order
        # (pair (A,B) is the same review as (B,A)).  Declared here so Alembic
        # autogenerate does not suggest recreating it on the next --autogenerate
        # run.  The index cannot be expressed as a UniqueConstraint because it
        # uses LEAST/GREATEST expressions — it must be declared with Index() and
        # a postgresql_using hint so SQLAlchemy can represent it in metadata.
        # See migration 029 for the authoritative CREATE UNIQUE INDEX statement.
        Index(
            "uq_chronos_sequence_reviews_pending_pair",
            text("LEAST(timeline_event_id_a::text, timeline_event_id_b::text)"),
            text("GREATEST(timeline_event_id_a::text, timeline_event_id_b::text)"),
            unique=True,
            postgresql_where=text("status = 'PENDING'"),
        ),
    )


# ── Hermes ORM models ────────────────────────────────────────────────────────


class HermesSourceModel(Base):
    __tablename__ = "hermes_sources"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=gen_uuid)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    source_type: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    base_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    reliability_tier: Mapped[str | None] = mapped_column(String(50), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc
    )

    __table_args__ = (
        CheckConstraint(
            "source_type IN ('OFFICIAL_AGENCY','NEWS','DATABASE','ARCHIVE','OTHER')",
            name="ck_hermes_sources_source_type",
        ),
        Index("uq_hermes_sources_name_lower", text("lower(name)"), unique=True),
    )


class HermesCrawlTargetModel(Base):
    __tablename__ = "hermes_crawl_targets"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=gen_uuid)
    source_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hermes_sources.id"), nullable=False, index=True
    )
    url: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_url: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="ACTIVE", index=True)
    label: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_fetch_job_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    last_fetched_document_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    last_content_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_fetched_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('ACTIVE','PAUSED','DISABLED')",
            name="ck_hermes_crawl_targets_status",
        ),
    )


class HermesFetchJobModel(Base):
    __tablename__ = "hermes_fetch_jobs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=gen_uuid)
    target_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hermes_crawl_targets.id"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="QUEUED", index=True)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=100, index=True)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    scheduled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    locked_by: Mapped[str | None] = mapped_column(String(200), nullable=True, index=True)
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('QUEUED','RUNNING','SUCCEEDED','FAILED','CANCELLED')",
            name="ck_hermes_fetch_jobs_status",
        ),
        CheckConstraint("attempt_count >= 0", name="ck_hermes_fetch_jobs_attempt_count"),
        CheckConstraint("max_attempts >= 1", name="ck_hermes_fetch_jobs_max_attempts"),
        Index(
            "uq_hermes_fetch_jobs_one_active_per_target",
            "target_id",
            unique=True,
            postgresql_where=text("status IN ('QUEUED', 'RUNNING')"),
        ),
        Index(
            "ix_hermes_fetch_jobs_stale_running",
            "lease_expires_at",
            "id",
            postgresql_where=text("status = 'RUNNING' AND lease_expires_at IS NOT NULL"),
        ),
    )


class HermesFetchedDocumentModel(Base):
    __tablename__ = "hermes_fetched_documents"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=gen_uuid)
    target_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hermes_crawl_targets.id"), nullable=False, index=True
    )
    fetch_job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hermes_fetch_jobs.id"), nullable=False, index=True
    )
    url: Mapped[str] = mapped_column(Text, nullable=False)
    final_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    content_type: Mapped[str] = mapped_column(String(20), nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    content_length: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    storage_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_text_preview: Mapped[str | None] = mapped_column(Text, nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)

    __table_args__ = (
        CheckConstraint(
            "content_type IN ('HTML','PDF','TEXT','JSON','XML','BINARY','UNKNOWN')",
            name="ck_hermes_fetched_documents_content_type",
        ),
        CheckConstraint("content_length >= 0", name="ck_hermes_fetched_documents_content_length"),
        UniqueConstraint(
            "target_id", "content_sha256", name="uq_hermes_fetched_documents_target_hash"
        ),
    )


class HermesSourceChangeModel(Base):
    __tablename__ = "hermes_source_changes"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=gen_uuid)
    target_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hermes_crawl_targets.id"), nullable=False, index=True
    )
    fetch_job_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hermes_fetch_jobs.id"), nullable=True, index=True
    )
    previous_document_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hermes_fetched_documents.id"), nullable=True
    )
    new_document_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hermes_fetched_documents.id"), nullable=True
    )
    change_type: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    previous_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    new_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    detected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)

    __table_args__ = (
        CheckConstraint(
            "change_type IN ('FIRST_SEEN','CONTENT_CHANGED','CONTENT_UNCHANGED','FETCH_FAILED')",
            name="ck_hermes_source_changes_change_type",
        ),
    )


# ── Argus ORM models ──────────────────────────────────────────────────────────

_ARGUS_SIGNAL_TYPES = (
    "NEW_SOURCE_CHANGE",
    "TIMELINE_SEQUENCE_CONFLICT",
    "HIGH_CONFLICT_ACCIDENT_RECORD",
    "REPEATED_AIRCRAFT_INVOLVEMENT",
    "REPEATED_OPERATOR_INVOLVEMENT",
    "SOURCE_FETCH_FAILURE_SPIKE",
    "ECHO_STRONG_PRECEDENT_MATCH",
)
_ARGUS_STATUSES = ("OPEN", "CONFIRMED", "DISMISSED", "NEEDS_MORE_REVIEW", "AUTO_RESOLVED")
_ARGUS_SEVERITIES = ("LOW", "MEDIUM", "HIGH", "CRITICAL")
_ARGUS_EVIDENCE_TYPES = (
    "ATLAS_CLAIM",
    "ATLAS_CONFLICT",
    "ATLAS_ACCIDENT_EVENT",
    "ORION_ENTITY",
    "ORION_RELATIONSHIP",
    "CHRONOS_TIMELINE_EVENT",
    "CHRONOS_SEQUENCE_REVIEW",
    "HERMES_SOURCE_CHANGE",
    "HERMES_FETCH_JOB",
    "HERMES_FETCHED_DOCUMENT",
    "ECHO_CROSSREF_RESULT",
)
_ARGUS_DECISIONS = ("CONFIRMED", "DISMISSED", "NEEDS_MORE_REVIEW")


class ArgusSignalModel(Base):
    __tablename__ = "argus_signals"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=gen_uuid)
    signal_type: Mapped[str] = mapped_column(String(60), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="OPEN", index=True)
    severity: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    accident_event_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("accident_events.id"), nullable=True, index=True
    )
    primary_entity_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True, index=True
    )
    source_engine: Mapped[str] = mapped_column(String(50), nullable=False)
    dedupe_key: Mapped[str] = mapped_column(Text, nullable=False)
    # Optimistic-concurrency token for reviewer actions.  See ``ArgusSignal``
    # entity docstring and migration 033.  NOT NULL with default 1 so the
    # backfill is implicit for any pre-existing rows.
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default=text("1")
    )
    first_detected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    last_detected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc
    )

    __table_args__ = (
        CheckConstraint(
            f"signal_type IN ({', '.join(repr(v) for v in _ARGUS_SIGNAL_TYPES)})",
            name="ck_argus_signals_signal_type",
        ),
        CheckConstraint(
            f"status IN ({', '.join(repr(v) for v in _ARGUS_STATUSES)})",
            name="ck_argus_signals_status",
        ),
        CheckConstraint(
            f"severity IN ({', '.join(repr(v) for v in _ARGUS_SEVERITIES)})",
            name="ck_argus_signals_severity",
        ),
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="ck_argus_signals_confidence"),
        CheckConstraint("version >= 1", name="ck_argus_signals_version_positive"),
        Index("uq_argus_signals_dedupe_key", "dedupe_key", unique=True),
        # Stable ordering for ``GET /argus/signals`` and the ``list`` repo
        # method.  ``last_detected_at`` alone is not unique (one detection
        # pass stamps many signals with the same ``now``), so offset
        # pagination would silently skip or duplicate rows without a
        # tiebreaker.  See migration 032.
        Index(
            "ix_argus_signals_last_detected_id_desc",
            "last_detected_at",
            "id",
        ),
    )


class ArgusSignalEvidenceModel(Base):
    __tablename__ = "argus_signal_evidence"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=gen_uuid)
    signal_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("argus_signals.id"), nullable=False, index=True
    )
    evidence_type: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    evidence_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    engine: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)

    __table_args__ = (
        CheckConstraint(
            f"evidence_type IN ({', '.join(repr(v) for v in _ARGUS_EVIDENCE_TYPES)})",
            name="ck_argus_signal_evidence_type",
        ),
        UniqueConstraint(
            "signal_id", "evidence_type", "evidence_id", name="uq_argus_signal_evidence_link"
        ),
    )


class ArgusSignalReviewModel(Base):
    __tablename__ = "argus_signal_reviews"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=gen_uuid)
    signal_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("argus_signals.id"), nullable=False, index=True
    )
    decision: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    reviewer_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True, index=True
    )
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, index=True
    )

    __table_args__ = (
        CheckConstraint(
            f"decision IN ({', '.join(repr(v) for v in _ARGUS_DECISIONS)})",
            name="ck_argus_signal_reviews_decision",
        ),
    )


# ── Publication: public event pages (Phase 1) ────────────────────────────────
#
# A ``public_event_pages`` row is editorial overlay metadata that sits *on
# top* of an existing canonical ``accident_events`` row and its
# ``projected_accident_records`` projection.  It does NOT duplicate
# projected facts: title is a short stable display string, short_summary
# and narrative_markdown are editorial prose, and any structured fields
# (operator, fatalities, location, ...) are read from the projection at
# response time.
#
# Statuses are deliberately limited to DRAFT, PUBLISHED, RETRACTED for
# Phase 1.  The full editorial state machine (IN_REVIEW, APPROVED,
# ARCHIVED) is reserved for Phase 9; the ``version`` column is present
# now so optimistic concurrency does not require another migration.


class PublicEventPageModel(Base):
    __tablename__ = "public_event_pages"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=gen_uuid)
    event_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("accident_events.id"), nullable=False
    )
    slug: Mapped[str] = mapped_column(String(160), nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    short_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    narrative_markdown: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="DRAFT", server_default=text("'DRAFT'")
    )
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default=text("1")
    )
    first_published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    retracted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    retraction_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('DRAFT', 'IN_REVIEW', 'APPROVED', 'PUBLISHED', 'ARCHIVED', 'RETRACTED')",
            name="ck_public_event_pages_status",
        ),
        CheckConstraint(
            "status <> 'PUBLISHED' OR last_published_at IS NOT NULL",
            name="ck_public_event_pages_published_requires_timestamp",
        ),
        CheckConstraint(
            "status <> 'RETRACTED' OR retracted_at IS NOT NULL",
            name="ck_public_event_pages_retracted_requires_timestamp",
        ),
        CheckConstraint(
            "version >= 1",
            name="ck_public_event_pages_version_ge_1",
        ),
        Index("uq_public_event_pages_slug", "slug", unique=True),
        Index("uq_public_event_pages_event_id", "event_id", unique=True),
        Index(
            "ix_public_event_pages_published_pub_id",
            text("last_published_at DESC"),
            text("id DESC"),
            postgresql_where=text("status = 'PUBLISHED'"),
        ),
        Index(
            "ix_public_event_pages_status_updated",
            "status",
            text("updated_at DESC"),
            text("id DESC"),
        ),
    )


class PublicEventPageRevisionModel(Base):
    """Immutable audit row written for every editorial transition.

    Append-only by convention.  The repository surface intentionally
    exposes no update or delete operation on this table; the CI lint
    or human review would catch any future repo method that did.
    """

    __tablename__ = "public_event_page_revisions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=gen_uuid)
    page_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("public_event_pages.id"), nullable=False
    )
    version_at_moment: Mapped[int] = mapped_column(Integer, nullable=False)
    from_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    to_status: Mapped[str] = mapped_column(String(20), nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    short_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    narrative_markdown: Mapped[str | None] = mapped_column(Text, nullable=True)
    editor_user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    transition_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    correction_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            "from_status IS NULL OR from_status IN "
            "('DRAFT', 'IN_REVIEW', 'APPROVED', 'PUBLISHED', 'ARCHIVED', 'RETRACTED')",
            name="ck_public_event_page_revisions_from_status",
        ),
        CheckConstraint(
            "to_status IN ('DRAFT', 'IN_REVIEW', 'APPROVED', 'PUBLISHED', 'ARCHIVED', 'RETRACTED')",
            name="ck_public_event_page_revisions_to_status",
        ),
        CheckConstraint(
            "version_at_moment >= 1",
            name="ck_public_event_page_revisions_version_ge_1",
        ),
        Index(
            "ix_public_event_page_revisions_page_version",
            "page_id",
            "version_at_moment",
            "id",
        ),
    )


# ── Search: full-text index over PUBLISHED public event pages (Phase 2) ──────


class SearchIndexEntryModel(Base):
    """Materialized search-index row for one PUBLISHED public event.

    The lifecycle is owned by the publication use cases: PUBLISH
    upserts a row, ARCHIVE/RETRACT remove it.  Editorial state
    changes that don't reach PUBLISHED do not touch this table, which
    is the structural enforcement of "search only indexes
    PUBLISHED".

    ``search_vector`` is built in the repository with weighted
    ``setweight`` calls (title=A, summary=B, structured facets=C,
    narrative=D) so query-time ``ts_rank_cd`` is deterministic and
    reproducible in tests.
    """

    __tablename__ = "search_index_entries"

    page_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("public_event_pages.id", ondelete="CASCADE"),
        primary_key=True,
    )
    slug: Mapped[str] = mapped_column(String(160), nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    short_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    operator: Mapped[str | None] = mapped_column(String(300), nullable=True)
    aircraft_type: Mapped[str | None] = mapped_column(String(300), nullable=True)
    country: Mapped[str | None] = mapped_column(String(300), nullable=True)
    event_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    fatalities_total: Mapped[int | None] = mapped_column(Integer, nullable=True)
    confidence_band: Mapped[str] = mapped_column(String(10), nullable=False)
    last_published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    indexed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, nullable=False
    )
    # tsvector is opaque to the application — the value is set via a
    # SQL expression at upsert time, not via Python.
    search_vector: Mapped[str] = mapped_column(TSVECTOR(), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "confidence_band IN ('high', 'medium', 'low', 'unknown')",
            name="ck_search_index_entries_confidence_band",
        ),
        Index(
            "ix_search_index_entries_search_vector",
            "search_vector",
            postgresql_using="gin",
        ),
        Index("ix_search_index_entries_operator", "operator"),
        Index("ix_search_index_entries_aircraft_type", "aircraft_type"),
        Index("ix_search_index_entries_event_date", "event_date"),
        Index(
            "ix_search_index_entries_pub_id",
            text("last_published_at DESC"),
            text("page_id DESC"),
        ),
    )


# ── Tenancy: tenants, memberships, sources, claims, overlays (Phase 5) ──────


class TenantModel(Base):
    """Directory row for a tenant organisation.

    ``slug`` is the URL-safe stable identifier; ``display_name`` is
    for UI.  ``is_active=False`` soft-deactivates without dropping
    membership / API key rows.
    """

    __tablename__ = "tenants"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=gen_uuid)
    slug: Mapped[str] = mapped_column(String(120), nullable=False)
    display_name: Mapped[str] = mapped_column(String(300), nullable=False)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, nullable=False
    )

    __table_args__ = (Index("uq_tenants_slug", "slug", unique=True),)


class TenantMembershipModel(Base):
    """A user's membership in a tenant.

    The authoritative answer to "may this user act inside this
    tenant".  Tenant API keys carry ``tenant_id`` + ``tenant_role``
    columns for fast auth, but the membership row is the canonical
    permission record — see ``require_tenant_membership``.
    """

    __tablename__ = "tenant_memberships"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=gen_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    tenant_role: Mapped[str] = mapped_column(String(20), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            "tenant_role IN ('OWNER', 'MEMBER', 'READ_ONLY')",
            name="ck_tenant_memberships_role",
        ),
        Index("uq_tenant_memberships_user", "tenant_id", "user_id", unique=True),
        Index("ix_tenant_memberships_user_id", "user_id"),
    )


class TenantSourceModel(Base):
    """Tenant-private source.

    Composite uniqueness on (tenant_id, name) so different tenants
    can both have a source called "Operations" without colliding.
    """

    __tablename__ = "tenant_sources"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=gen_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(300), nullable=False)
    kind: Mapped[str] = mapped_column(String(50), nullable=False)
    reliability_tier: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default=text("1")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, nullable=False
    )

    __table_args__ = (
        Index(
            "uq_tenant_sources_tenant_name",
            "tenant_id",
            "name",
            unique=True,
        ),
    )


class TenantIngestionRunModel(Base):
    __tablename__ = "tenant_ingestion_runs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=gen_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    tenant_source_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenant_sources.id", ondelete="CASCADE"),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="running", server_default=text("'running'")
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint(
            "status IN ('running', 'succeeded', 'failed')",
            name="ck_tenant_ingestion_runs_status",
        ),
        Index("ix_tenant_ingestion_runs_tenant", "tenant_id"),
    )


class TenantClaimModel(Base):
    """Tenant-private claim about a public event.

    The event_id FK points at the public ``accident_events`` table so
    the tenant view is anchored to public ground truth; the rest of
    the columns reference tenant-scoped parents.
    """

    __tablename__ = "tenant_claims"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=gen_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    event_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("accident_events.id"), nullable=False
    )
    tenant_source_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenant_sources.id", ondelete="CASCADE"),
        nullable=False,
    )
    tenant_ingestion_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenant_ingestion_runs.id", ondelete="CASCADE"),
        nullable=True,
    )
    field_name: Mapped[str] = mapped_column(String(200), nullable=False)
    field_value: Mapped[Any] = mapped_column(JSONB, nullable=True)
    # Phase 6: claim_kind discriminates FOQA / ASAP-derived / OTHER
    # structured claims so a single table carries them all.
    claim_kind: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="OTHER",
        server_default=text("'OTHER'"),
    )
    # Phase 6: confidence is the tenant's own confidence in the claim,
    # 0..1; nullable so legacy rows survive.
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            "claim_kind IN ('FOQA', 'ASAP', 'OTHER')",
            name="ck_tenant_claims_claim_kind",
        ),
        CheckConstraint(
            "confidence IS NULL OR (confidence >= 0.0 AND confidence <= 1.0)",
            name="ck_tenant_claims_confidence_range",
        ),
        Index("ix_tenant_claims_tenant_event", "tenant_id", "event_id"),
        Index("ix_tenant_claims_tenant_field", "tenant_id", "field_name"),
        Index(
            "ix_tenant_claims_tenant_event_kind",
            "tenant_id",
            "event_id",
            "claim_kind",
        ),
    )


class TenantEventOverlayModel(Base):
    """One row per (tenant, event) — a tenant's overlay on a public event.

    Single-row-per-(tenant,event) keeps the overlay a coherent unit
    of edit rather than a stream.  ``overlay_fields`` is JSONB so
    tenants can attach arbitrary structured private annotations.
    """

    __tablename__ = "tenant_event_overlays"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=gen_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    event_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("accident_events.id"), nullable=False
    )
    notes_markdown: Mapped[str | None] = mapped_column(Text, nullable=True)
    overlay_fields: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, nullable=False
    )

    __table_args__ = (
        Index(
            "uq_tenant_event_overlays_tenant_event",
            "tenant_id",
            "event_id",
            unique=True,
        ),
    )


# ── Maps: geospatial index over PUBLISHED public event pages (Phase 3) ──────


class MapIndexEntryModel(Base):
    """Materialised geo-index row for one PUBLISHED public event.

    Lifecycle matches the Phase 2 search index: PUBLISH upserts a
    row, ARCHIVE/RETRACT delete it.  Editorial-state changes that do
    not reach PUBLISHED never touch this table.

    The ``geom`` column is ``geography(Point, 4326)`` at the database
    level (set by migration 039's ``ALTER COLUMN``).  Application
    code never reads this column directly through the ORM — all
    spatial expressions go through PostGIS SQL.  We model it as a
    plain ``Text`` column in the ORM so SQLAlchemy can introspect
    the table without requiring GeoAlchemy2 as a build dependency.
    """

    __tablename__ = "map_index_entries"

    page_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("public_event_pages.id", ondelete="CASCADE"),
        primary_key=True,
    )
    slug: Mapped[str] = mapped_column(String(160), nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    operator: Mapped[str | None] = mapped_column(String(300), nullable=True)
    aircraft_type: Mapped[str | None] = mapped_column(String(300), nullable=True)
    country: Mapped[str | None] = mapped_column(String(300), nullable=True)
    event_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    fatalities_total: Mapped[int | None] = mapped_column(Integer, nullable=True)
    confidence_band: Mapped[str] = mapped_column(String(10), nullable=False)
    last_published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    indexed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, nullable=False
    )
    # ``geom`` is opaque to the ORM (see class docstring).  Declared
    # here only so SQLAlchemy knows the column exists; reads/writes
    # use raw PostGIS SQL.
    geom: Mapped[Any] = mapped_column(Text, nullable=False)

    __table_args__ = (
        CheckConstraint(
            "confidence_band IN ('high', 'medium', 'low', 'unknown')",
            name="ck_map_index_entries_confidence_band",
        ),
        Index("ix_map_index_entries_operator", "operator"),
        Index("ix_map_index_entries_aircraft_type", "aircraft_type"),
        Index("ix_map_index_entries_event_date", "event_date"),
        Index(
            "ix_map_index_entries_pub_id",
            text("last_published_at DESC"),
            text("page_id DESC"),
        ),
    )


# ── CMS: glossary, methodology, changelog (Phase 10) ────────────────────────


class GlossaryTermModel(Base):
    __tablename__ = "glossary_terms"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=gen_uuid)
    term: Mapped[str] = mapped_column(String(120), nullable=False)
    display_term: Mapped[str] = mapped_column(String(200), nullable=False)
    body_markdown: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="DRAFT", server_default=text("'DRAFT'")
    )
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default=text("1")
    )
    first_published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    retraction_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('DRAFT', 'IN_REVIEW', 'APPROVED', 'PUBLISHED', 'ARCHIVED', 'RETRACTED')",
            name="ck_glossary_terms_status",
        ),
        Index("uq_glossary_terms_term", "term", unique=True),
    )


class GlossaryTermRevisionModel(Base):
    __tablename__ = "glossary_term_revisions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=gen_uuid)
    term_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("glossary_terms.id", ondelete="CASCADE"),
        nullable=False,
    )
    from_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    to_status: Mapped[str] = mapped_column(String(20), nullable=False)
    version_at_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    editor_user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    transition_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    correction_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, nullable=False
    )

    __table_args__ = (
        Index(
            "ix_glossary_term_revisions_term",
            "term_id",
            "created_at",
        ),
    )


class MethodologyPageModel(Base):
    __tablename__ = "methodology_pages"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=gen_uuid)
    slug: Mapped[str] = mapped_column(String(160), nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    section: Mapped[str] = mapped_column(String(100), nullable=False)
    section_order: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    body_markdown: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="DRAFT", server_default=text("'DRAFT'")
    )
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default=text("1")
    )
    first_published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    retraction_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('DRAFT', 'IN_REVIEW', 'APPROVED', 'PUBLISHED', 'ARCHIVED', 'RETRACTED')",
            name="ck_methodology_pages_status",
        ),
        Index("uq_methodology_pages_slug", "slug", unique=True),
        Index(
            "ix_methodology_pages_section_order",
            "section",
            "section_order",
            "title",
        ),
    )


class MethodologyPageRevisionModel(Base):
    __tablename__ = "methodology_page_revisions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=gen_uuid)
    page_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("methodology_pages.id", ondelete="CASCADE"),
        nullable=False,
    )
    from_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    to_status: Mapped[str] = mapped_column(String(20), nullable=False)
    version_at_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    editor_user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    transition_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    correction_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, nullable=False
    )

    __table_args__ = (
        Index(
            "ix_methodology_page_revisions_page",
            "page_id",
            "created_at",
        ),
    )


class ChangelogEntryModel(Base):
    __tablename__ = "changelog_entries"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=gen_uuid)
    slug: Mapped[str] = mapped_column(String(160), nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    effective_date: Mapped[date] = mapped_column(Date, nullable=False)
    body_markdown: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="DRAFT", server_default=text("'DRAFT'")
    )
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default=text("1")
    )
    first_published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    retraction_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('DRAFT', 'IN_REVIEW', 'APPROVED', 'PUBLISHED', 'ARCHIVED', 'RETRACTED')",
            name="ck_changelog_entries_status",
        ),
        Index("uq_changelog_entries_slug", "slug", unique=True),
    )


class ChangelogEntryRevisionModel(Base):
    __tablename__ = "changelog_entry_revisions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=gen_uuid)
    entry_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("changelog_entries.id", ondelete="CASCADE"),
        nullable=False,
    )
    from_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    to_status: Mapped[str] = mapped_column(String(20), nullable=False)
    version_at_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    editor_user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    transition_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    correction_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, nullable=False
    )

    __table_args__ = (
        Index(
            "ix_changelog_entry_revisions_entry",
            "entry_id",
            "created_at",
        ),
    )


# ── Phase 6: ASAP narrative reports + event associations ────────────────────


class TenantSafetyReportModel(Base):
    """Tenant-private ASAP-style narrative safety report.

    Hard invariant maintained at the router layer: this table is
    never read by any public-side surface.  Phase 6 enforces this by
    routing every safety-report endpoint under the tenant prefix;
    the public router never imports the use cases that read this
    table.
    """

    __tablename__ = "tenant_safety_reports"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=gen_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )
    report_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    narrative_markdown: Mapped[str] = mapped_column(Text, nullable=False)
    # The operator attests at submission time that the narrative has
    # been deidentified; Atlas's PII scrubber is a second line of
    # defence, not the primary one.
    deidentified_attested: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    external_report_ref: Mapped[str | None] = mapped_column(String(200), nullable=True)
    submitter_user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            "report_kind IN ('FOQA', 'ASAP', 'OTHER')",
            name="ck_tenant_safety_reports_kind",
        ),
        Index(
            "ix_tenant_safety_reports_tenant_created",
            "tenant_id",
            text("created_at DESC"),
        ),
    )


class TenantEventAssociationModel(Base):
    """Editorial association between tenant evidence and a public event.

    Exactly one of (``claim_id``, ``safety_report_id``) is non-null;
    enforced both by the schema CHECK and by the entity's
    ``model_post_init`` validator.
    """

    __tablename__ = "tenant_event_associations"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=gen_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )
    event_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("accident_events.id"),
        nullable=False,
    )
    claim_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenant_claims.id", ondelete="CASCADE"),
        nullable=True,
    )
    safety_report_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenant_safety_reports.id", ondelete="CASCADE"),
        nullable=True,
    )
    association_kind: Mapped[str] = mapped_column(String(30), nullable=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by_user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            "association_kind IN ('RELATED', 'CONTRIBUTED_TO', 'PRECEDED')",
            name="ck_tenant_event_associations_kind",
        ),
        CheckConstraint(
            "(claim_id IS NOT NULL)::int + (safety_report_id IS NOT NULL)::int = 1",
            name="ck_tenant_event_associations_exactly_one_source",
        ),
        Index(
            "ix_tenant_event_associations_tenant_event",
            "tenant_id",
            "event_id",
        ),
        Index("ix_tenant_event_associations_claim", "claim_id"),
        Index(
            "ix_tenant_event_associations_safety_report",
            "safety_report_id",
        ),
    )


# ── Phase 4: HFACS taxonomy + attributions + SHELO ──────────────────────────


class HfacsCategoryModel(Base):
    __tablename__ = "hfacs_categories"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=gen_uuid)
    tier_code: Mapped[str] = mapped_column(String(4), nullable=False)
    code: Mapped[str] = mapped_column(String(20), nullable=False)
    tier: Mapped[str] = mapped_column(String(20), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    is_custom: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            "tier IN ('ORGANIZATIONAL', 'SUPERVISION', 'PRECONDITIONS', 'UNSAFE_ACTS')",
            name="ck_hfacs_categories_tier",
        ),
        Index("uq_hfacs_categories_code", "code", unique=True),
    )


class HfacsSubcategoryModel(Base):
    __tablename__ = "hfacs_subcategories"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=gen_uuid)
    category_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("hfacs_categories.id"),
        nullable=False,
    )
    code: Mapped[str] = mapped_column(String(40), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_custom: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, nullable=False
    )

    __table_args__ = (
        Index("uq_hfacs_subcategories_code", "code", unique=True),
        Index("ix_hfacs_subcategories_category", "category_id"),
    )


class EventHfacsAttributionModel(Base):
    __tablename__ = "event_hfacs_attributions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=gen_uuid)
    event_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("accident_events.id", ondelete="CASCADE"),
        nullable=False,
    )
    category_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("hfacs_categories.id"),
        nullable=False,
    )
    subcategory_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("hfacs_subcategories.id"),
        nullable=True,
    )
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    editor_user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default=text("1")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            "confidence >= 0.0 AND confidence <= 1.0",
            name="ck_event_hfacs_attributions_confidence_range",
        ),
        Index(
            "ix_event_hfacs_attributions_event",
            "event_id",
        ),
        # The COALESCE-based partial unique index from the migration
        # is not expressible declaratively here; it's created via raw
        # SQL in the migration.  The consistency test pinning
        # ORM↔migration alignment treats raw-SQL indexes as opaque.
    )


class SheloFactorModel(Base):
    __tablename__ = "shelo_factors"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=gen_uuid)
    event_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("accident_events.id", ondelete="CASCADE"),
        nullable=False,
    )
    factor_class: Mapped[str] = mapped_column(String(20), nullable=False)
    label: Mapped[str] = mapped_column(String(300), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    editor_user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default=text("1")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            "factor_class IN ('SOFTWARE', 'HARDWARE', 'ENVIRONMENT', 'LIVEWARE', 'OTHER')",
            name="ck_shelo_factors_class",
        ),
        Index("ix_shelo_factors_event", "event_id"),
    )


class SheloFactorInteractionModel(Base):
    __tablename__ = "shelo_factor_interactions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=gen_uuid)
    event_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("accident_events.id", ondelete="CASCADE"),
        nullable=False,
    )
    source_factor_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("shelo_factors.id", ondelete="CASCADE"),
        nullable=False,
    )
    target_factor_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("shelo_factors.id", ondelete="CASCADE"),
        nullable=False,
    )
    interaction_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    editor_user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            "interaction_kind IN ('PRECONDITION', 'AGGRAVATED', 'MITIGATED', 'MASKED')",
            name="ck_shelo_factor_interactions_kind",
        ),
        CheckConstraint(
            "source_factor_id <> target_factor_id",
            name="ck_shelo_factor_interactions_no_self_loop",
        ),
        Index(
            "uq_shelo_factor_interactions_natural",
            "event_id",
            "source_factor_id",
            "target_factor_id",
            "interaction_kind",
            unique=True,
        ),
        Index("ix_shelo_factor_interactions_event", "event_id"),
    )


# ── Phase 7: NL search log + saved queries ──────────────────────────────────


class NlQueryLogModel(Base):
    __tablename__ = "nl_query_log"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=gen_uuid)
    raw_query: Mapped[str] = mapped_column(Text, nullable=False)
    query_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    parsed_filters: Mapped[Any] = mapped_column(JSONB, nullable=False)
    result_count: Mapped[int] = mapped_column(Integer, nullable=False)
    parser_confidence: Mapped[float] = mapped_column(Float, nullable=False)
    hour_bucket: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            "parser_confidence >= 0.0 AND parser_confidence <= 1.0",
            name="ck_nl_query_log_confidence_range",
        ),
        CheckConstraint(
            "result_count >= 0",
            name="ck_nl_query_log_result_count_nonneg",
        ),
        Index("ix_nl_query_log_hour_bucket", "hour_bucket"),
        Index("ix_nl_query_log_query_hash", "query_hash"),
    )


class SavedNlQueryModel(Base):
    __tablename__ = "saved_nl_queries"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=gen_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    label: Mapped[str] = mapped_column(String(200), nullable=False)
    raw_query: Mapped[str] = mapped_column(Text, nullable=False)
    frozen_filters: Mapped[Any] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, nullable=False
    )

    __table_args__ = (Index("ix_saved_nl_queries_user", "user_id"),)


# ── Phase 8: metering ──────────────────────────────────────────────────────


class UsageEventModel(Base):
    __tablename__ = "usage_events"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=gen_uuid)
    metric_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=True,
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    resource_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            "metric_kind IN ('TENANT_CLAIM_INGESTED', "
            "'TENANT_REPORT_FILED', "
            "'TENANT_INGESTION_RUN_COMPLETED', "
            "'NL_QUERY_EXECUTED', "
            "'HFACS_ATTRIBUTION_CREATED', 'ECHO_CROSSREF_RUN')",
            name="ck_usage_events_metric_kind",
        ),
        Index(
            "ix_usage_events_tenant_recorded_at",
            "tenant_id",
            "recorded_at",
        ),
        Index(
            "ix_usage_events_metric_recorded_at",
            "metric_kind",
            "recorded_at",
        ),
        Index("ix_usage_events_resource_id", "resource_id"),
    )


class UsageDailyRollupModel(Base):
    __tablename__ = "usage_daily_rollups"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=gen_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    metric_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    day: Mapped[date] = mapped_column(Date, nullable=False)
    count: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default=text("0")
    )
    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            "metric_kind IN ('TENANT_CLAIM_INGESTED', "
            "'TENANT_REPORT_FILED', "
            "'TENANT_INGESTION_RUN_COMPLETED', "
            "'NL_QUERY_EXECUTED', "
            "'HFACS_ATTRIBUTION_CREATED', 'ECHO_CROSSREF_RUN')",
            name="ck_usage_daily_rollups_metric_kind",
        ),
        CheckConstraint(
            "count >= 0",
            name="ck_usage_daily_rollups_count_nonneg",
        ),
        UniqueConstraint(
            "tenant_id",
            "metric_kind",
            "day",
            name="uq_usage_daily_rollups_natural",
        ),
        Index("ix_usage_daily_rollups_day", "day"),
    )


class TenantCrossrefResultModel(Base):
    """Tenant-private Echo cross-reference result set.

    No public surface reads this table — the invariant is enforced at
    the router layer (all reads go through the tenant-prefix router).
    RLS policy ``tenant_isolation`` (migration 046) provides the DB-level
    guarantee.  ``matches_json`` is written once on transition to COMPLETE
    and never updated; re-runs create a new row.
    """

    __tablename__ = "tenant_crossref_results"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=gen_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )
    safety_report_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenant_safety_reports.id", ondelete="CASCADE"),
        nullable=True,
    )
    claim_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenant_claims.id", ondelete="CASCADE"),
        nullable=True,
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default=text("'PENDING'")
    )
    matches_json: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    matcher_config_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    match_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_detail: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        CheckConstraint(
            "status IN ('PENDING', 'COMPLETE', 'FAILED')",
            name="ck_tenant_crossref_results_status",
        ),
        CheckConstraint(
            "(safety_report_id IS NOT NULL)::int + (claim_id IS NOT NULL)::int = 1",
            name="ck_tenant_crossref_results_source_xor",
        ),
        CheckConstraint(
            "match_count >= 0",
            name="ck_tenant_crossref_results_match_count_nonneg",
        ),
        # Fast lookup: "all results for this tenant's report, newest first".
        Index(
            "ix_tenant_crossref_results_tenant_report",
            "tenant_id",
            "safety_report_id",
            postgresql_where=text("safety_report_id IS NOT NULL"),
        ),
        Index(
            "ix_tenant_crossref_results_tenant_requested",
            "tenant_id",
            text("requested_at DESC"),
        ),
    )
