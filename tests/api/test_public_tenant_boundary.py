"""Behavioral boundary tests: public endpoints must never return tenant data.

These tests exercise the full FastAPI stack with the in-memory UoW.  They seed
both public and tenant-private data and assert that every public-read endpoint
(``/public/events``, ``/search/events``, ``/maps/events``) returns only the
public projection, not any tenant overlay content.

The structural isolation is enforced by two mechanisms:
1. ``get_public_uow`` routes public endpoints to the public DB engine.
2. The use cases only read from public repos (``public_event_pages``,
   ``search``, ``maps``) — never from ``tenant_event_overlays`` etc.

These tests provide *behavioural* evidence that both mechanisms hold end-to-end,
catching the class of regression where someone adds a tenant-aware path to a
public use case or mistakenly seeds tenant data into a public index.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient

from atlas.domain.entities import AccidentEvent, ProjectedAccidentRecord
from atlas.domain.maps.entities import MapIndexEntry
from atlas.domain.publication.entities import PublicationStatus, PublicEventPage
from atlas.domain.search.entities import SearchIndexEntry
from atlas.domain.tenancy.entities import TenantEventOverlay
from tests.domain._fake_uow import InMemoryUnitOfWork

# ── Shared helpers ────────────────────────────────────────────────────────────

_NOW = datetime(2024, 7, 1, tzinfo=UTC)
_PRIVATE_MARKER = "TENANT_PRIVATE_SHOULD_NOT_APPEAR"


def _seed_published_event(
    uow: InMemoryUnitOfWork,
    *,
    slug: str = "public-event",
    operator: str = "PublicAirlines",
) -> tuple[UUID, UUID]:
    """Seed a minimal published public event page.  Return (event_id, page_id)."""
    event_id = uuid4()
    uow.store.events[event_id] = AccidentEvent(id=event_id)
    uow.store.projections[event_id] = ProjectedAccidentRecord(
        event_id=event_id,
        projection_version=1,
        fields={
            "event_date": "2024-06-01",
            "location": "Test Location",
            "operator": operator,
            "aircraft_type": "B738",
            "fatalities_total": 0,
        },
        completeness_score=0.8,
    )
    page = PublicEventPage(
        event_id=event_id,
        slug=slug,
        title="Test Event",
        short_summary="A public summary.",
        status=PublicationStatus.PUBLISHED,
        first_published_at=_NOW,
        last_published_at=_NOW,
    )
    uow.store.publication.pages[page.id] = page
    return event_id, page.id


def _seed_tenant_overlay(
    uow: InMemoryUnitOfWork,
    *,
    event_id: UUID,
    tenant_id: UUID,
) -> None:
    """Seed a tenant overlay with a distinctive private marker."""
    overlay = TenantEventOverlay(
        tenant_id=tenant_id,
        event_id=event_id,
        notes_markdown=_PRIVATE_MARKER,
        overlay_fields={"private_field": _PRIVATE_MARKER},
    )
    uow.store.tenancy.overlays[overlay.id] = overlay


def _seed_search_entry(
    uow: InMemoryUnitOfWork,
    *,
    page_id: UUID,
    slug: str,
    operator: str = "PublicAirlines",
) -> None:
    entry = SearchIndexEntry(
        page_id=page_id,
        slug=slug,
        title="Test Event",
        operator=operator,
        aircraft_type="B738",
        country="US",
        event_date=date(2024, 6, 1),
        fatalities_total=0,
        confidence_band="high",
        last_published_at=_NOW,
    )
    uow.store.search.entries[page_id] = entry


def _seed_map_entry(
    uow: InMemoryUnitOfWork,
    *,
    page_id: UUID,
    slug: str,
    operator: str = "PublicAirlines",
) -> None:
    entry = MapIndexEntry(
        page_id=page_id,
        slug=slug,
        title="Test Event",
        latitude=40.0,
        longitude=-75.0,
        operator=operator,
        aircraft_type="B738",
        country="US",
        event_date=date(2024, 6, 1),
        fatalities_total=0,
        confidence_band="high",
        last_published_at=_NOW,
    )
    uow.store.maps.entries[page_id] = entry


# ── Tests ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_public_list_returns_empty_when_no_published_pages(
    async_client_analyst: AsyncClient,
    in_memory_uow: InMemoryUnitOfWork,
) -> None:
    """With only a tenant overlay seeded (no published page), the list is empty."""
    tenant_id = uuid4()
    event_id = uuid4()
    uow = in_memory_uow
    uow.store.events[event_id] = AccidentEvent(id=event_id)
    _seed_tenant_overlay(uow, event_id=event_id, tenant_id=tenant_id)

    r = await async_client_analyst.get("/api/v1/public/events")
    assert r.status_code == 200
    data = r.json()
    assert data["items"] == [], "Public list must be empty when only tenant overlay data exists"


@pytest.mark.asyncio
async def test_public_list_returns_published_page_not_overlay_content(
    async_client_analyst: AsyncClient,
    in_memory_uow: InMemoryUnitOfWork,
) -> None:
    """Published page appears; its content does not include tenant overlay fields."""
    uow = in_memory_uow
    tenant_id = uuid4()
    event_id, _ = _seed_published_event(uow, slug="event-with-overlay", operator="PublicAir")
    _seed_tenant_overlay(uow, event_id=event_id, tenant_id=tenant_id)

    r = await async_client_analyst.get("/api/v1/public/events")
    assert r.status_code == 200
    body = r.json()
    assert len(body["items"]) == 1
    item_str = str(body["items"][0])
    assert _PRIVATE_MARKER not in item_str, (
        "Tenant overlay private marker must never appear in the public list response"
    )


@pytest.mark.asyncio
async def test_public_detail_404_when_no_published_page(
    async_client_analyst: AsyncClient,
    in_memory_uow: InMemoryUnitOfWork,
) -> None:
    """Slug lookup returns 404 when the event exists only as a tenant overlay."""
    uow = in_memory_uow
    tenant_id = uuid4()
    event_id = uuid4()
    uow.store.events[event_id] = AccidentEvent(id=event_id)
    _seed_tenant_overlay(uow, event_id=event_id, tenant_id=tenant_id)

    r = await async_client_analyst.get("/api/v1/public/events/tenant-only-event")
    assert r.status_code == 404, (
        "An event that exists only as a tenant overlay must return 404 on the public endpoint"
    )


@pytest.mark.asyncio
async def test_public_detail_does_not_include_tenant_overlay_fields(
    async_client_analyst: AsyncClient,
    in_memory_uow: InMemoryUnitOfWork,
) -> None:
    """Detail response reflects public projection; tenant overlay fields are absent."""
    uow = in_memory_uow
    tenant_id = uuid4()
    event_id, _ = _seed_published_event(uow, slug="detail-event", operator="PublicOp")
    _seed_tenant_overlay(uow, event_id=event_id, tenant_id=tenant_id)

    r = await async_client_analyst.get("/api/v1/public/events/detail-event")
    assert r.status_code == 200
    body_str = str(r.json())
    assert _PRIVATE_MARKER not in body_str, (
        "Tenant overlay notes_markdown / overlay_fields must never appear in the public detail response"
    )
    assert "PublicOp" in body_str or r.json()["fields"].get("operator") == "PublicOp", (
        "Public projection operator should be in the response"
    )


@pytest.mark.asyncio
async def test_search_returns_empty_when_only_tenant_data_seeded(
    async_client_analyst: AsyncClient,
    in_memory_uow: InMemoryUnitOfWork,
) -> None:
    """Search index has no entries; a tenant claim for the same event is irrelevant."""
    uow = in_memory_uow
    tenant_id = uuid4()
    event_id = uuid4()
    uow.store.events[event_id] = AccidentEvent(id=event_id)
    _seed_tenant_overlay(uow, event_id=event_id, tenant_id=tenant_id)
    # search.entries is empty — no public index entry

    r = await async_client_analyst.get("/api/v1/search/events")
    assert r.status_code == 200
    assert r.json()["items"] == [], (
        "Search must return no hits when only tenant data exists — the public search index is empty"
    )


@pytest.mark.asyncio
async def test_search_results_do_not_contain_tenant_overlay_content(
    async_client_analyst: AsyncClient,
    in_memory_uow: InMemoryUnitOfWork,
) -> None:
    """Search index entry appears; its fields do not include tenant overlay values."""
    uow = in_memory_uow
    tenant_id = uuid4()
    event_id, page_id = _seed_published_event(uow, slug="search-event")
    _seed_search_entry(uow, page_id=page_id, slug="search-event")
    _seed_tenant_overlay(uow, event_id=event_id, tenant_id=tenant_id)

    r = await async_client_analyst.get("/api/v1/search/events")
    assert r.status_code == 200
    body = r.json()
    assert len(body["items"]) == 1
    assert _PRIVATE_MARKER not in str(body), (
        "Tenant overlay private marker must never appear in the search response"
    )


@pytest.mark.asyncio
async def test_maps_returns_empty_when_only_tenant_data_seeded(
    async_client_analyst: AsyncClient,
    in_memory_uow: InMemoryUnitOfWork,
) -> None:
    """Map index has no entries; a tenant overlay for the same event is irrelevant."""
    uow = in_memory_uow
    tenant_id = uuid4()
    event_id = uuid4()
    uow.store.events[event_id] = AccidentEvent(id=event_id)
    _seed_tenant_overlay(uow, event_id=event_id, tenant_id=tenant_id)

    r = await async_client_analyst.get("/api/v1/maps/events?south=30&west=-80&north=50&east=-60")
    assert r.status_code == 200
    assert r.json()["items"] == [], (
        "Map endpoint must return no points when only tenant data exists"
    )


@pytest.mark.asyncio
async def test_maps_results_do_not_contain_tenant_overlay_content(
    async_client_analyst: AsyncClient,
    in_memory_uow: InMemoryUnitOfWork,
) -> None:
    """Map point reflects public index entry; tenant overlay fields are absent."""
    uow = in_memory_uow
    tenant_id = uuid4()
    event_id, page_id = _seed_published_event(uow, slug="map-event")
    _seed_map_entry(uow, page_id=page_id, slug="map-event")
    _seed_tenant_overlay(uow, event_id=event_id, tenant_id=tenant_id)

    r = await async_client_analyst.get("/api/v1/maps/events?south=30&west=-80&north=50&east=-60")
    assert r.status_code == 200
    body = r.json()
    assert len(body["items"]) == 1
    assert _PRIVATE_MARKER not in str(body), (
        "Tenant overlay private marker must never appear in the maps response"
    )


@pytest.mark.asyncio
async def test_draft_page_hidden_from_public_list(
    async_client_analyst: AsyncClient,
    in_memory_uow: InMemoryUnitOfWork,
) -> None:
    """DRAFT pages do not appear in the public list regardless of tenant status."""
    uow = in_memory_uow
    event_id = uuid4()
    uow.store.events[event_id] = AccidentEvent(id=event_id)
    uow.store.projections[event_id] = ProjectedAccidentRecord(
        event_id=event_id,
        fields={"operator": "DraftAir"},
    )
    draft_page = PublicEventPage(
        event_id=event_id,
        slug="draft-only-event",
        title="Draft Event",
        status=PublicationStatus.DRAFT,
    )
    uow.store.publication.pages[draft_page.id] = draft_page

    r = await async_client_analyst.get("/api/v1/public/events")
    assert r.status_code == 200
    items = r.json()["items"]
    slugs = [i["slug"] for i in items]
    assert "draft-only-event" not in slugs, "DRAFT page must not appear in the public list"


@pytest.mark.asyncio
async def test_retracted_page_returns_410(
    async_client_analyst: AsyncClient,
    in_memory_uow: InMemoryUnitOfWork,
) -> None:
    """RETRACTED pages return 410 Gone, not 200 or 404."""
    uow = in_memory_uow
    _event_id, _ = _seed_published_event(uow, slug="retracted-event")
    # Retract the page after seeding
    page = next(p for p in uow.store.publication.pages.values() if p.slug == "retracted-event")
    retracted = page.model_copy(update={"status": PublicationStatus.RETRACTED})
    uow.store.publication.pages[page.id] = retracted

    r = await async_client_analyst.get("/api/v1/public/events/retracted-event")
    assert r.status_code == 410, "A retracted public page must return 410 Gone, not 200 or 404"
