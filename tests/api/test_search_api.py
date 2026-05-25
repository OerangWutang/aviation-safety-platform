"""API tests for the public search endpoint and admin reindex."""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from atlas.domain.entities import AccidentEvent, ProjectedAccidentRecord
from atlas.domain.publication.entities import PublicationStatus, PublicEventPage
from atlas.domain.search.entities import SearchIndexEntry


def _seed_indexed_page(
    uow,
    *,
    slug: str,
    title: str,
    operator: str | None = None,
    aircraft_type: str | None = None,
    short_summary: str | None = None,
    confidence_band: str = "high",
):
    """Seed a published page + matching search-index entry.

    Bypasses the editorial use cases on purpose: these tests are
    about the read path, and seeding via the API would couple search
    API tests to publish-side concerns covered elsewhere.
    """
    from datetime import UTC, datetime

    event = AccidentEvent()
    uow.store.events[event.id] = event
    uow.store.projections[event.id] = ProjectedAccidentRecord(
        event_id=event.id, fields={}, completeness_score=0.9
    )
    now = datetime(2024, 6, 1, tzinfo=UTC)
    page = PublicEventPage(
        event_id=event.id,
        slug=slug,
        title=title,
        short_summary=short_summary,
        status=PublicationStatus.PUBLISHED,
        first_published_at=now,
        last_published_at=now,
    )
    uow.store.publication.pages[page.id] = page
    uow.store.search.entries[page.id] = SearchIndexEntry(
        page_id=page.id,
        slug=slug,
        title=title,
        short_summary=short_summary,
        operator=operator,
        aircraft_type=aircraft_type,
        confidence_band=confidence_band,
        last_published_at=now,
    )
    return page.id


@pytest.mark.asyncio
async def test_search_endpoint_returns_matching_pages(
    async_client_analyst: AsyncClient, in_memory_uow
):
    _seed_indexed_page(in_memory_uow, slug="boeing-737-event", title="Boeing 737 emergency")
    _seed_indexed_page(in_memory_uow, slug="something-else", title="Other")

    resp = await async_client_analyst.get("/api/v1/search/events?q=boeing")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    slugs = [item["slug"] for item in body["items"]]
    assert slugs == ["boeing-737-event"]


@pytest.mark.asyncio
async def test_search_response_omits_rank_by_default(
    async_client_analyst: AsyncClient, in_memory_uow
):
    """``rank`` is a debug field; default responses must not leak it.

    Hidden behind ``debug_rank=true`` so production payloads stay
    stable across ranking-algorithm tweaks.
    """
    _seed_indexed_page(in_memory_uow, slug="x", title="Hydraulic event")
    resp = await async_client_analyst.get("/api/v1/search/events?q=hydraulic")
    assert resp.status_code == 200
    assert resp.json()["items"][0]["rank"] is None

    debug = await async_client_analyst.get("/api/v1/search/events?q=hydraulic&debug_rank=true")
    assert debug.status_code == 200
    assert debug.json()["items"][0]["rank"] is not None


@pytest.mark.asyncio
async def test_search_filter_by_operator(async_client_analyst: AsyncClient, in_memory_uow):
    _seed_indexed_page(in_memory_uow, slug="abc", title="A", operator="ABC Airlines")
    _seed_indexed_page(in_memory_uow, slug="xyz", title="B", operator="XYZ Airlines")
    resp = await async_client_analyst.get("/api/v1/search/events?operator=ABC%20Airlines")
    assert resp.status_code == 200
    slugs = [item["slug"] for item in resp.json()["items"]]
    assert slugs == ["abc"]


@pytest.mark.asyncio
async def test_search_rejects_malformed_query(
    async_client_analyst: AsyncClient,
):
    # Inverted fatalities range — caught by SearchQuery validation
    # and surfaced as 422 via the generic DomainValidationError
    # handler.
    resp = await async_client_analyst.get(
        "/api/v1/search/events?fatalities_min=10&fatalities_max=5"
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "SEARCH_QUERY_MALFORMED"


@pytest.mark.asyncio
async def test_search_rejects_oversized_limit(
    async_client_analyst: AsyncClient,
):
    resp = await async_client_analyst.get("/api/v1/search/events?limit=9999")
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_search_requires_auth(client: AsyncClient):
    resp = await client.get("/api/v1/search/events")
    assert resp.status_code in (401, 403)


@pytest.mark.asyncio
async def test_admin_can_reindex(async_client_admin: AsyncClient, in_memory_uow):
    _seed_indexed_page(in_memory_uow, slug="a", title="A")
    _seed_indexed_page(in_memory_uow, slug="b", title="B")
    # Wipe to verify reindex actually populates.
    in_memory_uow.store.search.entries.clear()

    resp = await async_client_admin.post("/api/v1/admin/search/reindex")
    assert resp.status_code == 200, resp.text
    assert resp.json()["pages_reindexed"] == 2


@pytest.mark.asyncio
async def test_analyst_cannot_reindex(
    async_client_analyst: AsyncClient,
):
    resp = await async_client_analyst.post("/api/v1/admin/search/reindex")
    assert resp.status_code in (401, 403)
