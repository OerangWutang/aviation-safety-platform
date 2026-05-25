"""Metering router (Phase 8).

Three surfaces:

- Tenant-facing usage read (``GET .../tenants/{tenant_id}/usage``),
  member-gated and tenant-isolated.
- Admin usage summary (``GET /admin/usage/summary``), ADMIN-only.
- Admin rollup trigger (``POST /admin/usage/rollups``), ADMIN-only.
  Operators schedule this however they like; exposing it as an
  endpoint lets a cron job or manual reconciliation drive it.
"""

from __future__ import annotations

from datetime import date
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response

from atlas.application.dto import CurrentTenantUser, CurrentUser
from atlas.application.unit_of_work import UnitOfWork
from atlas.application.use_cases.metering import (
    ComputeDailyRollups,
    ComputeDailyRollupsInput,
    GetAdminUsageSummary,
    GetAdminUsageSummaryInput,
    GetTenantUsage,
    GetTenantUsageInput,
)
from atlas.domain.enums import Role
from atlas.presentation.api.dependencies import (
    get_uow,
    require_role,
    require_tenant_membership,
)
from atlas.presentation.api.responses import offloaded_json_response
from atlas.presentation.api.schemas.metering import (
    AdminUsageSummaryResponse,
    ComputeRollupsRequest,
    ComputeRollupsResponse,
    TenantUsageResponse,
    UsageRollupItem,
    UsageSummaryItem,
)

# Tenant-facing usage lives under the enterprise prefix alongside
# the rest of the tenant surface.
tenant_router = APIRouter(prefix="/enterprise/tenants/{tenant_id}", tags=["metering-tenant"])
# Admin-facing usage lives under /admin.
admin_router = APIRouter(prefix="/admin/usage", tags=["metering-admin"])


@tenant_router.get("/usage", response_model=TenantUsageResponse)
async def get_tenant_usage(
    tenant_id: UUID,
    day_from: date = Query(...),
    day_to: date = Query(...),
    caller: CurrentTenantUser = Depends(require_tenant_membership()),
    uow: UnitOfWork = Depends(get_uow, scope="function"),
) -> Response:
    rollups = await GetTenantUsage(uow).execute(
        GetTenantUsageInput(
            tenant_id=tenant_id,
            caller_tenant_id=caller.tenant_id,
            day_from=day_from,
            day_to=day_to,
        )
    )
    payload = TenantUsageResponse(
        tenant_id=tenant_id,
        day_from=day_from,
        day_to=day_to,
        rollups=[
            UsageRollupItem(
                metric_kind=r.metric_kind.value
                if hasattr(r.metric_kind, "value")
                else r.metric_kind,
                day=r.day,
                count=r.count,
                computed_at=r.computed_at,
            )
            for r in rollups
        ],
    )
    return await offloaded_json_response(payload.model_dump(mode="json"))


@admin_router.get("/summary", response_model=AdminUsageSummaryResponse)
async def get_admin_usage_summary(
    day_from: date = Query(...),
    day_to: date = Query(...),
    uow: UnitOfWork = Depends(get_uow, scope="function"),
    _user: CurrentUser = Depends(require_role(Role.ADMIN)),
) -> Response:
    rows = await GetAdminUsageSummary(uow).execute(
        GetAdminUsageSummaryInput(day_from=day_from, day_to=day_to)
    )
    payload = AdminUsageSummaryResponse(
        day_from=day_from,
        day_to=day_to,
        items=[
            UsageSummaryItem(
                tenant_id=r.tenant_id,
                tenant_slug=r.tenant_slug,
                metric_kind=r.metric_kind.value
                if hasattr(r.metric_kind, "value")
                else r.metric_kind,
                total_count=r.total_count,
            )
            for r in rows
        ],
    )
    return await offloaded_json_response(payload.model_dump(mode="json"))


@admin_router.post("/rollups", response_model=ComputeRollupsResponse, status_code=201)
async def compute_rollups(
    request: ComputeRollupsRequest,
    uow: UnitOfWork = Depends(get_uow, scope="function"),
    _user: CurrentUser = Depends(require_role(Role.ADMIN)),
) -> Response:
    result = await ComputeDailyRollups(uow).execute(
        ComputeDailyRollupsInput(
            day_from=request.day_from,
            day_to=request.day_to,
            tenant_ids=request.tenant_ids,
        )
    )
    payload = ComputeRollupsResponse(rows_written=result.rows_written)
    return await offloaded_json_response(payload.model_dump(mode="json"), status_code=201)
