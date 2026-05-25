"""Orion v0.1 API response schemas."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field

from atlas.domain.enums import OrionEntityType, OrionRelationshipType, OrionReviewStatus


class OrionIdentifierResponse(BaseModel):
    id: UUID
    entity_id: UUID
    identifier_type: str
    identifier_value: str
    normalized_value: str
    confidence: float
    source_claim_id: UUID | None
    created_at: datetime

    model_config = {"from_attributes": True}


class OrionEntityResponse(BaseModel):
    id: UUID
    entity_type: OrionEntityType
    canonical_name: str
    status: str
    confidence: float
    merged_into_entity_id: UUID | None
    created_at: datetime
    updated_at: datetime
    identifiers: list[OrionIdentifierResponse] = Field(default_factory=list)

    model_config = {"from_attributes": True}


class OrionRelationshipResponse(BaseModel):
    id: UUID
    subject_entity_id: UUID | None
    relationship_type: OrionRelationshipType
    object_entity_id: UUID
    accident_event_id: UUID
    confidence: float
    created_at: datetime

    model_config = {"from_attributes": True}


class OrionEventEntitiesResponse(BaseModel):
    event_id: UUID
    entities: list[OrionEntityResponse] = Field(default_factory=list)
    relationships: list[OrionRelationshipResponse] = Field(default_factory=list)


class OrionExtractionResponse(BaseModel):
    event_id: UUID
    entities_created_count: int
    entities_reused_count: int
    relationships_created_count: int
    entity_ids: list[UUID] = Field(default_factory=list)
    relationship_ids: list[UUID] = Field(default_factory=list)


class OrionEntitySearchResponse(BaseModel):
    query: str
    entity_type: OrionEntityType | None
    results: list[OrionEntityResponse] = Field(default_factory=list)


class OrionReviewResponse(BaseModel):
    id: UUID
    candidate_entity_id_a: UUID
    candidate_entity_id_b: UUID
    entity_type: OrionEntityType
    match_score: float
    matched_identifiers: list[str] = Field(default_factory=list)
    status: OrionReviewStatus
    created_at: datetime
    resolved_at: datetime | None
    resolution_note: str | None

    model_config = {"from_attributes": True}
