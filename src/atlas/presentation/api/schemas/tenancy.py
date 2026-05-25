"""Pydantic schemas for the tenancy router (Phase 5)."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class _TenancyModel(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)


# ── Sources ──────────────────────────────────────────────────────────────────


class RegisterTenantSourceRequest(_TenancyModel):
    name: str = Field(min_length=1, max_length=300)
    kind: str = Field(default="EXTERNAL", max_length=50)
    reliability_tier: int = Field(default=1, ge=1, le=10)


class TenantSourceResponse(_TenancyModel):
    id: UUID
    tenant_id: UUID
    name: str
    kind: str
    reliability_tier: int
    created_at: datetime


# ── Event overlay ────────────────────────────────────────────────────────────


class TenantOverlayItem(_TenancyModel):
    notes_markdown: str | None = None
    overlay_fields: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime


class TenantEventOverlayResponse(_TenancyModel):
    event_id: UUID
    overlay: TenantOverlayItem | None = None
    # Public snapshot context — what the public projection says
    # today, included so the tenant UI can render the overlay next to
    # the public truth without a second round-trip.
    public_fields: dict[str, Any]
    public_completeness_score: float
    public_projection_version: int


class UpsertTenantEventOverlayRequest(_TenancyModel):
    notes_markdown: str | None = None
    overlay_fields: dict[str, Any] = Field(default_factory=dict)


# ── Event list ───────────────────────────────────────────────────────────────


class TenantEventListItemResponse(_TenancyModel):
    event_id: UUID
    has_overlay: bool
    overlay_updated_at: datetime | None = None
    notes_preview: str | None = None


class TenantEventListResponse(_TenancyModel):
    items: list[TenantEventListItemResponse]
    limit: int
    next_cursor: UUID | None = None


# ── Phase 6: ingestion + safety reports ─────────────────────────────────────


class OpenIngestionRunRequest(_TenancyModel):
    tenant_source_id: UUID


class IngestionRunResponse(_TenancyModel):
    id: UUID
    tenant_id: UUID
    tenant_source_id: UUID
    status: str
    started_at: datetime
    finished_at: datetime | None = None


class IncomingClaimItem(_TenancyModel):
    """One claim in a batch submission.

    Carries only what the server cannot derive from context: the
    target event, the field name and value, and the optional
    discriminator/confidence.  Server-side fields (id, tenant_id,
    source_id, run_id, created_at) come from the path and the
    run lookup.
    """

    event_id: UUID
    field_name: str = Field(min_length=1, max_length=200)
    field_value: Any = None
    claim_kind: str = Field(default="OTHER")
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)


class SubmitClaimsBatchRequest(_TenancyModel):
    claims: list[IncomingClaimItem]


class SubmitClaimsBatchResponse(_TenancyModel):
    inserted_count: int


class CompleteIngestionRunRequest(_TenancyModel):
    final_status: str = Field(
        description="Either 'succeeded' or 'failed'.",
    )


class SubmitSafetyReportRequest(_TenancyModel):
    report_kind: str
    narrative_markdown: str = Field(min_length=1)
    deidentified_attested: bool
    external_report_ref: str | None = Field(default=None, max_length=200)
    # Optional event association.  If set, the safety report and the
    # association land in one UoW.
    associate_with_event_id: UUID | None = None
    association_kind: str = "RELATED"
    association_note: str | None = Field(default=None, max_length=2000)


class SafetyReportItem(_TenancyModel):
    id: UUID
    tenant_id: UUID
    report_kind: str
    narrative_markdown: str
    deidentified_attested: bool
    external_report_ref: str | None = None
    submitter_user_id: UUID
    created_at: datetime


class EventAssociationItem(_TenancyModel):
    id: UUID
    tenant_id: UUID
    event_id: UUID
    claim_id: UUID | None = None
    safety_report_id: UUID | None = None
    association_kind: str
    note: str | None = None
    created_by_user_id: UUID
    created_at: datetime


class SubmitSafetyReportResponse(_TenancyModel):
    report: SafetyReportItem
    association: EventAssociationItem | None = None
    # Audit info for the operator: substrings the scrubber redacted.
    # Atlas does NOT store this; we return it so the operator's SMS
    # tooling can log it locally if they choose.
    scrub_replacements: list[str]


class TenantClaimItem(_TenancyModel):
    id: UUID
    tenant_id: UUID
    event_id: UUID
    tenant_source_id: UUID
    tenant_ingestion_run_id: UUID | None = None
    field_name: str
    field_value: Any = None
    claim_kind: str
    confidence: float | None = None
    created_at: datetime


class TenantEvidenceForEventResponse(_TenancyModel):
    event_id: UUID
    foqa_claims: list[TenantClaimItem]
    asap_claims: list[TenantClaimItem]
    other_claims: list[TenantClaimItem]
    associated_reports: list[SafetyReportItem]
    associations: list[EventAssociationItem]


# ── Echo cross-reference schemas ─────────────────────────────────────────────


class CrossrefMatchComponentItem(_TenancyModel):
    """One scored contributor to a precedent match — the explainability unit."""

    name: str = Field(description="Component name: finding_categories | attributes | lexical")
    weight: float = Field(description="Weight of this component in the blended score (0-1)")
    score: float = Field(description="Component similarity score (0-1)")
    detail: str = Field(description="Human-readable explanation of what matched")


class CrossrefMatchItem(_TenancyModel):
    """A public accident surfaced as a precedent for a private hazard.

    ``score`` and ``support`` are *similarity* measures — evidence that
    analogous accidents exist in the public record.  They are not
    probabilities of recurrence.
    """

    event_id: str = Field(description="NTSB accession number of the matched public event")
    score: float = Field(
        description="Blended similarity score (0-1). Not a probability of recurrence."
    )
    support: str = Field(
        description=(
            "Coarse evidence-support band: STRONG (≥0.60) | MODERATE (≥0.35) | "
            "WEAK (≥0.15) | NONE (<0.15). Not a probability of recurrence."
        )
    )
    components: list[CrossrefMatchComponentItem] = Field(
        description="Per-component breakdown explaining why this precedent surfaced"
    )
    shared_finding_categories: list[str] = Field(
        description="NTSB cause taxonomy keys (CC.SS) shared between hazard and this accident"
    )
    shared_terms: list[str] = Field(
        description="Normalised narrative terms shared between hazard and this accident"
    )
    display_occurred_on: str | None = Field(None, description="Accident date (YYYY-MM-DD)")
    display_location: str | None = Field(None, description="City, State")
    display_aircraft: str | None = Field(None, description="Make Model")
    display_probable_cause: str | None = Field(
        None, description="NTSB probable-cause text (truncated to 300 chars)"
    )


class RequestCrossrefResponse(_TenancyModel):
    """Immediate response to POST /crossref — the run is queued, not yet complete.

    Poll the URL in ``poll_url`` until ``status`` is ``COMPLETE`` or ``FAILED``.
    Recommended interval: 2 s, timeout after 120 s.
    """

    crossref_result_id: UUID = Field(description="ID of the queued cross-reference result")
    poll_url: str = Field(
        description=(
            "Fully-qualified URL to poll for results. "
            "GET this URL every 2 s until status is COMPLETE or FAILED."
        )
    )
    status: str = Field(
        description="Always 'PENDING' on creation. Poll poll_url until 'COMPLETE' or 'FAILED'."
    )


class CrossrefResultResponse(_TenancyModel):
    """Echo cross-reference result.

    Polling contract
    ----------------
    ``POST /crossref`` returns 202 with a ``crossref_result_id``.
    Poll ``GET /crossref/{crossref_result_id}`` until ``status`` transitions:

    * ``PENDING``  — matching is in progress. Continue polling.
    * ``COMPLETE`` — ``matches`` and ``match_count`` are populated.
    * ``FAILED``   — ``error_detail`` explains why. Do not retry automatically;
                     surface the error to the user and allow manual re-request.

    Recommended poll interval: every 2 seconds.
    Recommended timeout: 120 seconds (corpus load ~8 s, matching <1 s).
    After timeout, treat as ``FAILED`` in the UI.

    Epistemic note
    --------------
    ``matches[].score`` and ``matches[].support`` are *similarity* measures,
    not probabilities of recurrence.  Render ``support`` as the primary signal
    (e.g. "Strong precedent found") and ``score`` as supporting detail.
    """

    id: UUID = Field(description="Result ID — use to poll this endpoint")
    tenant_id: UUID
    safety_report_id: UUID | None = None
    claim_id: UUID | None = None
    status: str = Field(description="PENDING | COMPLETE | FAILED")
    matches: list[CrossrefMatchItem] = Field(
        description="Ranked precedent matches. Empty list while PENDING or if no matches found."
    )
    match_count: int = Field(description="Number of matches returned (0 while PENDING)")
    matcher_config: dict[str, Any] = Field(
        description=(
            "Echo config snapshot: weights, thresholds, corpus size at run time. "
            "Preserved so results remain interpretable if matcher is later retuned."
        )
    )
    requested_at: datetime = Field(description="When the cross-reference was requested (UTC)")
    completed_at: datetime | None = Field(
        None, description="When matching completed (UTC). Null while PENDING."
    )
    error_detail: str | None = Field(
        None, description="Human-readable failure reason. Populated only on FAILED."
    )
