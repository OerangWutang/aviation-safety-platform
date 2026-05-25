"""Chronos v0.1 API response schemas."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field

from atlas.domain.enums import (
    ChronosSequenceReviewStatus,
    ChronosTimelineEventType,
    ChronosTimestampPrecision,
)


class ChronosTimelineEventResponse(BaseModel):
    id: UUID
    accident_event_id: UUID
    event_type: ChronosTimelineEventType
    occurred_at: datetime | None
    timestamp_precision: ChronosTimestampPrecision
    sequence_index: int | None
    description: str | None
    raw_value: str | None
    confidence: float
    source_claim_id: UUID | None
    raw_snapshot_id: UUID | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class ChronosEventLinkResponse(BaseModel):
    id: UUID
    accident_event_id: UUID
    predecessor_event_id: UUID
    successor_event_id: UUID
    relationship_type: str
    confidence: float
    source_claim_id: UUID | None
    raw_snapshot_id: UUID | None
    created_at: datetime

    model_config = {"from_attributes": True}


class ChronosTimelineResponse(BaseModel):
    event_id: UUID
    timeline_events: list[ChronosTimelineEventResponse] = Field(default_factory=list)
    event_links: list[ChronosEventLinkResponse] = Field(default_factory=list)


class ChronosExtractionResponse(BaseModel):
    event_id: UUID
    timeline_events_created_count: int
    timeline_events_reused_count: int
    event_links_created_count: int
    timeline_event_ids: list[UUID] = Field(default_factory=list)
    event_link_ids: list[UUID] = Field(default_factory=list)


class ChronosSequenceReviewResponse(BaseModel):
    id: UUID
    accident_event_id: UUID
    timeline_event_id_a: UUID
    timeline_event_id_b: UUID
    reason: str
    status: ChronosSequenceReviewStatus
    created_at: datetime
    resolved_at: datetime | None
    resolved_by: UUID | None
    resolution_note: str | None

    model_config = {"from_attributes": True}
