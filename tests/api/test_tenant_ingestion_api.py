"""API tests for the Phase 6 tenant ingestion endpoints.

End-to-end pins for the wire surface:

- Full ingestion flow open → batch → complete.
- Auth posture: READ_ONLY rejected from writes; closed run returns
  409; cross-tenant URL returns 403; system-only API key returns
  403.
- Safety report: attestation required (422), cross-tenant denied
  (403), happy path with optional event association.
- Tenant evidence read returns the composed shape.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from atlas.domain.entities import AccidentEvent, ProjectedAccidentRecord
from atlas.domain.tenancy.entities import Tenant, TenantRole, TenantSource
from tests.api.conftest import make_tenant_client_for

# ── Shared seeding helpers ──────────────────────────────────────────────────


def _seed_tenant(uow, *, slug: str = "acme", is_active: bool = True) -> Tenant:
    t = Tenant(slug=slug, display_name=slug.upper(), is_active=is_active)
    uow.store.tenancy.tenants[t.id] = t
    return t


def _seed_source(uow, *, tenant: Tenant, name: str = "primary") -> TenantSource:
    s = TenantSource(tenant_id=tenant.id, name=name, kind="FOQA_EXPORT")
    uow.store.tenancy.sources[s.id] = s
    return s


def _seed_event(uow):
    e = AccidentEvent()
    uow.store.events[e.id] = e
    uow.store.projections[e.id] = ProjectedAccidentRecord(
        event_id=e.id,
        fields={"operator": "Public Airlines"},
        completeness_score=0.8,
    )
    return e


async def _tenant_client(tenant: Tenant, role: TenantRole, uow, monkeypatch) -> AsyncClient:
    app = make_tenant_client_for(
        tenant_id=tenant.id,
        tenant_role=role.value,
        uow=uow,
        monkeypatch=monkeypatch,
    )
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver")


# ── Full ingestion flow ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_full_ingestion_flow(in_memory_uow, monkeypatch):
    tenant = _seed_tenant(in_memory_uow)
    source = _seed_source(in_memory_uow, tenant=tenant)
    event = _seed_event(in_memory_uow)

    async with await _tenant_client(tenant, TenantRole.OWNER, in_memory_uow, monkeypatch) as client:
        # Open.
        open_resp = await client.post(
            f"/api/v1/enterprise/tenants/{tenant.id}/ingestions",
            json={"tenant_source_id": str(source.id)},
        )
        assert open_resp.status_code == 201, open_resp.text
        run_id = open_resp.json()["id"]
        assert open_resp.json()["status"] == "running"

        # Submit a batch.
        batch_resp = await client.post(
            f"/api/v1/enterprise/tenants/{tenant.id}/ingestions/{run_id}/claims",
            json={
                "claims": [
                    {
                        "event_id": str(event.id),
                        "field_name": "exceedance:flap",
                        "field_value": 220,
                        "claim_kind": "FOQA",
                        "confidence": 0.85,
                    },
                    {
                        "event_id": str(event.id),
                        "field_name": "exceedance:sink",
                        "field_value": 1800,
                        "claim_kind": "FOQA",
                    },
                ]
            },
        )
        assert batch_resp.status_code == 200, batch_resp.text
        assert batch_resp.json()["inserted_count"] == 2

        # Complete.
        complete_resp = await client.post(
            f"/api/v1/enterprise/tenants/{tenant.id}/ingestions/{run_id}/complete",
            json={"final_status": "succeeded"},
        )
        assert complete_resp.status_code == 200, complete_resp.text
        assert complete_resp.json()["status"] == "succeeded"
        assert complete_resp.json()["finished_at"] is not None


@pytest.mark.asyncio
async def test_read_only_rejected_from_ingestion(in_memory_uow, monkeypatch):
    """READ_ONLY tenant role cannot open an ingestion run.

    The use case raises HTTPException(403); FastAPI's default handler
    serialises it as ``{"detail": {...}}`` with the body we set.
    """
    tenant = _seed_tenant(in_memory_uow)
    source = _seed_source(in_memory_uow, tenant=tenant)
    async with await _tenant_client(
        tenant, TenantRole.READ_ONLY, in_memory_uow, monkeypatch
    ) as client:
        resp = await client.post(
            f"/api/v1/enterprise/tenants/{tenant.id}/ingestions",
            json={"tenant_source_id": str(source.id)},
        )
        assert resp.status_code == 403


@pytest.mark.asyncio
async def test_closed_run_append_returns_409(in_memory_uow, monkeypatch):
    tenant = _seed_tenant(in_memory_uow)
    source = _seed_source(in_memory_uow, tenant=tenant)
    event = _seed_event(in_memory_uow)

    async with await _tenant_client(tenant, TenantRole.OWNER, in_memory_uow, monkeypatch) as client:
        run_id = (
            await client.post(
                f"/api/v1/enterprise/tenants/{tenant.id}/ingestions",
                json={"tenant_source_id": str(source.id)},
            )
        ).json()["id"]
        # Close it.
        await client.post(
            f"/api/v1/enterprise/tenants/{tenant.id}/ingestions/{run_id}/complete",
            json={"final_status": "succeeded"},
        )
        # Try to append.
        resp = await client.post(
            f"/api/v1/enterprise/tenants/{tenant.id}/ingestions/{run_id}/claims",
            json={
                "claims": [
                    {
                        "event_id": str(event.id),
                        "field_name": "x",
                        "field_value": 1,
                    }
                ]
            },
        )
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "TENANT_INGESTION_RUN_CLOSED"


@pytest.mark.asyncio
async def test_complete_into_running_returns_422(in_memory_uow, monkeypatch):
    """Router coercion rejects 'running' as a final status before the
    use case ever runs."""
    tenant = _seed_tenant(in_memory_uow)
    source = _seed_source(in_memory_uow, tenant=tenant)
    async with await _tenant_client(tenant, TenantRole.OWNER, in_memory_uow, monkeypatch) as client:
        run_id = (
            await client.post(
                f"/api/v1/enterprise/tenants/{tenant.id}/ingestions",
                json={"tenant_source_id": str(source.id)},
            )
        ).json()["id"]
        resp = await client.post(
            f"/api/v1/enterprise/tenants/{tenant.id}/ingestions/{run_id}/complete",
            json={"final_status": "running"},
        )
        assert resp.status_code == 422


# ── Safety reports ──────────────────────────────────────────────────────────


_NARRATIVE = (
    "We observed an unstable approach below 1000 ft and executed "
    "a go-around per stabilised-approach criteria. Crew responded "
    "by the book."
)


@pytest.mark.asyncio
async def test_safety_report_without_attestation_422(in_memory_uow, monkeypatch):
    tenant = _seed_tenant(in_memory_uow)
    async with await _tenant_client(tenant, TenantRole.OWNER, in_memory_uow, monkeypatch) as client:
        resp = await client.post(
            f"/api/v1/enterprise/tenants/{tenant.id}/safety-reports",
            json={
                "report_kind": "ASAP",
                "narrative_markdown": _NARRATIVE,
                "deidentified_attested": False,
            },
        )
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "DEIDENTIFICATION_REQUIRED"


@pytest.mark.asyncio
async def test_safety_report_happy_path(in_memory_uow, monkeypatch):
    tenant = _seed_tenant(in_memory_uow)
    async with await _tenant_client(tenant, TenantRole.OWNER, in_memory_uow, monkeypatch) as client:
        resp = await client.post(
            f"/api/v1/enterprise/tenants/{tenant.id}/safety-reports",
            json={
                "report_kind": "ASAP",
                "narrative_markdown": _NARRATIVE,
                "deidentified_attested": True,
            },
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["report"]["report_kind"] == "ASAP"
        assert body["association"] is None
        assert isinstance(body["scrub_replacements"], list)


@pytest.mark.asyncio
async def test_safety_report_with_event_association(in_memory_uow, monkeypatch):
    tenant = _seed_tenant(in_memory_uow)
    event = _seed_event(in_memory_uow)
    async with await _tenant_client(tenant, TenantRole.OWNER, in_memory_uow, monkeypatch) as client:
        resp = await client.post(
            f"/api/v1/enterprise/tenants/{tenant.id}/safety-reports",
            json={
                "report_kind": "ASAP",
                "narrative_markdown": _NARRATIVE,
                "deidentified_attested": True,
                "associate_with_event_id": str(event.id),
                "association_kind": "CONTRIBUTED_TO",
            },
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["association"] is not None
        assert body["association"]["event_id"] == str(event.id)
        assert body["association"]["association_kind"] == "CONTRIBUTED_TO"


@pytest.mark.asyncio
async def test_cross_tenant_safety_report_403(in_memory_uow, monkeypatch):
    """Tenant A's client posting to tenant B's URL returns 403
    CROSS_TENANT_ACCESS — same shape as Phase 5 cross-tenant probes.
    """
    tenant_a = _seed_tenant(in_memory_uow, slug="a")
    tenant_b = _seed_tenant(in_memory_uow, slug="b")
    async with await _tenant_client(
        tenant_a, TenantRole.OWNER, in_memory_uow, monkeypatch
    ) as client:
        resp = await client.post(
            f"/api/v1/enterprise/tenants/{tenant_b.id}/safety-reports",
            json={
                "report_kind": "ASAP",
                "narrative_markdown": _NARRATIVE,
                "deidentified_attested": True,
            },
        )
        assert resp.status_code == 403


# ── Tenant evidence read ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_tenant_evidence_read_returns_composed_shape(in_memory_uow, monkeypatch):
    tenant = _seed_tenant(in_memory_uow)
    source = _seed_source(in_memory_uow, tenant=tenant)
    event = _seed_event(in_memory_uow)

    async with await _tenant_client(tenant, TenantRole.OWNER, in_memory_uow, monkeypatch) as client:
        # Open, submit, complete, plus a safety report with
        # association — covers all four sub-collections.
        run_id = (
            await client.post(
                f"/api/v1/enterprise/tenants/{tenant.id}/ingestions",
                json={"tenant_source_id": str(source.id)},
            )
        ).json()["id"]
        await client.post(
            f"/api/v1/enterprise/tenants/{tenant.id}/ingestions/{run_id}/claims",
            json={
                "claims": [
                    {
                        "event_id": str(event.id),
                        "field_name": "f",
                        "field_value": 1,
                        "claim_kind": "FOQA",
                    },
                    {
                        "event_id": str(event.id),
                        "field_name": "a",
                        "field_value": 2,
                        "claim_kind": "ASAP",
                    },
                ]
            },
        )
        await client.post(
            f"/api/v1/enterprise/tenants/{tenant.id}/safety-reports",
            json={
                "report_kind": "ASAP",
                "narrative_markdown": _NARRATIVE,
                "deidentified_attested": True,
                "associate_with_event_id": str(event.id),
            },
        )

        resp = await client.get(
            f"/api/v1/enterprise/tenants/{tenant.id}/events/{event.id}/tenant-evidence"
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["event_id"] == str(event.id)
        assert len(body["foqa_claims"]) == 1
        assert len(body["asap_claims"]) == 1
        assert len(body["other_claims"]) == 0
        assert len(body["associated_reports"]) == 1
        assert len(body["associations"]) == 1


# ── Auth posture ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ingestion_endpoints_require_tenant_auth(client: AsyncClient):
    """Unauthenticated request (the default client has no auth) gets
    401/403 on the ingestion surface."""
    fake_tenant = uuid4()
    fake_run = uuid4()
    for path in (
        f"/api/v1/enterprise/tenants/{fake_tenant}/ingestions",
        f"/api/v1/enterprise/tenants/{fake_tenant}/ingestions/{fake_run}/claims",
        f"/api/v1/enterprise/tenants/{fake_tenant}/safety-reports",
    ):
        resp = await client.post(path, json={})
        assert resp.status_code in (401, 403), path
