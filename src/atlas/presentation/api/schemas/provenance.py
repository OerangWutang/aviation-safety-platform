from typing import Any
from uuid import UUID

from pydantic import BaseModel


class ProvenancePagination(BaseModel):
    limit: int
    next_cursor: UUID | None = None
    next_cursors: dict[str, UUID | None]
    has_more: bool


class ProvenanceResponse(BaseModel):
    event_id: UUID
    absorbed_event_id: UUID | None = None
    canonicalized: bool = False
    projection: dict[str, Any] | None
    claims: list[dict[str, Any]]
    claim_histories: list[dict[str, Any]]
    conflicts: list[dict[str, Any]]
    conflict_activity_logs: list[dict[str, Any]]
    projection_history: list[dict[str, Any]]
    pagination: ProvenancePagination
    archive_available: bool
