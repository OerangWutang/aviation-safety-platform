from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field, model_validator


class ResolveConflictRequest(BaseModel):
    expected_version: int
    winning_claim_id: UUID | None = None
    manual_override_value: Any | None = None
    reason: str = ""

    @property
    def manual_override_provided(self) -> bool:
        """Whether the client sent manual_override_value, even when it is JSON null."""
        return "manual_override_value" in self.model_fields_set

    @model_validator(mode="after")
    def validate_choice(self) -> ResolveConflictRequest:
        has_claim = self.winning_claim_id is not None
        has_override = self.manual_override_provided
        if has_claim == has_override:
            raise ValueError("Provide exactly one of winning_claim_id or manual_override_value")
        return self


class ClaimSummaryDTO(BaseModel):
    id: UUID
    event_id: UUID
    field_name: str
    field_value: Any | None = None
    source_id: UUID
    raw_snapshot_id: UUID | None = None
    claim_type: str
    created_at: datetime
    created_by: UUID | None = None
    superseded_by_claim_id: UUID | None = None


class ConflictSummaryDTO(BaseModel):
    id: UUID
    event_id: UUID
    field_name: str
    status: str
    version: int
    last_modified_reason: str
    last_modified_note: str | None = None
    winning_claim_id: UUID | None = None
    resolved_at: datetime | None = None
    resolved_by: UUID | None = None
    created_at: datetime
    updated_at: datetime
    claim_ids: list[UUID] = Field(default_factory=list)


class ProjectionFieldsDTO(BaseModel):
    event_id: UUID
    projection_version: int
    fields: dict[str, Any]
    completeness_score: float
    unresolved_conflict_fields: list[str]
    updated_at: datetime


class ResolveConflictResponse(BaseModel):
    conflict: ConflictSummaryDTO
    accident_record: ProjectionFieldsDTO


class ReopenConflictRequest(BaseModel):
    expected_version: int
    reason: str = ""


class ActivityLogEntryDTO(BaseModel):
    id: UUID
    conflict_id: UUID
    sequence: int
    from_status: str | None = None
    to_status: str
    modifier_type: str
    modifier_id: UUID | None = None
    reason: str
    version_at_moment: int
    claims_snapshot: dict[str, Any] | None = None
    created_at: datetime


class PaginationDTO(BaseModel):
    limit: int
    next_cursor: UUID | None = None


class ConflictHistoryResponse(BaseModel):
    conflict_id: UUID
    archive_available: bool
    transitions: list[ActivityLogEntryDTO]
    pagination: PaginationDTO | None = None
