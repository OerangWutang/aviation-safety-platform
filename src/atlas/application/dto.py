from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, field_validator


@dataclass(frozen=True)
class CurrentUser:
    user_id: UUID
    role: str


@dataclass(frozen=True)
class CurrentTenantUser:
    """An authenticated caller acting inside a tenant scope.

    Constructed by ``get_current_tenant_user`` when the API key
    carries a non-null ``tenant_id`` + ``tenant_role`` pair AND a
    matching active ``TenantMembership`` row exists.  Carries both
    the system identity (``user_id``, ``role``) and the tenant
    identity (``tenant_id``, ``tenant_role``).

    The system ``role`` still governs public-side reads — a tenant
    caller acting as analyst can read public events the same way any
    other analyst can.  The tenant binding is purely additive.
    """

    user_id: UUID
    role: str
    tenant_id: UUID
    tenant_role: str


@dataclass(frozen=True)
class IngestionResult:
    event_id: UUID
    event_created: bool
    snapshot_created: bool
    idempotent_replay: bool = False
    # Backward-compatible primary review handle. When an ambiguous identity
    # match fans out to multiple candidate events, this is the first review id
    # and ``pending_review_ids`` contains the full set.
    pending_review_id: UUID | None = None
    pending_review_ids: tuple[UUID, ...] = ()
    attached_by: str = ""

    def __post_init__(self) -> None:
        """Keep legacy ``pending_review_id`` and new plural ids consistent."""
        ids = tuple(dict.fromkeys(self.pending_review_ids))
        primary = self.pending_review_id
        if primary is None and ids:
            primary = ids[0]
        if primary is not None:
            ids = (primary, *tuple(review_id for review_id in ids if review_id != primary))
        object.__setattr__(self, "pending_review_id", primary)
        object.__setattr__(self, "pending_review_ids", ids)


class IngestionClaimDTO(BaseModel):
    """Application-layer DTO for a single normalized/source claim.

    This validation intentionally lives below the HTTP layer so non-HTTP
    callers (CLI, tests, workers, future imports) cannot bypass the invariant
    that every claim has a meaningful field name.
    """

    field_name: str
    field_value: Any

    @field_validator("field_name")
    @classmethod
    def field_name_non_empty(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("field_name must not be blank")
        return value


class ProjectionDTO(BaseModel):
    event_id: UUID
    projection_version: int
    fields: dict[str, Any]
    completeness_score: float
    unresolved_conflict_fields: list[str]
    updated_at: datetime


class ProjectionHistoryEntryDTO(BaseModel):
    id: UUID
    projection_version: int
    caused_by_conflict_id: UUID | None = None
    caused_by_ingestion_run_id: UUID | None = None
    caused_by_outbox_event_id: UUID | None = None
    changed_fields: list[str] | None = None
    created_at: datetime
