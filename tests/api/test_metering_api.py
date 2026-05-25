"""API tests for Phase 8 metering.

Coverage:

- Admin rollup trigger then admin summary read.
- Tenant usage read (member-gated, tenant-isolated).
- Cross-tenant usage read → 403.
- Admin endpoints reject non-admin roles.
- Auth gates.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from httpx import ASGITransport, AsyncClient

from atlas.domain.metering.entities import MetricKind, UsageEvent
from atlas.domain.tenancy.entities import Tenant, TenantRole
from tests.api.conftest import make_tenant_client_for


def _seed_tenant(uow, *, slug: str = "acme") -> Tenant:
    t = Tenant(slug=slug, display_name=slug.upper())
    uow.store.tenancy.tenants[t.id] = t
    return t


def _seed_claim_event(uow, tenant_id, *, when=None):
    uow.store.metering.events.append(
        UsageEvent(
            metric_kind=MetricKind.TENANT_CLAIM_INGESTED,
            tenant_id=tenant_id,
            recorded_at=when or datetime(2024, 6, 1, 10, 0, tzinfo=UTC),
        )
    )


async def _tenant_client(tenant, role, uow, monkeypatch) -> AsyncClient:
    app = make_tenant_client_for(
        tenant_id=tenant.id,
        tenant_role=role.value,
        uow=uow,
        monkeypatch=monkeypatch,
    )
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver")


# ── Admin rollup + summary ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_admin_rollup_then_summary(async_client_admin: AsyncClient, in_memory_uow):
    tenant = _seed_tenant(in_memory_uow)
    _seed_claim_event(in_memory_uow, tenant.id)
    _seed_claim_event(in_memory_uow, tenant.id)

    # Trigger rollup.
    rollup_resp = await async_client_admin.post(
        "/api/v1/admin/usage/rollups",
        json={"day_from": "2024-06-01", "day_to": "2024-06-01"},
    )
    assert rollup_resp.status_code == 201, rollup_resp.text
    assert rollup_resp.json()["rows_written"] > 0

    # Read summary.
    summary_resp = await async_client_admin.get(
        "/api/v1/admin/usage/summary",
        params={"day_from": "2024-06-01", "day_to": "2024-06-01"},
    )
    assert summary_resp.status_code == 200
    items = summary_resp.json()["items"]
    claim_items = [
        i
        for i in items
        if i["metric_kind"] == "TENANT_CLAIM_INGESTED" and i["tenant_slug"] == "acme"
    ]
    assert claim_items and claim_items[0]["total_count"] == 2


@pytest.mark.asyncio
async def test_admin_summary_rejects_non_admin(
    async_client_analyst: AsyncClient,
):
    resp = await async_client_analyst.get(
        "/api/v1/admin/usage/summary",
        params={"day_from": "2024-06-01", "day_to": "2024-06-01"},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_admin_rollup_rejects_non_admin(
    async_client_analyst: AsyncClient,
):
    resp = await async_client_analyst.post(
        "/api/v1/admin/usage/rollups",
        json={"day_from": "2024-06-01", "day_to": "2024-06-01"},
    )
    assert resp.status_code == 403


# ── Tenant usage read ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_tenant_reads_own_usage(in_memory_uow, monkeypatch):
    tenant = _seed_tenant(in_memory_uow)
    _seed_claim_event(in_memory_uow, tenant.id)

    # First roll up via the use case directly (admin client would
    # also work, but we keep this test focused on the tenant read).
    from atlas.application.use_cases.metering import (
        ComputeDailyRollups,
        ComputeDailyRollupsInput,
    )

    await ComputeDailyRollups(in_memory_uow).execute(
        ComputeDailyRollupsInput(day_from=date(2024, 6, 1), day_to=date(2024, 6, 1))
    )

    async with await _tenant_client(tenant, TenantRole.OWNER, in_memory_uow, monkeypatch) as client:
        resp = await client.get(
            f"/api/v1/enterprise/tenants/{tenant.id}/usage",
            params={"day_from": "2024-06-01", "day_to": "2024-06-01"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        claim_rollups = [r for r in body["rollups"] if r["metric_kind"] == "TENANT_CLAIM_INGESTED"]
        assert claim_rollups and claim_rollups[0]["count"] == 1


@pytest.mark.asyncio
async def test_cross_tenant_usage_read_403(in_memory_uow, monkeypatch):
    tenant_a = _seed_tenant(in_memory_uow, slug="a")
    tenant_b = _seed_tenant(in_memory_uow, slug="b")
    async with await _tenant_client(
        tenant_a, TenantRole.OWNER, in_memory_uow, monkeypatch
    ) as client:
        resp = await client.get(
            f"/api/v1/enterprise/tenants/{tenant_b.id}/usage",
            params={"day_from": "2024-06-01", "day_to": "2024-06-01"},
        )
        assert resp.status_code == 403


# ── Auth gates ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_admin_endpoints_require_auth(client: AsyncClient):
    r1 = await client.get(
        "/api/v1/admin/usage/summary",
        params={"day_from": "2024-06-01", "day_to": "2024-06-01"},
    )
    assert r1.status_code in (401, 403)
    r2 = await client.post(
        "/api/v1/admin/usage/rollups",
        json={"day_from": "2024-06-01", "day_to": "2024-06-01"},
    )
    assert r2.status_code in (401, 403)
