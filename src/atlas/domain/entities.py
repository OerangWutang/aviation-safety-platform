from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_serializer,
    field_validator,
    model_validator,
)

from atlas.domain.constants import (
    DISPUTED_MARKER,
    MAX_REGISTRATION_ALIASES,
    DisputedType,
    replace_disputed,
)
from atlas.domain.enums import (
    ArgusEvidenceType,
    ArgusReviewDecision,
    ArgusSeverity,
    ArgusSignalStatus,
    ArgusSignalType,
    ChronosSequenceReviewStatus,
    ChronosTimelineEventType,
    ChronosTimestampPrecision,
    ClaimType,
    ConflictModifierReason,
    ConflictStatus,
    DuplicateReviewStatus,
    HermesChangeType,
    HermesDocumentContentType,
    HermesFetchJobStatus,
    HermesSourceType,
    HermesTargetStatus,
    ModifierType,
    OrionEntityType,
    OrionRelationshipType,
    OrionReviewStatus,
    OutboxStatus,
    SourceKind,
)
from atlas.domain.exceptions import ClaimNotInConflictError, ConflictAlreadyResolvedError
from atlas.domain.utils import utc_now


class DomainModel(BaseModel):
    model_config = ConfigDict(
        from_attributes=True,
        arbitrary_types_allowed=True,
        extra="ignore",
        json_encoders={DisputedType: lambda _: DISPUTED_MARKER},
    )


class Source(DomainModel):
    id: UUID = Field(default_factory=uuid4)
    name: str
    kind: SourceKind
    reliability_tier: int = Field(default=1, ge=1)  # lower = more trusted; must be >= 1
    # Durable source-specific field-name mapping.  Keys are raw source field
    # names, values are Atlas canonical field names.  This lets ingestion map
    # ambiguous names (for example, "date") only for sources where that
    # meaning is known, instead of relying on dangerous global aliases.
    field_mapping_json: dict[str, str] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)


class RawSnapshot(DomainModel):
    id: UUID = Field(default_factory=uuid4)
    source_id: UUID
    ingestion_run_id: UUID
    # Backward-compatible column historically named payload_hash.  New ingestions
    # store the full submission hash here until all callers have migrated to the
    # explicit submission_hash column below.
    payload_hash: str
    payload_json: dict[str, Any]
    schema_version: int = 1
    captured_at: datetime
    created_at: datetime = Field(default_factory=utc_now)
    # Stable identifier assigned by the source system (e.g. NTSB accident number,
    # IATA incident ID). When provided, enables re-ingestion of updated data for
    # the same record and cross-source event matching. NULL for sources that do
    # not supply stable record identifiers.
    source_record_id: str | None = None
    # Explicit hash/audit fields introduced after payload_hash was repurposed
    # for full-submission idempotency.  Nullable for rows created before the
    # migration; new rows should populate all three.
    raw_payload_hash: str | None = None
    submission_hash: str | None = None
    submission_fingerprint_json: dict[str, Any] | None = None
    # Durable result snapshot used for exact idempotent replay.  This prevents
    # replays from inferring event/review metadata by scanning claims, which can
    # become ambiguous after duplicate-event merges copy/supersede claims.
    ingestion_result_json: dict[str, Any] | None = None


class IngestionRun(DomainModel):
    id: UUID = Field(default_factory=uuid4)
    source_id: UUID
    status: str = "running"
    started_at: datetime = Field(default_factory=utc_now)
    finished_at: datetime | None = None


class AccidentEvent(DomainModel):
    id: UUID = Field(default_factory=uuid4)
    created_at: datetime = Field(default_factory=utc_now)
    merged_into_event_id: UUID | None = None

    @property
    def is_merged(self) -> bool:
        return self.merged_into_event_id is not None


class Claim(DomainModel):
    id: UUID = Field(default_factory=uuid4)
    event_id: UUID
    source_id: UUID
    raw_snapshot_id: UUID | None = None
    field_name: str
    field_value: Any
    claim_type: ClaimType = ClaimType.RAW
    created_at: datetime = Field(default_factory=utc_now)
    created_by: UUID | None = None
    superseded_by_claim_id: UUID | None = None

    def can_win(self) -> bool:
        return self.claim_type in (
            ClaimType.MANUAL_OVERRIDE,
            ClaimType.CONFIRMED,
            ClaimType.RAW,
        )

    @property
    def is_active(self) -> bool:
        """Return whether the claim still participates in current evidence."""
        return self.claim_type.value in ClaimType.active_values()

    def supersede(self, by_claim_id: UUID) -> None:
        self.claim_type = ClaimType.SUPERSEDED
        self.superseded_by_claim_id = by_claim_id


class ClaimHistory(DomainModel):
    id: UUID = Field(default_factory=uuid4)
    claim_id: UUID
    # Denormalized for event-level provenance keyset pagination.  This is a
    # hard domain requirement so repositories never need to query parent rows
    # during the same Unit-of-Work transaction.
    event_id: UUID
    from_value: Any | None = None
    to_value: Any | None = None
    from_claim_type: ClaimType | None = None
    to_claim_type: ClaimType
    action: str = "updated"
    reason: str
    modifier_type: ModifierType
    modifier_id: UUID | None = None
    created_at: datetime = Field(default_factory=utc_now)


class ClaimConflict(DomainModel):
    """A disagreement between two or more sources about the value of a field.

    Note on ``last_modified_note``
    ------------------------------
    This field is ``None`` when a conflict is first created.  The *reason* for
    the initial creation is recorded in the first ``ConflictActivityLogEntry``
    for this conflict (``reason="Initial conflict detected"``), not on the
    conflict row itself.  This is intentional: the conflict row captures the
    *current state* of the last modification, which on creation is simply the
    INITIAL state with no curator note.

    Callers (API responses, UI) should read the activity log for the full
    audit history of why a conflict was opened.
    """

    id: UUID = Field(default_factory=uuid4)
    event_id: UUID
    field_name: str
    status: ConflictStatus = ConflictStatus.OPEN
    version: int = 1
    last_modified_reason: ConflictModifierReason = ConflictModifierReason.INITIAL
    last_modified_note: str | None = None  # null on creation; see docstring above
    winning_claim_id: UUID | None = None
    resolved_at: datetime | None = None
    resolved_by: UUID | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    claim_ids: list[UUID] = Field(default_factory=list)

    @model_validator(mode="after")
    def check_resolved_fields(self) -> ClaimConflict:
        if (
            self.status == ConflictStatus.RESOLVED
            and self.winning_claim_id is None
            and self.last_modified_reason != ConflictModifierReason.SYSTEM_AUTO_CLOSED
        ):
            raise ValueError("Resolved conflict must have a winning_claim_id")
        return self

    def resolve(self, winning_claim_id: UUID, resolved_by: UUID, reason: str = "") -> None:
        if self.status != ConflictStatus.OPEN:
            raise ConflictAlreadyResolvedError(self.id)
        if self.claim_ids and winning_claim_id not in self.claim_ids:
            raise ClaimNotInConflictError(
                f"Claim {winning_claim_id} is not part of conflict {self.id}"
            )
        self.status = ConflictStatus.RESOLVED
        self.winning_claim_id = winning_claim_id
        self.resolved_by = resolved_by
        self.resolved_at = utc_now()
        self.version += 1
        self.last_modified_reason = ConflictModifierReason.USER_RESOLVED
        # Preserve the curator-supplied reason on the conflict row itself so that
        # downstream UIs see the same justification that lives on the activity log
        # and claim_history rows. Trim to the column width (255) defensively.
        self.last_modified_note = (reason or None) and reason[:255]
        self.updated_at = utc_now()

    def reopen_for_new_evidence(self, reason: str = "New evidence") -> None:
        self.status = ConflictStatus.OPEN
        self.winning_claim_id = None
        self.resolved_by = None
        self.resolved_at = None
        self.version += 1
        self.last_modified_reason = ConflictModifierReason.NEW_EVIDENCE
        self.last_modified_note = reason[:255] if reason else None
        self.updated_at = utc_now()

    def reopen_by_user(self, reason: str = "Manual reopen") -> None:
        """Reopen a previously resolved conflict at the request of a curator.

        Mirrors ``reopen_for_new_evidence`` but uses ``USER_REOPENED`` so the
        activity log distinguishes manual reopens from ingestion-driven ones.
        """
        if self.status != ConflictStatus.RESOLVED:
            raise ValueError("Only resolved conflicts can be reopened")
        self.status = ConflictStatus.OPEN
        self.winning_claim_id = None
        self.resolved_by = None
        self.resolved_at = None
        self.version += 1
        self.last_modified_reason = ConflictModifierReason.USER_REOPENED
        self.last_modified_note = reason[:255] if reason else None
        self.updated_at = utc_now()

    def add_claim_id(self, claim_id: UUID) -> None:
        """Append a claim id using replacement to avoid shared-list bugs."""
        if claim_id not in self.claim_ids:
            self.claim_ids = [*self.claim_ids, claim_id]

    def add_evidence(
        self, reason: ConflictModifierReason = ConflictModifierReason.NEW_EVIDENCE
    ) -> None:
        self.version += 1
        self.last_modified_reason = reason
        self.updated_at = utc_now()


class ConflictActivityLogEntry(DomainModel):
    id: UUID = Field(default_factory=uuid4)
    conflict_id: UUID
    # Denormalized for event-level provenance keyset pagination.  This is a
    # hard domain requirement so repositories never need to query parent rows
    # during the same Unit-of-Work transaction.
    event_id: UUID
    sequence: int
    from_status: ConflictStatus | None = None
    to_status: ConflictStatus
    modifier_type: ModifierType
    modifier_id: UUID | None = None
    reason: str
    version_at_moment: int
    claims_snapshot: dict[str, Any] | None = None
    created_at: datetime = Field(default_factory=utc_now)


class ProjectedAccidentRecord(DomainModel):
    event_id: UUID
    projection_version: int = 0
    fields: dict[str, Any]
    completeness_score: float = 0.0
    unresolved_conflict_fields: list[str] = Field(default_factory=list)
    updated_at: datetime = Field(default_factory=utc_now)

    @field_serializer("fields")
    def serialize_fields(self, fields: dict[str, Any]) -> dict[str, Any]:
        result = replace_disputed(fields)
        # ``replace_disputed`` always returns a dict when given a dict input.
        # Cast for mypy's benefit.
        assert isinstance(result, dict)
        return result


class AccidentProjectionHistory(DomainModel):
    id: UUID = Field(default_factory=uuid4)
    accident_event_id: UUID
    projection_version: int
    caused_by_conflict_id: UUID | None = None
    caused_by_ingestion_run_id: UUID | None = None
    caused_by_outbox_event_id: UUID | None = None
    projected_record_snapshot: dict[str, Any]
    projected_record_hash: str
    changed_fields: list[str] | None = None
    created_at: datetime = Field(default_factory=utc_now)


class OutboxEvent(DomainModel):
    id: UUID = Field(default_factory=uuid4)
    event_type: str
    aggregate_id: UUID
    payload: dict[str, Any]
    status: OutboxStatus = OutboxStatus.PENDING
    attempt_count: int = 0
    locked_at: datetime | None = None
    locked_by: str | None = None
    last_error: str | None = None
    next_attempt_at: datetime | None = None
    created_at: datetime = Field(default_factory=utc_now)
    processed_at: datetime | None = None


class ArchiveManifest(DomainModel):
    id: UUID = Field(default_factory=uuid4)
    object_path: str
    date_range_start: datetime | None = None
    date_range_end: datetime | None = None
    source_system: str
    row_count: int
    checksum: str
    schema_version: int
    created_at: datetime = Field(default_factory=utc_now)
    created_by_process_id: str | None = None


class EventIdentityIndex(DomainModel):
    """Synchronous identity record written in the same transaction as ingestion.

    This is the substrate that ``_resolve_or_create_event`` queries.  It must
    be written in the ingestion transaction - not asynchronously by the outbox
    worker - because the outbox worker may not have run yet when the next
    ingestion arrives for the same accident.

    Without this, the event matcher queries ``projected_accident_records`` which
    is only populated after the outbox worker fires.  Two rapid ingestions of
    the same accident (Source A then Source B before A's projection is built)
    both find an empty projection table, both create new events, and no
    duplicate review is queued.

    All string fields are stored in normalised form (lowercase, stripped) so
    the matching query is a cheap index scan rather than a full-table string
    comparison.  The ``fields`` property adapts this to a plain ``dict`` so
    ``EventMatcher`` can score it without knowing the concrete type.
    """

    event_id: UUID
    event_date_norm: str | None = None  # YYYY-MM-DD
    registration_norm: str | None = None  # lowercase, no hyphens/spaces (primary)
    operator_norm: str | None = None  # lowercase, stripped
    location_norm: str | None = None  # lowercase, stripped
    aircraft_type_norm: str | None = None  # lowercase, stripped
    # All source_record_ids that have been attached to this event so far.
    # Written by every ingestion that targets this event, so the list grows
    # as more sources are ingested.
    source_record_ids: list[str] = Field(default_factory=list)
    # Bounded set of normalised registrations asserted for this event.  The
    # scalar ``registration_norm`` remains the primary/current registration;
    # this list is only a low-confidence alias substrate.  It is capped to avoid
    # review-queue amplification from bad source mappings or malicious payloads
    # that submit many unrelated tail numbers for one event.
    registration_norms: list[str] = Field(default_factory=list)
    updated_at: datetime = Field(default_factory=utc_now)

    @field_validator("registration_norms")
    @classmethod
    def cap_registration_norms(cls, value: list[str]) -> list[str]:
        """Keep only the most recent unique aliases within the safety cap.

        Repository upserts append newer aliases to the end of the JSON array.
        Retaining the right-most unique values preserves recent/current evidence
        while bounding the number of historical aliases that can trigger future
        duplicate-review candidates.
        """
        seen: set[str] = set()
        capped_reversed: list[str] = []
        for item in reversed(value or []):
            if item in seen:
                continue
            seen.add(item)
            capped_reversed.append(item)
            if len(capped_reversed) >= MAX_REGISTRATION_ALIASES:
                break
        return list(reversed(capped_reversed))

    @property
    def fields(self) -> dict[str, Any]:
        """Adapt to the ``{field_name: value}`` dict that EventMatcher expects.

        The matcher scores against ``candidate.fields`` so it works against
        both ``ProjectedAccidentRecord`` and ``EventIdentityIndex`` without
        knowing which it has.

        Registration scoring strategy
        ------------------------------
        ``registration`` is always the **primary** scalar (``registration_norm``,
        the most recently ingested value) and is scored at full weight (1.0) by
        ``_field_score``.  This lets a source reporting the current registration
        reach the HIGH_CONFIDENCE threshold and be auto-attached.

        Historical aliases (registrations that were once asserted but are no
        longer the primary) are exposed separately as ``registration_norms``.
        ``score_match`` checks this key and scores a match at **half weight**
        (0.5 x 0.45 = 0.225), putting the total score into the
        UNCERTAIN_LOW..HIGH_CONFIDENCE range so a duplicate review is queued
        rather than an auto-attach.

        This prevents a corrected-away or historically-conflicting registration
        from granting full auto-attach power to future ingestions.
        """
        result: dict[str, Any] = {}
        if self.event_date_norm:
            result["event_date"] = self.event_date_norm
        # Primary registration: always scalar, full-weight scoring.
        if self.registration_norm:
            result["registration"] = self.registration_norm
        if self.operator_norm:
            result["operator"] = self.operator_norm
        if self.location_norm:
            result["location"] = self.location_norm
        if self.aircraft_type_norm:
            result["aircraft_type"] = self.aircraft_type_norm
        # Historical aliases (everything except the current primary), already
        # bounded by ``MAX_REGISTRATION_ALIASES`` at the entity/repository layers.
        # Exposed under a separate key so score_match can apply half weight,
        # keeping historical matches in the review band not the attach band.
        historical = [r for r in self.registration_norms if r != self.registration_norm]
        if historical:
            result["registration_norms"] = historical
        return result


class PendingDuplicateReview(DomainModel):
    """Two candidate events that may describe the same real-world accident.

    Created by the ingestion pipeline when it detects a medium-confidence
    match between an incoming source record and an existing event.  A curator
    then either confirms the match (triggering a merge) or rejects it
    (recording a permanent exclusion so the pair is not surfaced again).

    High-confidence matches bypass this table and are merged automatically;
    their existence is recorded here with status=AUTO_MERGED for audit purposes.
    """

    id: UUID = Field(default_factory=uuid4)
    # event_id_a is the pre-existing event; event_id_b is the newly ingested one.
    event_id_a: UUID
    event_id_b: UUID
    status: DuplicateReviewStatus
    # Normalised 0-1 score produced by EventMatcher. Higher = more similar.
    match_score: float
    # Which fields drove the match (e.g. ["event_date", "registration"]).
    matched_fields: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)
    resolved_at: datetime | None = None
    resolved_by: UUID | None = None
    resolution_note: str | None = None


# ── Orion Entity Intelligence Layer ──────────────────────────────────────────


class OrionEntity(DomainModel):
    id: UUID = Field(default_factory=uuid4)
    entity_type: OrionEntityType
    canonical_name: str
    status: str = "ACTIVE"
    confidence: float = 1.0
    merged_into_entity_id: UUID | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class OrionEntityIdentifier(DomainModel):
    id: UUID = Field(default_factory=uuid4)
    entity_id: UUID
    # Denormalized from OrionEntity so the database can enforce that one
    # active strong identifier maps to one canonical entity per entity type.
    entity_type: OrionEntityType | None = None
    identifier_type: str
    identifier_value: str
    normalized_value: str
    source_claim_id: UUID | None = None
    raw_snapshot_id: UUID | None = None
    confidence: float = 1.0
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    created_at: datetime = Field(default_factory=utc_now)


class OrionRelationship(DomainModel):
    id: UUID = Field(default_factory=uuid4)
    subject_entity_id: UUID | None = None
    relationship_type: OrionRelationshipType
    object_entity_id: UUID
    accident_event_id: UUID
    source_claim_id: UUID | None = None
    raw_snapshot_id: UUID | None = None
    confidence: float = 1.0
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    created_at: datetime = Field(default_factory=utc_now)


class OrionEntityClaimLink(DomainModel):
    id: UUID = Field(default_factory=uuid4)
    entity_id: UUID
    claim_id: UUID
    raw_snapshot_id: UUID | None = None
    source_id: UUID
    accident_event_id: UUID
    confidence: float = 1.0
    created_at: datetime = Field(default_factory=utc_now)


class OrionEntityReview(DomainModel):
    id: UUID = Field(default_factory=uuid4)
    candidate_entity_id_a: UUID
    candidate_entity_id_b: UUID
    entity_type: OrionEntityType
    match_score: float
    matched_identifiers: list[str] = Field(default_factory=list)
    status: OrionReviewStatus = OrionReviewStatus.PENDING
    created_at: datetime = Field(default_factory=utc_now)
    resolved_at: datetime | None = None
    resolved_by: UUID | None = None
    resolution_note: str | None = None


class OrionExtractionResult(DomainModel):
    event_id: UUID
    entities_created_count: int = 0
    entities_reused_count: int = 0
    relationships_created_count: int = 0
    entity_ids: list[UUID] = Field(default_factory=list)
    relationship_ids: list[UUID] = Field(default_factory=list)


# ── Chronos Timeline Engine ───────────────────────────────────────────────────


class ChronosTimelineEvent(DomainModel):
    id: UUID = Field(default_factory=uuid4)
    accident_event_id: UUID
    event_type: ChronosTimelineEventType
    occurred_at: datetime | None = None
    timestamp_precision: ChronosTimestampPrecision = ChronosTimestampPrecision.UNKNOWN
    sequence_index: int | None = None
    description: str | None = None
    raw_value: str | None = None
    confidence: float = 1.0
    source_claim_id: UUID | None = None
    raw_snapshot_id: UUID | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class ChronosEventLink(DomainModel):
    id: UUID = Field(default_factory=uuid4)
    accident_event_id: UUID
    predecessor_event_id: UUID
    successor_event_id: UUID
    relationship_type: str
    confidence: float = 1.0
    source_claim_id: UUID | None = None
    raw_snapshot_id: UUID | None = None
    created_at: datetime = Field(default_factory=utc_now)


class ChronosSequenceReview(DomainModel):
    id: UUID = Field(default_factory=uuid4)
    accident_event_id: UUID
    timeline_event_id_a: UUID
    timeline_event_id_b: UUID
    reason: str
    status: ChronosSequenceReviewStatus = ChronosSequenceReviewStatus.PENDING
    created_at: datetime = Field(default_factory=utc_now)
    resolved_at: datetime | None = None
    resolved_by: UUID | None = None
    resolution_note: str | None = None


class ChronosExtractionResult(DomainModel):
    event_id: UUID
    timeline_events_created_count: int = 0
    timeline_events_reused_count: int = 0
    event_links_created_count: int = 0
    timeline_event_ids: list[UUID] = Field(default_factory=list)
    event_link_ids: list[UUID] = Field(default_factory=list)


# ── Hermes Source Discovery & Fetch Queue ────────────────────────────────────


class HermesSource(DomainModel):
    id: UUID = Field(default_factory=uuid4)
    name: str
    source_type: HermesSourceType
    base_url: str | None = None
    reliability_tier: str | None = None
    is_active: bool = True
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class HermesCrawlTarget(DomainModel):
    id: UUID = Field(default_factory=uuid4)
    source_id: UUID
    url: str
    normalized_url: str
    status: HermesTargetStatus = HermesTargetStatus.ACTIVE
    label: str | None = None
    last_fetch_job_id: UUID | None = None
    last_fetched_document_id: UUID | None = None
    last_content_sha256: str | None = None
    last_http_status: int | None = None
    last_fetched_at: datetime | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class HermesFetchJob(DomainModel):
    id: UUID = Field(default_factory=uuid4)
    target_id: UUID
    status: HermesFetchJobStatus = HermesFetchJobStatus.QUEUED
    priority: int = 100
    attempt_count: int = 0
    max_attempts: int = 3
    scheduled_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error_message: str | None = None
    # Claim lease/fencing fields.  A RUNNING job is owned by ``locked_by`` until
    # ``lease_expires_at``.  Finalization checks these fields plus attempt_count
    # so a slow/stale worker cannot overwrite a recovered claim.
    locked_by: str | None = None
    locked_at: datetime | None = None
    lease_expires_at: datetime | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class HermesFetchedDocument(DomainModel):
    id: UUID = Field(default_factory=uuid4)
    target_id: UUID
    fetch_job_id: UUID
    url: str
    final_url: str | None = None
    http_status: int | None = None
    content_type: HermesDocumentContentType
    content_sha256: str
    content_length: int
    title: str | None = None
    storage_path: str | None = None
    raw_text_preview: str | None = None
    fetched_at: datetime
    created_at: datetime = Field(default_factory=utc_now)


class HermesSourceChange(DomainModel):
    id: UUID = Field(default_factory=uuid4)
    target_id: UUID
    fetch_job_id: UUID | None = None
    previous_document_id: UUID | None = None
    new_document_id: UUID | None = None
    change_type: HermesChangeType
    previous_sha256: str | None = None
    new_sha256: str | None = None
    detected_at: datetime = Field(default_factory=utc_now)
    created_at: datetime = Field(default_factory=utc_now)


class HermesFetchResult(DomainModel):
    job_id: UUID
    target_id: UUID
    status: HermesFetchJobStatus
    document_id: UUID | None = None
    change_id: UUID | None = None
    change_type: HermesChangeType | None = None
    content_sha256: str | None = None
    error_message: str | None = None


# ── Argus Signal Detection Engine ────────────────────────────────────────────


class ArgusSignal(DomainModel):
    id: UUID = Field(default_factory=uuid4)
    signal_type: ArgusSignalType
    status: ArgusSignalStatus = ArgusSignalStatus.OPEN
    severity: ArgusSeverity
    confidence: float
    title: str
    description: str | None = None
    accident_event_id: UUID | None = None
    primary_entity_id: UUID | None = None
    source_engine: str
    dedupe_key: str
    # Optimistic-concurrency token.  Incremented by every reviewer action via
    # ``ArgusSignalRepository.update_with_version_check``.  Reviewers must
    # pass ``expected_version`` on the review request; mismatches yield 409
    # ``ARGUS_SIGNAL_MODIFIED`` so concurrent confirm/dismiss can't silently
    # last-write-wins.  Created with version 1; reaches 2 after the first
    # successful review, and so on.  ``upsert_signal`` does NOT bump the
    # version — detection passes are non-editorial and shouldn't invalidate
    # in-flight reviewer state.
    version: int = 1
    first_detected_at: datetime = Field(default_factory=utc_now)
    last_detected_at: datetime = Field(default_factory=utc_now)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class ArgusSignalEvidence(DomainModel):
    id: UUID = Field(default_factory=uuid4)
    signal_id: UUID
    evidence_type: ArgusEvidenceType
    evidence_id: UUID
    engine: str
    summary: str | None = None
    created_at: datetime = Field(default_factory=utc_now)


class ArgusSignalReview(DomainModel):
    id: UUID = Field(default_factory=uuid4)
    signal_id: UUID
    decision: ArgusReviewDecision
    reviewer_id: UUID | None = None
    note: str | None = None
    created_at: datetime = Field(default_factory=utc_now)


class ArgusDetectionResult(DomainModel):
    signals_created_count: int = 0
    signals_reused_count: int = 0
    evidence_links_created_count: int = 0
    signal_ids: list[UUID] = Field(default_factory=list)
    evidence_ids: list[UUID] = Field(default_factory=list)
    # Per-engine error reporting: when a detector pass fails before producing
    # any output, the engine name (e.g. "chronos", "hermes") is appended here.
    # The HTTP response stays 200 so partial detection results remain useful,
    # but operators can monitor a non-empty ``engines_errored`` as a failure
    # signal.  See ``RunArgusSignalDetection._detect_chronos``/``_detect_hermes``.
    engines_errored: list[str] = Field(default_factory=list)
    # Engines explicitly requested but intentionally skipped because their
    # detector is not yet implemented (e.g. "orion").  Distinct from
    # ``engines_errored`` — a skip is expected and documented; an error is
    # an infrastructure/runtime failure.  Monitoring should alert on
    # non-empty ``engines_errored``; ``engines_skipped`` is informational.
    engines_skipped: list[str] = Field(default_factory=list)
    # Per-signal-type breakdown of newly-created signals, suitable as the
    # source of truth for ``argus_signals_created_total{signal_type=...}``
    # Prometheus counters.  ``sum(values()) == signals_created_count`` is an
    # invariant — kept in lock-step inside ``_upsert_signal`` so the two
    # counters never diverge.  Reused signals are intentionally not broken
    # down here; the rate-of-discovery is the operational signal that
    # matters.  Keys are the string enum values so the result is JSON-
    # serialisable without a custom encoder.
    signals_created_by_type: dict[str, int] = Field(default_factory=dict)
