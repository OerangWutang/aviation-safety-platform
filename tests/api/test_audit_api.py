"""API tests for the Phase 11 audit endpoints.

Exercise the full FastAPI stack: routing, role gates, schema
``extra='forbid'`` whitelist, exception handlers, and the slug-vs-
event-id endpoint split.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from httpx import AsyncClient

from atlas.domain.entities import (
    AccidentEvent,
    Claim,
    ProjectedAccidentRecord,
    RawSnapshot,
    Source,
)
from atlas.domain.enums import ClaimType, SourceKind
from atlas.domain.publication.entities import PublicationStatus, PublicEventPage
from tests.domain._fake_uow import InMemoryUnitOfWork

# ── Helpers ──────────────────────────────────────────────────────────────────


def _seed_published_event(
    uow: InMemoryUnitOfWork, *, slug: str = "test-event"
) -> tuple[AccidentEvent, PublicEventPage, ProjectedAccidentRecord]:
    event = AccidentEvent()
    uow.store.events[event.id] = event
    projection = ProjectedAccidentRecord(
        event_id=event.id,
        fields={
            "operator": "ABC Airlines",
            "location": "Anchorage",
            "fatalities_total": 0,
        },
        completeness_score=0.9,
    )
    uow.store.projections[event.id] = projection
    now = datetime(2024, 7, 1, tzinfo=UTC)
    page = PublicEventPage(
        event_id=event.id,
        slug=slug,
        title="Test event",
        short_summary="Brief",
        status=PublicationStatus.PUBLISHED,
        first_published_at=now,
        last_published_at=now,
    )
    uow.store.publication.pages[page.id] = page
    return event, page, projection


def _seed_source(uow: InMemoryUnitOfWork, *, name: str = "NTSB", tier: int = 1) -> Source:
    s = Source(name=name, kind=SourceKind.EXTERNAL, reliability_tier=tier)
    uow.store.sources[s.id] = s
    return s


def _seed_claim(uow, event_id, source, *, field_name, field_value, claim_type=ClaimType.RAW):
    c = Claim(
        event_id=event_id,
        source_id=source.id,
        field_name=field_name,
        field_value=field_value,
        claim_type=claim_type,
    )
    uow.store.claims[c.id] = c
    return c


# ── Page audit ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_page_audit_returns_summary_and_fields(
    async_client_analyst: AsyncClient, in_memory_uow
):
    _seed_published_event(in_memory_uow, slug="page-audit-test")
    resp = await async_client_analyst.get("/api/v1/public/events/page-audit-test/audit")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["slug"] == "page-audit-test"
    assert body["confidence"] == "high"
    # All three projected fields are surfaced.
    assert {row["field_name"] for row in body["fields"]} == {
        "operator",
        "location",
        "fatalities_total",
    }
    # Summary is non-empty plain English.
    assert len(body["summary"]) > 0
    # Confidence meaning is more than just the label.
    assert body["confidence_meaning"] != "high"


@pytest.mark.asyncio
async def test_page_audit_for_draft_returns_404(async_client_analyst: AsyncClient, in_memory_uow):
    """DRAFT pages must not leak through the audit endpoint either.

    This is the same invariant Phase 1 enforces on the public detail
    endpoint — the audit endpoint sits next to it and must agree.
    """
    event = AccidentEvent()
    in_memory_uow.store.events[event.id] = event
    in_memory_uow.store.projections[event.id] = ProjectedAccidentRecord(
        event_id=event.id, fields={"operator": "X"}, completeness_score=0.5
    )
    draft = PublicEventPage(
        event_id=event.id,
        slug="draft-audit",
        title="X",
        status=PublicationStatus.DRAFT,
    )
    in_memory_uow.store.publication.pages[draft.id] = draft

    resp = await async_client_analyst.get("/api/v1/public/events/draft-audit/audit")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_page_audit_requires_auth(client: AsyncClient):
    resp = await client.get("/api/v1/public/events/any-slug/audit")
    assert resp.status_code in (401, 403)


# ── Field explanation ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_field_explanation_happy_path(async_client_analyst: AsyncClient, in_memory_uow):
    event, _page, _proj = _seed_published_event(in_memory_uow, slug="field-x")
    source = _seed_source(in_memory_uow, name="NTSB")
    _seed_claim(
        in_memory_uow,
        event.id,
        source,
        field_name="operator",
        field_value="ABC Airlines",
    )
    resp = await async_client_analyst.get(
        f"/api/v1/audit/events/{event.id}/fields/operator/explanation"
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["has_winner"] is True
    assert body["winner"]["source_name"] == "NTSB"
    # Default response is summary mode — expert key is None.
    assert body["winner"]["expert"] is None


@pytest.mark.asyncio
async def test_field_explanation_expert_mode_returns_expert_block(
    async_client_analyst: AsyncClient, in_memory_uow
):
    event, _page, _proj = _seed_published_event(in_memory_uow, slug="field-expert")
    source = _seed_source(in_memory_uow, name="NTSB", tier=1)
    claim = _seed_claim(
        in_memory_uow,
        event.id,
        source,
        field_name="operator",
        field_value="ABC Airlines",
    )
    resp = await async_client_analyst.get(
        f"/api/v1/audit/events/{event.id}/fields/operator/explanation?detail=expert"
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["winner"]["expert"] is not None
    assert body["winner"]["expert"]["claim_id"] == str(claim.id)
    assert body["winner"]["expert"]["source_reliability_tier"] == 1


@pytest.mark.asyncio
async def test_field_explanation_unknown_field_returns_404(
    async_client_analyst: AsyncClient, in_memory_uow
):
    """Field-locking: probing for a field absent from the projection
    must 404, not return an empty response."""
    event, _page, _proj = _seed_published_event(in_memory_uow, slug="field-unknown")
    resp = await async_client_analyst.get(
        f"/api/v1/audit/events/{event.id}/fields/secret_field/explanation"
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_field_explanation_unknown_event_returns_404(
    async_client_analyst: AsyncClient,
):
    resp = await async_client_analyst.get(
        f"/api/v1/audit/events/{uuid4()}/fields/operator/explanation"
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_field_explanation_requires_auth(client: AsyncClient):
    resp = await client.get(f"/api/v1/audit/events/{uuid4()}/fields/operator/explanation")
    assert resp.status_code in (401, 403)


# ── Claim explanation ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_claim_explanation_happy_path(async_client_analyst: AsyncClient, in_memory_uow):
    event, _page, _proj = _seed_published_event(in_memory_uow, slug="claim-x")
    source = _seed_source(in_memory_uow, name="NTSB")
    claim = _seed_claim(
        in_memory_uow,
        event.id,
        source,
        field_name="operator",
        field_value="ABC Airlines",
    )
    resp = await async_client_analyst.get(f"/api/v1/audit/claims/{claim.id}/explanation")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["claim_id"] == str(claim.id)
    assert body["is_winning"] is True
    assert body["is_active"] is True
    assert body["source_name"] == "NTSB"


@pytest.mark.asyncio
async def test_claim_explanation_unknown_id_returns_404(
    async_client_analyst: AsyncClient,
):
    resp = await async_client_analyst.get(f"/api/v1/audit/claims/{uuid4()}/explanation")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_claim_explanation_requires_auth(client: AsyncClient):
    resp = await client.get(f"/api/v1/audit/claims/{uuid4()}/explanation")
    assert resp.status_code in (401, 403)


# ── Source verification ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_source_verification_returns_hash_and_recipe(
    async_client_analyst: AsyncClient, in_memory_uow
):
    source = _seed_source(in_memory_uow, name="NTSB")
    snap = RawSnapshot(
        source_id=source.id,
        ingestion_run_id=uuid4(),
        payload_hash="x",
        payload_json={"any": "thing"},
        captured_at=datetime(2024, 6, 1, tzinfo=UTC),
        raw_payload_hash=("0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"),
        source_record_id="NTSB-ABC-123",
    )
    in_memory_uow.store.snapshots[snap.id] = snap

    resp = await async_client_analyst.get(f"/api/v1/audit/sources/{snap.id}/verification")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["raw_payload_hash"] == snap.raw_payload_hash
    assert body["source_name"] == "NTSB"
    assert body["recipe_version"]
    assert len(body["recipe_steps"]) >= 3
    # Verification note signals non-redistribution / fetch-yourself.
    note = body["verification_note"].lower()
    assert "redistribut" in note or "fetch" in note


@pytest.mark.asyncio
async def test_source_verification_never_includes_payload(
    async_client_analyst: AsyncClient, in_memory_uow
):
    """The response schema must not carry the raw source payload.

    This is the whitelist-by-construction contract: even if a future
    contributor accidentally added a ``payload`` key to the use-case
    response, ``extra='forbid'`` on the Pydantic schema would reject
    it at the boundary.
    """
    source = _seed_source(in_memory_uow)
    snap = RawSnapshot(
        source_id=source.id,
        ingestion_run_id=uuid4(),
        payload_hash="x",
        payload_json={"sensitive": "payload"},
        captured_at=datetime(2024, 6, 1, tzinfo=UTC),
        raw_payload_hash="abc",
    )
    in_memory_uow.store.snapshots[snap.id] = snap

    resp = await async_client_analyst.get(f"/api/v1/audit/sources/{snap.id}/verification")
    assert resp.status_code == 200
    body = resp.json()
    # No payload-shaped keys may appear anywhere in the response.
    forbidden = {"payload", "payload_json", "raw_payload", "raw_payload_json"}
    assert not (forbidden & set(body.keys())), body


@pytest.mark.asyncio
async def test_source_verification_unknown_snapshot_returns_404(
    async_client_analyst: AsyncClient,
):
    resp = await async_client_analyst.get(f"/api/v1/audit/sources/{uuid4()}/verification")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_source_verification_requires_auth(client: AsyncClient):
    resp = await client.get(f"/api/v1/audit/sources/{uuid4()}/verification")
    assert resp.status_code in (401, 403)
