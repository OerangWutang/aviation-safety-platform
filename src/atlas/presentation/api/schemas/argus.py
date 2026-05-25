"""Argus v0.1 API schemas."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field

from atlas.domain.enums import (
    ArgusEvidenceType,
    ArgusReviewDecision,
    ArgusSeverity,
    ArgusSignalStatus,
    ArgusSignalType,
)


class ArgusRunDetectionRequest(BaseModel):
    include_chronos: bool = True
    include_hermes: bool = True
    include_atlas: bool = True
    # Orion detector is NOT YET IMPLEMENTED.  Defaults to False so the API
    # response is honest about what was actually evaluated.  Setting this to
    # True returns engines_skipped=["orion"] in the response.
    include_orion: bool = False
    # Maximum rows each engine reads per detection pass.  Bounded to prevent
    # accidental full-table scans if the tables grow large.
    recent_limit: int = Field(default=100, ge=1, le=1000)
    # Minimum number of OPEN claim_conflicts an event must have before Argus
    # emits a HIGH_CONFLICT_ACCIDENT_RECORD signal.  Must be >= 2.  Severity
    # then scales with the count via ``severity_for_atlas_high_conflict``.
    high_conflict_threshold: int = Field(default=3, ge=2)


class ArgusDetectionResponse(BaseModel):
    signals_created_count: int
    signals_reused_count: int
    evidence_links_created_count: int
    signal_ids: list[UUID] = Field(default_factory=list)
    evidence_ids: list[UUID] = Field(default_factory=list)
    # Engine names that failed mid-pass (e.g. "chronos", "hermes").  A non-empty
    # list means the run is partial; HTTP status stays 200 so partial results
    # are still consumable, but monitoring should alert on this field.
    engines_errored: list[str] = Field(default_factory=list)
    # Engine names that were requested but intentionally skipped because their
    # detector is not yet implemented (e.g. "orion").  Informational — not an
    # error.  Monitoring should NOT alert on this field.
    engines_skipped: list[str] = Field(default_factory=list)
    # Per-signal-type breakdown of newly-created signals.  Keys are
    # ``ArgusSignalType`` string values.  Useful for dashboards and for
    # validating that the Prometheus counter rates match the API response.
    signals_created_by_type: dict[str, int] = Field(default_factory=dict)

    model_config = {"from_attributes": True}


class ArgusSignalResponse(BaseModel):
    id: UUID
    signal_type: ArgusSignalType
    status: ArgusSignalStatus
    severity: ArgusSeverity
    confidence: float
    title: str
    description: str | None
    accident_event_id: UUID | None
    primary_entity_id: UUID | None
    source_engine: str
    dedupe_key: str
    # Optimistic-concurrency token.  Echo this back as ``expected_version``
    # on the next ``POST /signals/{id}/review`` to detect concurrent races.
    version: int
    first_detected_at: datetime
    last_detected_at: datetime
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class ArgusSignalEvidenceResponse(BaseModel):
    id: UUID
    signal_id: UUID
    evidence_type: ArgusEvidenceType
    evidence_id: UUID
    engine: str
    summary: str | None
    created_at: datetime

    model_config = {"from_attributes": True}


class ArgusSignalReviewResponse(BaseModel):
    id: UUID
    signal_id: UUID
    decision: ArgusReviewDecision
    reviewer_id: UUID | None
    note: str | None
    created_at: datetime

    model_config = {"from_attributes": True}


class ArgusSignalDetailResponse(BaseModel):
    signal: ArgusSignalResponse
    evidence: list[ArgusSignalEvidenceResponse] = Field(default_factory=list)
    reviews: list[ArgusSignalReviewResponse] = Field(default_factory=list)

    model_config = {"from_attributes": True}


class ArgusSignalsPagination(BaseModel):
    limit: int
    # ``next_cursor`` is the id of the last item on the current page.  Pass
    # it back as ``cursor`` to fetch the next page.  ``None`` means no more
    # rows in the requested filter set.
    next_cursor: UUID | None = None


class ArgusSignalsPageResponse(BaseModel):
    """Paginated response envelope for ``GET /argus/signals/page``.

    Distinct from the legacy ``GET /argus/signals``, which returns a bare
    list with offset pagination.  Once consumers migrate, the legacy
    endpoint can be removed.
    """

    items: list[ArgusSignalResponse] = Field(default_factory=list)
    pagination: ArgusSignalsPagination

    model_config = {"from_attributes": True}


class ArgusReviewSignalRequest(BaseModel):
    decision: ArgusReviewDecision
    # Optimistic-concurrency token.  Clients must pass the ``version`` they
    # observed on the most recent GET; mismatches yield 409
    # ``ARGUS_SIGNAL_MODIFIED`` so two reviewers can't last-write-wins.
    expected_version: int = Field(ge=1)
    note: str | None = None
