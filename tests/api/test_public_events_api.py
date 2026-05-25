"""API tests for the public encyclopedia router (Phase 1).

These exercise the actual FastAPI app with the in-memory UoW, so the
router, schema serialization, exception handlers (especially 404 for
DRAFT and 410 for RETRACTED), and the path-level slug regex all run
in production code paths.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient

from atlas.domain.entities import (
    AccidentEvent,
    Claim,
    ProjectedAccidentRecord,
    Source,
)
from atlas.domain.enums import ClaimType, SourceKind
from atlas.domain.publication.entities import PublicationStatus, PublicEventPage
from tests.domain._fake_uow import InMemoryUnitOfWork

# ── Fixtures specific to public-event tests ──────────────────────────────────


def _seed_published_event(
    uow: InMemoryUnitOfWork,
    *,
    slug: str = "test-event",
    title: str = "Test Event",
    short_summary: str | None = "A short summary.",
    fields: dict | None = None,
    published_at: datetime | None = None,
) -> tuple[UUID, PublicEventPage]:
    """Seed an accident + projection + published page; return ids."""
    event_id = uuid4()
    uow.store.events[event_id] = AccidentEvent(id=event_id)
    uow.store.projections[event_id] = ProjectedAccidentRecord(
        event_id=event_id,
        projection_version=1,
        fields=fields
        or {
            "event_date": "2024-06-01",
            "location": "Test Location",
            "operator": "Test Op",
            "aircraft_type": "T-737",
            "fatalities_total": 0,
        },
        completeness_score=0.9,
    )
    now = published_at or datetime(2024, 7, 1, tzinfo=UTC)
    page = PublicEventPage(
        event_id=event_id,
        slug=slug,
        title=title,
        short_summary=short_summary,
        status=PublicationStatus.PUBLISHED,
        first_published_at=now,
        last_published_at=now,
    )
    uow.store.publication.pages[page.id] = page
    return event_id, page


# ── List endpoint ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_endpoint_returns_only_published(
    async_client_analyst: AsyncClient, in_memory_uow
):
    _seed_published_event(in_memory_uow, slug="published-one")
    # DRAFT
    draft_event = uuid4()
    in_memory_uow.store.events[draft_event] = AccidentEvent(id=draft_event)
    in_memory_uow.store.projections[draft_event] = ProjectedAccidentRecord(
        event_id=draft_event, fields={}, completeness_score=0.0
    )
    draft_page = PublicEventPage(
        event_id=draft_event,
        slug="draft-one",
        title="Draft",
        status=PublicationStatus.DRAFT,
    )
    in_memory_uow.store.publication.pages[draft_page.id] = draft_page

    resp = await async_client_analyst.get("/api/v1/public/events")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    slugs = [item["slug"] for item in body["items"]]
    assert slugs == ["published-one"]
    assert body["next_cursor"] is None


@pytest.mark.asyncio
async def test_list_response_carries_projection_fields_not_internal_ids(
    async_client_analyst: AsyncClient, in_memory_uow
):
    _seed_published_event(
        in_memory_uow,
        slug="boeing-737-anchorage",
        fields={
            "event_date": "2024-06-01",
            "location": "Anchorage, AK",
            "operator": "ABC Airlines",
            "aircraft_type": "Boeing 737-800",
            "fatalities_total": 2,
        },
    )
    resp = await async_client_analyst.get("/api/v1/public/events")
    assert resp.status_code == 200
    item = resp.json()["items"][0]

    # Whitelisted fields are present.
    assert item["operator"] == "ABC Airlines"
    assert item["aircraft_type"] == "Boeing 737-800"
    assert item["confidence"] == "high"
    # Internal identifiers must NOT appear in the public summary
    # (the schema uses extra='forbid' so any drift would surface in
    # mypy/test; this is a runtime defence in depth).
    forbidden_keys = {"id", "event_id", "version", "field_mapping_json"}
    assert not (forbidden_keys & set(item.keys())), item


@pytest.mark.asyncio
async def test_list_keyset_pagination(async_client_analyst: AsyncClient, in_memory_uow):
    base = datetime(2024, 1, 1, tzinfo=UTC)
    for i in range(4):
        _seed_published_event(
            in_memory_uow,
            slug=f"e-{i}",
            published_at=base + timedelta(days=i),
        )

    first = await async_client_analyst.get("/api/v1/public/events?limit=2")
    assert first.status_code == 200
    body = first.json()
    assert len(body["items"]) == 2
    assert body["next_cursor"] is not None

    second = await async_client_analyst.get(
        f"/api/v1/public/events?limit=2&cursor={body['next_cursor']}"
    )
    assert second.status_code == 200
    body2 = second.json()
    assert len(body2["items"]) == 2

    all_slugs = [i["slug"] for i in body["items"]] + [i["slug"] for i in body2["items"]]
    assert sorted(all_slugs) == ["e-0", "e-1", "e-2", "e-3"]


@pytest.mark.asyncio
async def test_list_rejects_oversized_limit(
    async_client_analyst: AsyncClient,
):
    resp = await async_client_analyst.get("/api/v1/public/events?limit=10000")
    assert resp.status_code == 422


# ── Detail endpoint ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_detail_returns_editorial_block_separate_from_projection(
    async_client_analyst: AsyncClient, in_memory_uow
):
    _seed_published_event(
        in_memory_uow,
        slug="editorial-block",
        title="Editor's title",
        short_summary="Editor summary",
    )
    resp = await async_client_analyst.get("/api/v1/public/events/editorial-block")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["editorial"]["title"] == "Editor's title"
    assert body["editorial"]["short_summary"] == "Editor summary"
    # Projection lives under "fields", not under "editorial".
    assert "operator" in body["fields"]
    assert "operator" not in body["editorial"]


@pytest.mark.asyncio
async def test_detail_for_draft_returns_404(async_client_analyst: AsyncClient, in_memory_uow):
    event_id = uuid4()
    in_memory_uow.store.events[event_id] = AccidentEvent(id=event_id)
    in_memory_uow.store.projections[event_id] = ProjectedAccidentRecord(
        event_id=event_id, fields={}, completeness_score=0.0
    )
    draft = PublicEventPage(
        event_id=event_id, slug="hidden-draft", title="X", status=PublicationStatus.DRAFT
    )
    in_memory_uow.store.publication.pages[draft.id] = draft

    resp = await async_client_analyst.get("/api/v1/public/events/hidden-draft")
    # 404 not 410/403 so DRAFT existence is not observable.
    assert resp.status_code == 404
    body = resp.json()
    assert body["error"]["code"] == "PUBLIC_EVENT_PAGE_NOT_PUBLISHED"


@pytest.mark.asyncio
async def test_detail_for_retracted_returns_410_with_note(
    async_client_analyst: AsyncClient, in_memory_uow
):
    _event_id, page = _seed_published_event(in_memory_uow, slug="retracted-one")
    page.retract("Editorial correction required.")

    resp = await async_client_analyst.get("/api/v1/public/events/retracted-one")
    assert resp.status_code == 410
    body = resp.json()
    assert body["error"]["code"] == "PUBLIC_EVENT_PAGE_RETRACTED"
    assert body["error"]["details"]["slug"] == "retracted-one"
    assert body["error"]["details"]["retraction_note"] == "Editorial correction required."


@pytest.mark.asyncio
async def test_detail_for_missing_returns_404(
    async_client_analyst: AsyncClient,
):
    resp = await async_client_analyst.get("/api/v1/public/events/no-such-slug")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_detail_for_malformed_slug_returns_422_not_500(
    async_client_analyst: AsyncClient,
):
    """Path regex must reject malformed slugs before any DB query.

    422 is the canonical Pydantic-validation status; anything else
    here would mean the slug regex regressed and bad input would
    reach repositories.
    """
    # Uppercase, spaces, and trailing hyphens are all canonical-form
    # violations.  The path validator should reject every one.
    for bad in ("UPPER", "double--hyphen", "trailing-", "with space"):
        resp = await async_client_analyst.get(f"/api/v1/public/events/{bad}")
        # URL-encoded space becomes %20 in the request; FastAPI parses
        # it and the regex rejects.
        assert resp.status_code == 422, (bad, resp.status_code, resp.text)


# ── Evidence endpoint ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_evidence_endpoint_returns_claims_and_sources(
    async_client_analyst: AsyncClient, in_memory_uow
):
    event_id, _page = _seed_published_event(in_memory_uow, slug="ev")
    source = Source(name="NTSB", kind=SourceKind.EXTERNAL, reliability_tier=1)
    in_memory_uow.store.sources[source.id] = source
    claim = Claim(
        event_id=event_id,
        source_id=source.id,
        field_name="location",
        field_value="Test Location",
        claim_type=ClaimType.RAW,
    )
    in_memory_uow.store.claims[claim.id] = claim

    resp = await async_client_analyst.get("/api/v1/public/events/ev/evidence")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["slug"] == "ev"
    assert body["claim_count"] == 1
    assert body["truncated"] is False
    assert body["sources"][0]["name"] == "NTSB"
    # is_winning aligns with the projection's location value.
    assert body["claims"][0]["is_winning"] is True


@pytest.mark.asyncio
async def test_evidence_source_dto_does_not_expose_field_mapping(
    async_client_analyst: AsyncClient, in_memory_uow
):
    event_id, _page = _seed_published_event(in_memory_uow, slug="no-mapping")
    source = Source(
        name="NTSB",
        kind=SourceKind.EXTERNAL,
        reliability_tier=1,
        field_mapping_json={"date": "event_date"},
    )
    in_memory_uow.store.sources[source.id] = source
    claim = Claim(
        event_id=event_id,
        source_id=source.id,
        field_name="event_date",
        field_value="2024-06-01",
    )
    in_memory_uow.store.claims[claim.id] = claim

    resp = await async_client_analyst.get("/api/v1/public/events/no-mapping/evidence")
    assert resp.status_code == 200
    body = resp.json()
    assert body["sources"]
    # Hard whitelist: only these three keys must appear.
    assert set(body["sources"][0].keys()) == {"name", "kind", "reliability_tier"}


# ── Timeline endpoint ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_timeline_endpoint_returns_empty_list_when_no_timeline(
    async_client_analyst: AsyncClient, in_memory_uow
):
    _seed_published_event(in_memory_uow, slug="no-timeline")
    resp = await async_client_analyst.get("/api/v1/public/events/no-timeline/timeline")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["events"] == []
    assert body["slug"] == "no-timeline"


# ── Related endpoint ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_related_endpoint_returns_empty_list_when_no_relationships(
    async_client_analyst: AsyncClient, in_memory_uow
):
    _seed_published_event(in_memory_uow, slug="no-rel")
    resp = await async_client_analyst.get("/api/v1/public/events/no-rel/related")
    assert resp.status_code == 200
    body = resp.json()
    assert body["items"] == []


# ── Auth gates ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_requires_auth(client):
    """Without an X-API-Key, every public endpoint must return 401/403.

    Phase 1 intentionally keeps the existing reader gate.  Truly
    anonymous public access is deferred — flagged in the plan.
    """
    resp = await client.get("/api/v1/public/events")
    assert resp.status_code in (401, 403)


@pytest.mark.asyncio
async def test_detail_requires_auth(client):
    resp = await client.get("/api/v1/public/events/some-slug")
    assert resp.status_code in (401, 403)
