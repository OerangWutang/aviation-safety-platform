"""Pydantic schemas for the Phase 8 metering router."""

from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class _MeteringModel(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)


class UsageRollupItem(_MeteringModel):
    metric_kind: str
    day: date
    count: int
    computed_at: datetime


class TenantUsageResponse(_MeteringModel):
    tenant_id: UUID
    day_from: date
    day_to: date
    rollups: list[UsageRollupItem]


class UsageSummaryItem(_MeteringModel):
    tenant_id: UUID | None
    tenant_slug: str | None
    metric_kind: str
    total_count: int


class AdminUsageSummaryResponse(_MeteringModel):
    day_from: date
    day_to: date
    items: list[UsageSummaryItem]


class ComputeRollupsRequest(_MeteringModel):
    day_from: date
    day_to: date
    # Optional explicit tenant filter; omit to roll up all tenants
    # that had activity in the range.
    tenant_ids: list[UUID] | None = None


class ComputeRollupsResponse(_MeteringModel):
    rows_written: int
