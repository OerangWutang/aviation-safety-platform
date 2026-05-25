"""Tests verifying the Echo crossref polling contract response shapes.

A frontend developer polling GET /reports/{id}/crossref/{result_id} needs to
handle three states: PENDING, COMPLETE, and FAILED.  These tests pin the
exact shape of each so any breaking schema change is caught immediately.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from atlas.domain.tenancy.entities import (
    CrossrefResultStatus,
    Tenant,
    TenantCrossrefResult,
    TenantSafetyReport,
    TenantSafetyReportKind,
)
from tests.api.conftest import make_tenant_client_for
from tests.domain.fakes import InMemoryUnitOfWork


def _seed(uow: InMemoryUnitOfWork) -> tuple[Tenant, TenantSafetyReport]:
    t = Tenant(slug="polling-test", display_name="POLLING-TEST", is_active=True)
    uow.store.tenancy.tenants[t.id] = t
    r = TenantSafetyReport(
        tenant_id=t.id,
        report_kind=TenantSafetyReportKind.ASAP,
        narrative_markdown="Crosswind exceedance during approach.",
        deidentified_attested=True,
        submitter_user_id=uuid4(),
    )
    uow.store.tenancy.safety_reports[r.id] = r
    return t, r


def _seed_result(
    uow: InMemoryUnitOfWork,
    tenant: Tenant,
    report: TenantSafetyReport,
    status: CrossrefResultStatus,
    **kwargs,
) -> TenantCrossrefResult:

    result = TenantCrossrefResult(
        tenant_id=tenant.id,
        safety_report_id=report.id,
        status=status,
        **kwargs,
    )
    uow.store.tenancy.crossref_results[result.id] = result
    return result


async def _get(uow, tenant, report, result, monkeypatch):
    from atlas.domain.tenancy.entities import TenantRole

    app = make_tenant_client_for(
        tenant_id=tenant.id,
        tenant_role=TenantRole.MEMBER.value,
        uow=uow,
        monkeypatch=monkeypatch,
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as c:
        return await c.get(
            f"/api/v1/enterprise/tenants/{tenant.id}/reports/{report.id}/crossref/{result.id}"
        )


@pytest.mark.asyncio
async def test_pending_response_shape(in_memory_uow, monkeypatch):
    """PENDING: matches is empty, completed_at and error_detail are null."""
    tenant, report = _seed(in_memory_uow)
    result = _seed_result(in_memory_uow, tenant, report, CrossrefResultStatus.PENDING)

    resp = await _get(in_memory_uow, tenant, report, result, monkeypatch)

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "PENDING"
    assert body["matches"] == []
    assert body["match_count"] == 0
    assert body["completed_at"] is None
    assert body["error_detail"] is None
    # Frontend should keep polling — no terminal fields populated.
    assert "crossref_result_id" not in body  # not the POST response shape
    assert "id" in body


@pytest.mark.asyncio
async def test_complete_response_shape(in_memory_uow, monkeypatch):
    """COMPLETE: matches populated, completed_at set, error_detail null."""
    from datetime import UTC, datetime

    tenant, report = _seed(in_memory_uow)
    matches_json = [
        {
            "event_id": "WPR20LA123",
            "score": 0.88,
            "support": "STRONG",
            "components": [
                {
                    "name": "finding_categories",
                    "weight": 0.5,
                    "score": 1.0,
                    "detail": "2 shared cause categories",
                }
            ],
            "shared_finding_categories": ["01.06"],
            "shared_terms": ["crosswind", "landing"],
            "display_occurred_on": "2020-06-15",
            "display_location": "Mesa, AZ",
            "display_aircraft": "Piper PA-28",
            "display_probable_cause": "Failure to maintain directional control.",
        }
    ]
    result = _seed_result(
        in_memory_uow,
        tenant,
        report,
        CrossrefResultStatus.COMPLETE,
        matches_json=matches_json,
        match_count=1,
        completed_at=datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC),
        matcher_config_json={"weights": {"finding_categories": 0.5}},
    )

    resp = await _get(in_memory_uow, tenant, report, result, monkeypatch)

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "COMPLETE"
    assert len(body["matches"]) == 1
    assert body["match_count"] == 1
    assert body["completed_at"] is not None
    assert body["error_detail"] is None

    m = body["matches"][0]
    assert m["event_id"] == "WPR20LA123"
    assert m["score"] == 0.88
    assert m["support"] == "STRONG"
    assert len(m["components"]) == 1
    assert m["components"][0]["name"] == "finding_categories"
    assert m["shared_finding_categories"] == ["01.06"]
    assert "crosswind" in m["shared_terms"]
    assert m["display_location"] == "Mesa, AZ"
    # score is similarity, not probability — field must not be renamed
    assert "probability" not in m


@pytest.mark.asyncio
async def test_failed_response_shape(in_memory_uow, monkeypatch):
    """FAILED: matches empty, error_detail populated, completed_at set."""
    from datetime import UTC, datetime

    tenant, report = _seed(in_memory_uow)
    result = _seed_result(
        in_memory_uow,
        tenant,
        report,
        CrossrefResultStatus.FAILED,
        error_detail="Corpus load failed: connection refused",
        completed_at=datetime(2026, 1, 1, 12, 0, 1, tzinfo=UTC),
    )

    resp = await _get(in_memory_uow, tenant, report, result, monkeypatch)

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "FAILED"
    assert body["matches"] == []
    assert body["match_count"] == 0
    assert body["error_detail"] == "Corpus load failed: connection refused"
    assert body["completed_at"] is not None


@pytest.mark.asyncio
async def test_post_returns_202_with_pending_status(in_memory_uow, monkeypatch):
    """POST returns 202 immediately; status is PENDING so the frontend knows to poll."""
    import atlas.presentation.api.routers.tenancy as _tenancy_router
    from atlas.domain.tenancy.entities import TenantRole

    async def _noop_bg(*_a, **_kw):
        pass

    monkeypatch.setattr(_tenancy_router, "_run_crossref_background", _noop_bg)

    tenant, report = _seed(in_memory_uow)
    app = make_tenant_client_for(
        tenant_id=tenant.id,
        tenant_role=TenantRole.MEMBER.value,
        uow=in_memory_uow,
        monkeypatch=monkeypatch,
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as c:
        resp = await c.post(
            f"/api/v1/enterprise/tenants/{tenant.id}/reports/{report.id}/crossref",
            json={},
        )

    assert resp.status_code == 202
    body = resp.json()
    assert body["status"] == "PENDING"
    assert "crossref_result_id" in body
    # poll_url must be present and contain the result_id — the frontend
    # follows this URL directly without constructing it from parts.
    assert "poll_url" in body
    assert body["crossref_result_id"] in body["poll_url"]
    # The result_id must be a valid UUID.
    from uuid import UUID

    UUID(body["crossref_result_id"])  # raises ValueError if malformed
