"""API auth tests for the Echo cross-reference endpoints.

Covers the four auth postures that matter before a frontend can safely
use these endpoints:

1. No API key → the routes require authentication (401/403).
2. Cross-tenant access → tenant A cannot request cross-reference on
   tenant B's report (403 CROSS_TENANT_ACCESS).
3. READ_ONLY role → cannot trigger a cross-reference run (write
   operation), but CAN poll for an existing result (read operation).
4. MEMBER / OWNER → can trigger and poll (happy paths).

The outbox worker path (``RunEchoCrossReference``) is deliberately NOT
exercised here — it requires a live corpus and worker-owned database UoWs.
These tests verify only the auth gate and the immediate HTTP surface of the
two endpoints.  The use-case logic is covered by
``tests/application/use_cases/test_echo_crossref.py``.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from atlas.domain.tenancy.entities import (
    Tenant,
    TenantRole,
    TenantSafetyReport,
    TenantSafetyReportKind,
)
from tests.api.conftest import make_tenant_client_for

# ── Fixed UUIDs for stable pytest node IDs (--lf) ───────────────────────────

_TENANT_ID = "aaaaaaaa-0000-0000-0000-000000000001"
_REPORT_ID = "bbbbbbbb-0000-0000-0000-000000000002"
_RESULT_ID = "cccccccc-0000-0000-0000-000000000003"
_OTHER_TENANT_ID = "dddddddd-0000-0000-0000-000000000004"

_POST_URL = f"/api/v1/enterprise/tenants/{_TENANT_ID}/reports/{_REPORT_ID}/crossref"
_GET_URL = f"/api/v1/enterprise/tenants/{_TENANT_ID}/reports/{_REPORT_ID}/crossref/{_RESULT_ID}"


# ── Seeding helpers ──────────────────────────────────────────────────────────


def _seed_tenant(uow, *, tenant_id=None, slug="acme") -> Tenant:
    from uuid import UUID

    t = Tenant(
        id=UUID(tenant_id) if tenant_id else uuid4(),
        slug=slug,
        display_name=slug.upper(),
        is_active=True,
    )
    uow.store.tenancy.tenants[t.id] = t
    return t


def _seed_report(uow, *, tenant: Tenant, report_id=None) -> TenantSafetyReport:
    from uuid import UUID

    r = TenantSafetyReport(
        id=UUID(report_id) if report_id else uuid4(),
        tenant_id=tenant.id,
        report_kind=TenantSafetyReportKind.ASAP,
        narrative_markdown="Crosswind landing exceedance during approach.",
        deidentified_attested=True,
        submitter_user_id=uuid4(),
    )
    uow.store.tenancy.safety_reports[r.id] = r
    return r


async def _client(tenant: Tenant, role: TenantRole, uow, monkeypatch) -> AsyncClient:
    app = make_tenant_client_for(
        tenant_id=tenant.id,
        tenant_role=role.value,
        uow=uow,
        monkeypatch=monkeypatch,
    )
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver")


def _client_no_bg(tenant: Tenant, role: TenantRole, uow, monkeypatch) -> AsyncClient:
    """Client for request tests; execution is deferred to the outbox worker."""
    app = make_tenant_client_for(
        tenant_id=tenant.id,
        tenant_role=role.value,
        uow=uow,
        monkeypatch=monkeypatch,
    )
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver")


# ── 1. Unauthenticated requests are rejected ─────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method,url",
    [
        ("POST", _POST_URL),
        ("GET", _GET_URL),
    ],
)
async def test_echo_endpoints_require_authentication(method, url, client):
    """No API key → 401 or 403; never a 2xx."""
    if method == "POST":
        r = await client.post(url, json={})
    else:
        r = await client.get(url)
    assert r.status_code in (401, 403), (
        f"{method} {url} returned {r.status_code}; expected 401/403 for unauthenticated request"
    )


# ── 2. Cross-tenant access is rejected ───────────────────────────────────────


@pytest.mark.asyncio
async def test_cross_tenant_crossref_request_403(in_memory_uow, monkeypatch):
    """Tenant A's client POSTing to tenant B's report URL → 403."""
    tenant_a = _seed_tenant(in_memory_uow, slug="alpha")
    tenant_b = _seed_tenant(in_memory_uow, slug="beta")
    report_b = _seed_report(in_memory_uow, tenant=tenant_b)

    async with await _client(tenant_a, TenantRole.OWNER, in_memory_uow, monkeypatch) as c:
        resp = await c.post(
            f"/api/v1/enterprise/tenants/{tenant_b.id}/reports/{report_b.id}/crossref",
            json={},
        )
    assert resp.status_code == 403, (
        f"Expected 403 for cross-tenant crossref request, got {resp.status_code}: {resp.text}"
    )


@pytest.mark.asyncio
async def test_cross_tenant_crossref_get_403(in_memory_uow, monkeypatch):
    """Tenant A's client GETting tenant B's result URL → 403."""
    tenant_a = _seed_tenant(in_memory_uow, slug="alpha2")
    tenant_b = _seed_tenant(in_memory_uow, slug="beta2")

    async with await _client(tenant_a, TenantRole.OWNER, in_memory_uow, monkeypatch) as c:
        resp = await c.get(
            f"/api/v1/enterprise/tenants/{tenant_b.id}/reports/{uuid4()}/crossref/{uuid4()}",
        )
    # The require_tenant_membership dependency fires before any repo access.
    assert resp.status_code == 403, (
        f"Expected 403 for cross-tenant GET, got {resp.status_code}: {resp.text}"
    )


# ── 3. READ_ONLY role is rejected from the write endpoint ───────────────────


@pytest.mark.asyncio
async def test_read_only_cannot_request_crossref(in_memory_uow, monkeypatch):
    """READ_ONLY tenant members cannot trigger a cross-reference run."""
    tenant = _seed_tenant(in_memory_uow, slug="ro-tenant")
    report = _seed_report(in_memory_uow, tenant=tenant)

    async with _client_no_bg(tenant, TenantRole.READ_ONLY, in_memory_uow, monkeypatch) as c:
        resp = await c.post(
            f"/api/v1/enterprise/tenants/{tenant.id}/reports/{report.id}/crossref",
            json={},
        )
    assert resp.status_code == 403, (
        f"Expected 403 for READ_ONLY crossref request, got {resp.status_code}: {resp.text}"
    )


@pytest.mark.asyncio
async def test_read_only_can_poll_crossref_result(in_memory_uow, monkeypatch):
    """READ_ONLY tenant members CAN poll for an existing result (read-only operation)."""

    from atlas.domain.tenancy.entities import CrossrefResultStatus, TenantCrossrefResult

    tenant = _seed_tenant(in_memory_uow, slug="ro-poll-tenant")
    report = _seed_report(in_memory_uow, tenant=tenant)
    result = TenantCrossrefResult(
        tenant_id=tenant.id,
        safety_report_id=report.id,
        status=CrossrefResultStatus.COMPLETE,
        match_count=3,
    )
    in_memory_uow.store.tenancy.crossref_results[result.id] = result

    async with await _client(tenant, TenantRole.READ_ONLY, in_memory_uow, monkeypatch) as c:
        resp = await c.get(
            f"/api/v1/enterprise/tenants/{tenant.id}/reports/{report.id}/crossref/{result.id}",
        )
    assert resp.status_code == 200, (
        f"READ_ONLY should be able to poll a result, got {resp.status_code}: {resp.text}"
    )
    body = resp.json()
    assert body["status"] == "COMPLETE"
    assert body["match_count"] == 3


# ── 4. MEMBER / OWNER happy paths ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_member_can_request_crossref(in_memory_uow, monkeypatch):
    """MEMBER role can trigger a cross-reference (202 Accepted)."""
    tenant = _seed_tenant(in_memory_uow, slug="member-tenant")
    report = _seed_report(in_memory_uow, tenant=tenant)

    async with _client_no_bg(tenant, TenantRole.MEMBER, in_memory_uow, monkeypatch) as c:
        resp = await c.post(
            f"/api/v1/enterprise/tenants/{tenant.id}/reports/{report.id}/crossref",
            json={},
        )
    assert resp.status_code == 202, (
        f"Expected 202 for MEMBER crossref request, got {resp.status_code}: {resp.text}"
    )
    body = resp.json()
    assert "crossref_result_id" in body
    assert "poll_url" in body
    assert body["status"] == "PENDING"
    assert str(body["crossref_result_id"]) in body["poll_url"]


@pytest.mark.asyncio
async def test_owner_can_request_crossref(in_memory_uow, monkeypatch):
    """OWNER role can trigger a cross-reference (202 Accepted)."""
    tenant = _seed_tenant(in_memory_uow, slug="owner-tenant")
    report = _seed_report(in_memory_uow, tenant=tenant)

    async with _client_no_bg(tenant, TenantRole.OWNER, in_memory_uow, monkeypatch) as c:
        resp = await c.post(
            f"/api/v1/enterprise/tenants/{tenant.id}/reports/{report.id}/crossref",
            json={},
        )
    assert resp.status_code == 202, (
        f"Expected 202 for OWNER crossref request, got {resp.status_code}: {resp.text}"
    )


@pytest.mark.asyncio
async def test_get_nonexistent_result_returns_404(in_memory_uow, monkeypatch):
    """GET with an unknown result_id returns 404, not 500."""
    tenant = _seed_tenant(in_memory_uow, slug="get-404-tenant")
    report = _seed_report(in_memory_uow, tenant=tenant)

    async with await _client(tenant, TenantRole.MEMBER, in_memory_uow, monkeypatch) as c:
        resp = await c.get(
            f"/api/v1/enterprise/tenants/{tenant.id}/reports/{report.id}/crossref/{uuid4()}",
        )
    assert resp.status_code == 404, (
        f"Expected 404 for missing result, got {resp.status_code}: {resp.text}"
    )


@pytest.mark.asyncio
async def test_request_crossref_for_nonexistent_report_returns_404(in_memory_uow, monkeypatch):
    """POST for a report that doesn't exist returns 404, not 500."""
    tenant = _seed_tenant(in_memory_uow, slug="missing-report-tenant")

    async with await _client(tenant, TenantRole.OWNER, in_memory_uow, monkeypatch) as c:
        resp = await c.post(
            f"/api/v1/enterprise/tenants/{tenant.id}/reports/{uuid4()}/crossref",
            json={},
        )
    assert resp.status_code == 404, (
        f"Expected 404 for missing report, got {resp.status_code}: {resp.text}"
    )
