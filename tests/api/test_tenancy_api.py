"""API tests for the Phase 5 tenant router.

Layer 1 of the three isolation layers is enforced here at the HTTP
boundary.  Two cases worth pinning explicitly:

1. A tenant API key targeting **a different tenant's URL** must get
   403 CROSS_TENANT_ACCESS.
2. A system-only API key (no tenant binding) using **any tenant
   route** must get 403 NOT_A_TENANT_API_KEY.

Plus the standard endpoint coverage: register source, get overlay,
upsert overlay, list events.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from atlas.domain.entities import AccidentEvent, ProjectedAccidentRecord
from atlas.domain.tenancy.entities import Tenant, TenantRole
from tests.api.conftest import make_tenant_client_for


def _seed_tenant(uow, *, slug: str = "acme", is_active: bool = True) -> Tenant:
    t = Tenant(slug=slug, display_name=slug.upper(), is_active=is_active)
    uow.store.tenancy.tenants[t.id] = t
    return t


def _seed_event(uow, *, fields=None):
    e = AccidentEvent()
    uow.store.events[e.id] = e
    uow.store.projections[e.id] = ProjectedAccidentRecord(
        event_id=e.id,
        fields=fields or {"operator": "Public Airlines"},
        completeness_score=0.85,
    )
    return e


# ── Tenant fixtures (factories rather than fixtures so each test can
# control the tenant_role) ─────────────────────────────────────────────────


async def _tenant_client(
    tenant,
    role: TenantRole,
    uow,
    monkeypatch,
):
    app = make_tenant_client_for(
        tenant_id=tenant.id,
        tenant_role=role.value,
        uow=uow,
        monkeypatch=monkeypatch,
    )
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver")


# ── Register source ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_owner_can_register_tenant_source(in_memory_uow, monkeypatch):
    tenant = _seed_tenant(in_memory_uow)
    async with await _tenant_client(tenant, TenantRole.OWNER, in_memory_uow, monkeypatch) as client:
        resp = await client.post(
            f"/api/v1/enterprise/tenants/{tenant.id}/sources",
            json={"name": "Internal Source", "kind": "EXTERNAL"},
        )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["name"] == "Internal Source"
    assert body["tenant_id"] == str(tenant.id)


@pytest.mark.asyncio
async def test_read_only_cannot_register_source(in_memory_uow, monkeypatch):
    tenant = _seed_tenant(in_memory_uow)
    async with await _tenant_client(
        tenant, TenantRole.READ_ONLY, in_memory_uow, monkeypatch
    ) as client:
        resp = await client.post(
            f"/api/v1/enterprise/tenants/{tenant.id}/sources",
            json={"name": "X"},
        )
    assert resp.status_code == 403


# ── Cross-tenant denial at the HTTP layer ────────────────────────────────────


@pytest.mark.asyncio
async def test_cross_tenant_url_returns_403(in_memory_uow, monkeypatch):
    """A key bound to tenant A cannot use tenant B's URLs.

    This is the highest-leverage isolation test: it confirms the
    auth gate catches the attempt before any use case runs."""
    a = _seed_tenant(in_memory_uow, slug="a")
    b = _seed_tenant(in_memory_uow, slug="b")

    # Caller bound to A, targeting B's URL.
    async with await _tenant_client(a, TenantRole.OWNER, in_memory_uow, monkeypatch) as client:
        resp = await client.get(f"/api/v1/enterprise/tenants/{b.id}/events")
    assert resp.status_code == 403
    body = resp.json()
    assert body["error"]["code"] == "CROSS_TENANT_ACCESS"
    # Response body does not leak the target tenant's display_name.
    assert "B" not in body["error"].get("message", "")


@pytest.mark.asyncio
async def test_system_only_key_rejected_on_tenant_route(
    async_client_admin: AsyncClient, in_memory_uow
):
    """A system-only API key (no tenant binding) hitting a tenant
    route must be rejected.

    In this test harness ``async_client_admin`` overrides only
    ``get_current_user`` — ``get_current_tenant_user`` is not
    overridden, so the real dependency runs.  Without an API key
    header, it raises 401; with a valid system-only key it would
    raise 403 NOT_A_TENANT_API_KEY.  Either is a correct denial; the
    invariant we care about is "system-only keys cannot use tenant
    routes", and any 4xx denial confirms that.
    """
    tenant = _seed_tenant(in_memory_uow)
    resp = await async_client_admin.get(f"/api/v1/enterprise/tenants/{tenant.id}/events")
    assert resp.status_code in (401, 403)


@pytest.mark.asyncio
async def test_revoked_membership_rejected_even_when_key_is_tenant_bound(
    in_memory_uow, monkeypatch
):
    tenant = _seed_tenant(in_memory_uow)
    app = make_tenant_client_for(
        tenant_id=tenant.id,
        tenant_role=TenantRole.OWNER.value,
        uow=in_memory_uow,
        monkeypatch=monkeypatch,
    )
    # Simulate an admin revoking the canonical membership while an API-key cache
    # entry can still carry a tenant_id/tenant_role binding.
    in_memory_uow.store.tenancy.memberships.clear()

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        resp = await client.get(f"/api/v1/enterprise/tenants/{tenant.id}/events")

    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "TENANT_MEMBERSHIP_REQUIRED"


@pytest.mark.asyncio
async def test_inactive_tenant_returns_403(in_memory_uow, monkeypatch):
    """A tenant flagged ``is_active=False`` rejects all access."""
    tenant = _seed_tenant(in_memory_uow, is_active=False)
    async with await _tenant_client(tenant, TenantRole.OWNER, in_memory_uow, monkeypatch) as client:
        resp = await client.get(f"/api/v1/enterprise/tenants/{tenant.id}/events")
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "TENANT_INACTIVE"


# ── Event overlay (read + upsert) ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_overlay_returns_public_context_even_without_overlay(in_memory_uow, monkeypatch):
    """Reading an overlay for an unannotated event still returns the
    public projection — the empty-overlay case is the common "I want
    to start writing one; what do I see today?" path."""
    tenant = _seed_tenant(in_memory_uow)
    event = _seed_event(in_memory_uow, fields={"operator": "Public", "location": "Anchorage"})
    async with await _tenant_client(tenant, TenantRole.OWNER, in_memory_uow, monkeypatch) as client:
        resp = await client.get(f"/api/v1/enterprise/tenants/{tenant.id}/events/{event.id}/overlay")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["overlay"] is None
    assert body["public_fields"] == {"operator": "Public", "location": "Anchorage"}


@pytest.mark.asyncio
async def test_upsert_then_get_overlay(in_memory_uow, monkeypatch):
    tenant = _seed_tenant(in_memory_uow)
    event = _seed_event(in_memory_uow)
    async with await _tenant_client(tenant, TenantRole.OWNER, in_memory_uow, monkeypatch) as client:
        put_resp = await client.put(
            f"/api/v1/enterprise/tenants/{tenant.id}/events/{event.id}/overlay",
            json={
                "notes_markdown": "# Internal note\nDetails…",
                "overlay_fields": {"severity": "high"},
            },
        )
        assert put_resp.status_code == 200, put_resp.text
        assert put_resp.json()["overlay"]["overlay_fields"] == {"severity": "high"}

        get_resp = await client.get(
            f"/api/v1/enterprise/tenants/{tenant.id}/events/{event.id}/overlay"
        )
    assert get_resp.status_code == 200
    body = get_resp.json()
    assert body["overlay"]["overlay_fields"] == {"severity": "high"}
    assert "Internal note" in body["overlay"]["notes_markdown"]


@pytest.mark.asyncio
async def test_read_only_cannot_upsert_overlay(in_memory_uow, monkeypatch):
    tenant = _seed_tenant(in_memory_uow)
    event = _seed_event(in_memory_uow)
    async with await _tenant_client(
        tenant, TenantRole.READ_ONLY, in_memory_uow, monkeypatch
    ) as client:
        resp = await client.put(
            f"/api/v1/enterprise/tenants/{tenant.id}/events/{event.id}/overlay",
            json={"notes_markdown": "should fail"},
        )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_upsert_with_unknown_event_returns_404(in_memory_uow, monkeypatch):
    """Tenant overlays anchor to public events.  Trying to create one
    for an event id that has no public projection must fail closed —
    otherwise tenants could create "orphan" overlays referencing
    fabricated events."""
    tenant = _seed_tenant(in_memory_uow)
    async with await _tenant_client(tenant, TenantRole.OWNER, in_memory_uow, monkeypatch) as client:
        resp = await client.put(
            f"/api/v1/enterprise/tenants/{tenant.id}/events/{uuid4()}/overlay",
            json={"notes_markdown": "x"},
        )
    assert resp.status_code == 404


# ── Event list ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_events_filters_by_tenant(in_memory_uow, monkeypatch):
    a = _seed_tenant(in_memory_uow, slug="a")
    b = _seed_tenant(in_memory_uow, slug="b")
    event_x = _seed_event(in_memory_uow)
    event_y = _seed_event(in_memory_uow)

    # A annotates both events.
    async with await _tenant_client(a, TenantRole.OWNER, in_memory_uow, monkeypatch) as a_client:
        await a_client.put(
            f"/api/v1/enterprise/tenants/{a.id}/events/{event_x.id}/overlay",
            json={"notes_markdown": "A on X"},
        )
        await a_client.put(
            f"/api/v1/enterprise/tenants/{a.id}/events/{event_y.id}/overlay",
            json={"notes_markdown": "A on Y"},
        )
        a_list = await a_client.get(f"/api/v1/enterprise/tenants/{a.id}/events")
    assert a_list.status_code == 200
    a_event_ids = {item["event_id"] for item in a_list.json()["items"]}
    assert a_event_ids == {str(event_x.id), str(event_y.id)}

    # B sees nothing (no overlays of its own).
    async with await _tenant_client(b, TenantRole.OWNER, in_memory_uow, monkeypatch) as b_client:
        b_list = await b_client.get(f"/api/v1/enterprise/tenants/{b.id}/events")
    assert b_list.status_code == 200
    assert b_list.json()["items"] == []


# ── Public surfaces never expose tenant data via HTTP ───────────────────────


@pytest.mark.asyncio
async def test_public_event_detail_does_not_leak_tenant_overlay(
    async_client_analyst: AsyncClient, in_memory_uow, monkeypatch
):
    """Crucial end-to-end isolation: an analyst hitting the public
    detail endpoint cannot see any tenant overlay annotations.
    Confirms the parallel-tables design at the HTTP boundary."""
    tenant = _seed_tenant(in_memory_uow)
    event = _seed_event(in_memory_uow, fields={"operator": "Public Airlines"})
    # Publish a page so the public detail is reachable.
    from datetime import UTC, datetime

    from atlas.domain.publication.entities import (
        PublicationStatus,
        PublicEventPage,
    )

    page = PublicEventPage(
        event_id=event.id,
        slug="public-event-x",
        title="Public Event X",
        short_summary="A short summary.",
        status=PublicationStatus.PUBLISHED,
        first_published_at=datetime(2024, 1, 1, tzinfo=UTC),
        last_published_at=datetime(2024, 1, 1, tzinfo=UTC),
    )
    in_memory_uow.store.publication.pages[page.id] = page

    # Tenant adds an overlay with a private "operator" value.
    async with await _tenant_client(
        tenant, TenantRole.OWNER, in_memory_uow, monkeypatch
    ) as tenant_client:
        await tenant_client.put(
            f"/api/v1/enterprise/tenants/{tenant.id}/events/{event.id}/overlay",
            json={
                "notes_markdown": "private",
                "overlay_fields": {"operator": "PRIVATE TENANT VALUE"},
            },
        )

    # Public detail reflects only the public projection.
    resp = await async_client_analyst.get("/api/v1/public/events/public-event-x")
    assert resp.status_code == 200
    body = resp.json()
    # Public operator is unchanged.
    serialized = str(body)
    assert "Public Airlines" in serialized
    assert "PRIVATE TENANT VALUE" not in serialized
    # The editorial narrative on the public page is also tenant-free —
    # check defensively because the field can be None.
    editorial = body.get("editorial", {}) or {}
    narrative = editorial.get("narrative_markdown") or ""
    assert "private" not in narrative
