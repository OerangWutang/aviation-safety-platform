"""API tests for Phase 4 causality.

End-to-end HTTP coverage for:

- Public taxonomy endpoint.
- Public per-event HFACS / SHELO reads with visibility gating.
- Editorial attach/update/delete for HFACS attributions.
- Editorial attach/delete for SHELO factors and interactions.
- Auth gates and error code mappings.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from httpx import AsyncClient

from atlas.domain.causality.entities import (
    HfacsCategory,
    HfacsTier,
)
from atlas.domain.entities import AccidentEvent, ProjectedAccidentRecord
from atlas.domain.publication.entities import (
    PublicationStatus,
    PublicEventPage,
)

# ── Seeding helpers ─────────────────────────────────────────────────────────


def _seed_event_and_page(
    uow,
    *,
    slug: str,
    status: PublicationStatus = PublicationStatus.PUBLISHED,
    retraction_note: str | None = None,
):
    event = AccidentEvent()
    uow.store.events[event.id] = event
    uow.store.projections[event.id] = ProjectedAccidentRecord(
        event_id=event.id, fields={}, completeness_score=0.5
    )
    now = datetime(2024, 6, 1, tzinfo=UTC)
    page = PublicEventPage(
        event_id=event.id,
        slug=slug,
        title=slug.upper(),
        status=status,
        first_published_at=now
        if status in (PublicationStatus.PUBLISHED, PublicationStatus.RETRACTED)
        else None,
        last_published_at=now
        if status in (PublicationStatus.PUBLISHED, PublicationStatus.RETRACTED)
        else None,
        retracted_at=now if status == PublicationStatus.RETRACTED else None,
        retraction_note=retraction_note,
    )
    uow.store.publication.pages[page.id] = page
    return event, page


def _seed_hfacs_category(uow, *, code: str = "PRE-CRM") -> HfacsCategory:
    cat = HfacsCategory(
        tier_code=code.split("-")[0],
        code=code,
        tier=HfacsTier.PRECONDITIONS,
        name=code,
        description="x",
    )
    uow.store.causality.hfacs_categories[cat.id] = cat
    return cat


# ── Public: taxonomy ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_taxonomy_endpoint_returns_seeded_categories(
    async_client_analyst: AsyncClient, in_memory_uow
):
    _seed_hfacs_category(in_memory_uow, code="ACT-SBE")
    _seed_hfacs_category(in_memory_uow, code="ORG-RM")
    resp = await async_client_analyst.get("/api/v1/public/hfacs/taxonomy")
    assert resp.status_code == 200
    codes = [c["code"] for c in resp.json()["categories"]]
    assert codes == ["ACT-SBE", "ORG-RM"]


# ── Public: per-event reads ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_event_hfacs_endpoint_published(async_client_admin: AsyncClient, in_memory_uow):
    event, _ = _seed_event_and_page(in_memory_uow, slug="pub")
    cat = _seed_hfacs_category(in_memory_uow)
    # Attach via the editorial endpoint to exercise that surface too.
    attach_resp = await async_client_admin.post(
        f"/api/v1/editorial/events/{event.id}/hfacs",
        json={
            "category_id": str(cat.id),
            "confidence": 0.85,
            "note": "CRM",
        },
    )
    assert attach_resp.status_code == 201, attach_resp.text

    public_resp = await async_client_admin.get("/api/v1/public/events/pub/hfacs")
    assert public_resp.status_code == 200
    body = public_resp.json()
    assert body["event_id"] == str(event.id)
    assert len(body["attributions"]) == 1
    a = body["attributions"][0]
    assert a["category_code"] == "PRE-CRM"
    assert a["confidence"] == 0.85


@pytest.mark.asyncio
async def test_event_hfacs_draft_returns_404(async_client_analyst: AsyncClient, in_memory_uow):
    _seed_event_and_page(in_memory_uow, slug="wip", status=PublicationStatus.DRAFT)
    resp = await async_client_analyst.get("/api/v1/public/events/wip/hfacs")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "PUBLIC_EVENT_PAGE_NOT_PUBLISHED"


@pytest.mark.asyncio
async def test_event_hfacs_retracted_returns_410(async_client_analyst: AsyncClient, in_memory_uow):
    _seed_event_and_page(
        in_memory_uow,
        slug="gone",
        status=PublicationStatus.RETRACTED,
        retraction_note="Misattributed.",
    )
    resp = await async_client_analyst.get("/api/v1/public/events/gone/hfacs")
    assert resp.status_code == 410
    assert resp.json()["error"]["code"] == "PUBLIC_EVENT_PAGE_RETRACTED"


@pytest.mark.asyncio
async def test_event_shelo_endpoint_published(async_client_admin: AsyncClient, in_memory_uow):
    event, _ = _seed_event_and_page(in_memory_uow, slug="shelo-ok")
    # Attach two factors and one interaction via the editorial API.
    f1 = await async_client_admin.post(
        f"/api/v1/editorial/events/{event.id}/shelo/factors",
        json={
            "factor_class": "SOFTWARE",
            "label": "FADEC fault",
        },
    )
    assert f1.status_code == 201, f1.text
    f2 = await async_client_admin.post(
        f"/api/v1/editorial/events/{event.id}/shelo/factors",
        json={
            "factor_class": "LIVEWARE",
            "label": "fatigued pilot",
        },
    )
    assert f2.status_code == 201, f2.text
    interaction = await async_client_admin.post(
        f"/api/v1/editorial/events/{event.id}/shelo/interactions",
        json={
            "source_factor_id": f1.json()["id"],
            "target_factor_id": f2.json()["id"],
            "interaction_kind": "AGGRAVATED",
        },
    )
    assert interaction.status_code == 201, interaction.text

    public_resp = await async_client_admin.get("/api/v1/public/events/shelo-ok/shelo")
    assert public_resp.status_code == 200
    body = public_resp.json()
    assert len(body["factors"]) == 2
    assert len(body["interactions"]) == 1
    assert body["interactions"][0]["interaction_kind"] == "AGGRAVATED"


# ── Editorial: errors ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_attach_hfacs_with_unknown_category_returns_404(
    async_client_admin: AsyncClient, in_memory_uow
):
    event, _ = _seed_event_and_page(in_memory_uow, slug="x")
    resp = await async_client_admin.post(
        f"/api/v1/editorial/events/{event.id}/hfacs",
        json={
            "category_id": str(uuid4()),
            "confidence": 0.5,
        },
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "HFACS_CATEGORY_NOT_FOUND"


@pytest.mark.asyncio
async def test_attach_hfacs_duplicate_natural_key_returns_409(
    async_client_admin: AsyncClient, in_memory_uow
):
    event, _ = _seed_event_and_page(in_memory_uow, slug="y")
    cat = _seed_hfacs_category(in_memory_uow)
    body = {"category_id": str(cat.id), "confidence": 0.5}
    first = await async_client_admin.post(f"/api/v1/editorial/events/{event.id}/hfacs", json=body)
    assert first.status_code == 201
    dup = await async_client_admin.post(f"/api/v1/editorial/events/{event.id}/hfacs", json=body)
    assert dup.status_code == 409
    assert dup.json()["error"]["code"] == "HFACS_ATTRIBUTION_CONFLICT"


@pytest.mark.asyncio
async def test_update_hfacs_stale_version_returns_409(
    async_client_admin: AsyncClient, in_memory_uow
):
    event, _ = _seed_event_and_page(in_memory_uow, slug="z")
    cat = _seed_hfacs_category(in_memory_uow)
    attach = await async_client_admin.post(
        f"/api/v1/editorial/events/{event.id}/hfacs",
        json={"category_id": str(cat.id), "confidence": 0.5},
    )
    attribution_id = attach.json()["id"]
    # First update v1 succeeds.
    ok = await async_client_admin.put(
        f"/api/v1/editorial/events/{event.id}/hfacs/{attribution_id}",
        json={
            "expected_version": 1,
            "confidence": 0.7,
            "note": "revised",
        },
    )
    assert ok.status_code == 200
    # Stale v1 update returns 409.
    conflict = await async_client_admin.put(
        f"/api/v1/editorial/events/{event.id}/hfacs/{attribution_id}",
        json={"expected_version": 1, "confidence": 0.8},
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "HFACS_ATTRIBUTION_CONFLICT"


@pytest.mark.asyncio
async def test_delete_hfacs_returns_204(async_client_admin: AsyncClient, in_memory_uow):
    event, _ = _seed_event_and_page(in_memory_uow, slug="d")
    cat = _seed_hfacs_category(in_memory_uow)
    attach = await async_client_admin.post(
        f"/api/v1/editorial/events/{event.id}/hfacs",
        json={"category_id": str(cat.id), "confidence": 0.5},
    )
    attribution_id = attach.json()["id"]
    resp = await async_client_admin.delete(
        f"/api/v1/editorial/events/{event.id}/hfacs/{attribution_id}"
    )
    assert resp.status_code == 204
    # Idempotent: a second delete also returns 204.
    resp2 = await async_client_admin.delete(
        f"/api/v1/editorial/events/{event.id}/hfacs/{attribution_id}"
    )
    assert resp2.status_code == 204


@pytest.mark.asyncio
async def test_shelo_self_loop_returns_422(async_client_admin: AsyncClient, in_memory_uow):
    event, _ = _seed_event_and_page(in_memory_uow, slug="s")
    f = await async_client_admin.post(
        f"/api/v1/editorial/events/{event.id}/shelo/factors",
        json={"factor_class": "SOFTWARE", "label": "x"},
    )
    factor_id = f.json()["id"]
    resp = await async_client_admin.post(
        f"/api/v1/editorial/events/{event.id}/shelo/interactions",
        json={
            "source_factor_id": factor_id,
            "target_factor_id": factor_id,
            "interaction_kind": "AGGRAVATED",
        },
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "SHELO_FACTOR_INTERACTION_SAME_NODE"


@pytest.mark.asyncio
async def test_shelo_invalid_class_returns_422(async_client_admin: AsyncClient, in_memory_uow):
    event, _ = _seed_event_and_page(in_memory_uow, slug="bad")
    resp = await async_client_admin.post(
        f"/api/v1/editorial/events/{event.id}/shelo/factors",
        json={"factor_class": "NOT_REAL", "label": "x"},
    )
    assert resp.status_code == 422


# ── Auth gates ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_taxonomy_requires_auth(client: AsyncClient):
    resp = await client.get("/api/v1/public/hfacs/taxonomy")
    assert resp.status_code in (401, 403)


@pytest.mark.asyncio
async def test_editorial_hfacs_requires_reviewer_plus(
    in_memory_uow, async_client_analyst: AsyncClient
):
    """An analyst (read-only role) cannot attach an attribution."""
    event, _ = _seed_event_and_page(in_memory_uow, slug="a")
    cat = _seed_hfacs_category(in_memory_uow)
    resp = await async_client_analyst.post(
        f"/api/v1/editorial/events/{event.id}/hfacs",
        json={"category_id": str(cat.id), "confidence": 0.5},
    )
    assert resp.status_code == 403
