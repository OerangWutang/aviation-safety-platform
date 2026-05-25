"""Pydantic response schemas for the audit endpoints (Phase 11).

Same policy as the other public surfaces: ``extra='forbid'`` keeps
unknown fields from accidentally leaking into the response payload,
and the explicit schema acts as a documented whitelist.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class _AuditModel(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)


# ── Page audit ──────────────────────────────────────────────────────────────


class AuditFieldRow(_AuditModel):
    field_name: str
    current_value: Any = None
    is_disputed: bool
    is_manually_overridden: bool
    confidence: str
    plain_english: str


class PageAuditResponse(_AuditModel):
    slug: str
    canonical_event_id: UUID
    summary: str
    confidence: str
    confidence_meaning: str
    projection_version: int
    last_updated_at: datetime
    fields: list[AuditFieldRow]


# ── Field explanation ───────────────────────────────────────────────────────


class ExpertDetail(_AuditModel):
    """Internal-style detail returned when ``detail=expert``.

    Reserved for audit consumers who want the raw machinery.  Default
    responses omit this entirely, so changing it cannot break the
    non-technical surface.
    """

    claim_id: UUID
    claim_type: str
    source_reliability_tier: int | None = None
    created_at: datetime


class FieldExplanationWinnerItem(_AuditModel):
    field_name: str
    current_value: Any = None
    plain_english: str
    source_name: str
    source_kind: str
    expert: ExpertDetail | None = None


class FieldExplanationLoserItem(_AuditModel):
    source_name: str
    source_kind: str
    reported_value: Any = None
    plain_english: str
    expert: ExpertDetail | None = None


class FieldExplanationConflictItem(_AuditModel):
    status: str
    plain_english: str
    resolved_at: datetime | None = None


class FieldExplanationResponse(_AuditModel):
    event_id: UUID
    field_name: str
    has_winner: bool
    winner: FieldExplanationWinnerItem | None = None
    losers: list[FieldExplanationLoserItem]
    losers_truncated: bool
    conflict: FieldExplanationConflictItem | None = None


# ── Claim explanation ───────────────────────────────────────────────────────


class ClaimHistoryItem(_AuditModel):
    action: str
    reason: str
    to_claim_type: str
    from_claim_type: str | None = None
    created_at: datetime


class ClaimExplanationResponse(_AuditModel):
    claim_id: UUID
    event_id: UUID
    field_name: str
    field_value: Any = None
    claim_type: str
    plain_english: str
    source_name: str
    source_kind: str
    is_winning: bool
    is_active: bool
    is_superseded: bool
    created_at: datetime
    history: list[ClaimHistoryItem]
    history_truncated: bool


# ── Source verification ─────────────────────────────────────────────────────


class SourceVerificationResponse(_AuditModel):
    snapshot_id: UUID
    source_name: str
    source_kind: str
    source_record_id: str | None = None
    raw_payload_hash: str | None = Field(
        default=None,
        description=(
            "SHA-256 hash of the canonicalised source payload at the "
            "time of ingestion.  ``None`` for older snapshots that "
            "predate the hash columns."
        ),
    )
    captured_at: datetime
    recipe_version: str
    recipe_steps: list[str]
    verification_note: str
