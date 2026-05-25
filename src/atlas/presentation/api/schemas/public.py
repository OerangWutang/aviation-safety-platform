"""Pydantic response schemas for the public encyclopedia endpoints.

These are intentionally kept separate from the dataclasses used inside
``atlas.application.use_cases.public_events`` so that:

- the OpenAPI surface is owned by the presentation layer, not the
  application layer;
- adding fields here is a deliberate decision (Pydantic enforces the
  whitelist by construction — anything not declared here is dropped on
  serialization);
- the application layer dataclasses can stay framework-free.

Field policy
------------

Every field exposed below is either:

1. editorial overlay from ``PublicEventPage`` (title, summaries,
   publication timestamps), or
2. derived from the live ``ProjectedAccidentRecord`` or its evidence
   chain, with internal identifiers and raw hashes stripped.

What is *intentionally* not exposed:

- ``raw_payload_hash`` / ``submission_hash`` / ``ingestion_run_id``
  (internal audit data);
- ``Source.field_mapping_json`` (internal normalization config);
- ``Claim.id`` / ``Claim.raw_snapshot_id`` / ``Claim.created_by``
  (internal identifiers — would only be useful for an authenticated
  audit caller, not the public surface);
- any tenant-private overlay (served under the tenant-scoped prefix).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class _PublicModel(BaseModel):
    """Base model that forbids extra fields on serialization.

    ``extra='forbid'`` here is doubly defensive: callers do not POST
    these models (every public endpoint in Phase 1 is GET-only), but
    if a future contributor wires up a write endpoint by accident,
    extra payload fields will be rejected at the boundary instead of
    silently dropped.
    """

    model_config = ConfigDict(extra="forbid", from_attributes=True)


# ── List ─────────────────────────────────────────────────────────────────────


class PublicEventSummary(_PublicModel):
    slug: str
    title: str
    short_summary: str | None = None
    event_date: str | None = None
    location: str | None = None
    operator: str | None = None
    aircraft_type: str | None = None
    fatalities_total: Any = None
    confidence: str = Field(description="Coarse confidence band: high, medium, low, unknown.")
    has_unresolved_conflicts: bool = False
    last_published_at: datetime | None = None


class PublicEventListResponse(_PublicModel):
    items: list[PublicEventSummary]
    limit: int
    next_cursor: UUID | None = Field(
        default=None,
        description=(
            "Keyset cursor for the next page; pass it back as the "
            "``cursor`` query parameter.  ``None`` when the result "
            "set is exhausted."
        ),
    )


# ── Detail ───────────────────────────────────────────────────────────────────


class PublicEventEditorial(_PublicModel):
    """Editorial overlay block in the detail response.

    Kept under its own key so consumers cannot confuse editorial prose
    with evidence-backed structured facts.
    """

    title: str
    short_summary: str | None = None
    narrative_markdown: str | None = None


class PublicEventDetailResponse(_PublicModel):
    slug: str
    canonical_event_id: UUID
    editorial: PublicEventEditorial
    fields: dict[str, Any] = Field(
        description=(
            "Projected structured fields.  Disputed fields appear as the string ``__DISPUTED__``."
        ),
    )
    completeness_score: float
    confidence: str
    unresolved_conflict_fields: list[str]
    projection_version: int
    first_published_at: datetime | None = None
    last_published_at: datetime | None = None
    last_updated_at: datetime


# ── Evidence ─────────────────────────────────────────────────────────────────


class PublicEvidenceClaimItem(_PublicModel):
    field_name: str
    field_value: Any
    claim_type: str
    source_name: str
    source_kind: str
    source_reliability_tier: int
    is_winning: bool
    is_superseded: bool
    created_at: datetime


class PublicEvidenceSourceItem(_PublicModel):
    name: str
    kind: str
    reliability_tier: int


class PublicEvidenceResponse(_PublicModel):
    slug: str
    canonical_event_id: UUID
    claims: list[PublicEvidenceClaimItem]
    sources: list[PublicEvidenceSourceItem]
    claim_count: int
    truncated: bool = Field(
        description=(
            "True when the public claim cap was hit and the response "
            "is a stable prefix of all active claims."
        ),
    )


# ── Timeline ─────────────────────────────────────────────────────────────────


class PublicTimelineEventItem(_PublicModel):
    event_type: str
    occurred_at: datetime | None = None
    timestamp_precision: str
    sequence_index: int | None = None
    description: str | None = None


class PublicTimelineResponse(_PublicModel):
    slug: str
    canonical_event_id: UUID
    events: list[PublicTimelineEventItem]


# ── Related ──────────────────────────────────────────────────────────────────


class PublicRelatedEventItem(_PublicModel):
    slug: str
    title: str
    short_summary: str | None = None
    last_published_at: datetime | None = None
    relation: str = Field(
        description="Reason this event is related (e.g. OPERATED_BY, AIRCRAFT_TYPE)."
    )


class PublicRelatedResponse(_PublicModel):
    slug: str
    canonical_event_id: UUID
    items: list[PublicRelatedEventItem]


# ── Retraction error body ────────────────────────────────────────────────────


class PublicRetractionDetail(_PublicModel):
    """Body of the 410 Gone response for a retracted page.

    Surfaced via the ``PublicEventPageRetractedError`` exception
    handler in ``app.py``.  Keeping the note on the retraction (not in
    a generic error message) lets consumers render a meaningful notice
    without scraping prose.
    """

    slug: str
    retraction_note: str | None = None
