"""API tests for the Phase 3 map router.

Exercise the full HTTP stack: routing, query-param validation,
schema ``extra='forbid'`` whitelist, role gates.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from httpx import AsyncClient

from atlas.domain.entities import AccidentEvent, ProjectedAccidentRecord
from atlas.domain.maps.entities import MapIndexEntry
from atlas.domain.publication.entities import PublicationStatus, PublicEventPage


def _seed_indexed_point(
    uow,
    *,
    slug: str,
    title: str,
    lat: float,
    lng: float,
    operator: str | None = None,
    confidence_band: str = "high",
):
    """Seed a published page + matching map-index row.

    Bypasses the editorial use cases on purpose: these tests are
    about the read path, and seeding via the API would couple map
    tests to publish-side concerns already covered elsewhere.
    """
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
        status=PublicationStatus.PUBLISHED,
        first_published_at=now,
        last_published_at=now,
    )
    uow.store.publication.pages[page.id] = page
    uow.store.maps.entries[page.id] = MapIndexEntry(
        page_id=page.id,
        slug=slug,
        title=title,
        latitude=lat,
        longitude=lng,
        operator=operator,
        confidence_band=confidence_band,
        last_published_at=now,
    )
    return page.id


@pytest.mark.asyncio
async def test_bbox_search_returns_in_range_points(
    async_client_analyst: AsyncClient, in_memory_uow
):
    _seed_indexed_point(in_memory_uow, slug="sf", title="SF", lat=37.7, lng=-122.4)
    _seed_indexed_point(in_memory_uow, slug="ny", title="NY", lat=40.7, lng=-74.0)
    # West-coast bbox.
    resp = await async_client_analyst.get(
        "/api/v1/maps/events",
        params={
            "south": 30.0,
            "west": -130.0,
            "north": 45.0,
            "east": -115.0,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    slugs = [item["slug"] for item in body["items"]]
    assert slugs == ["sf"]
    assert body["truncated"] is False


@pytest.mark.asyncio
async def test_bbox_search_response_shape(async_client_analyst: AsyncClient, in_memory_uow):
    """Response shape pinned: items carry lat/lng, slug, page_id; the
    truncated/limit fields are present."""
    _seed_indexed_point(
        in_memory_uow,
        slug="x",
        title="X",
        lat=10.0,
        lng=10.0,
        operator="Op",
    )
    resp = await async_client_analyst.get(
        "/api/v1/maps/events",
        params={"south": -1, "west": -1, "north": 20, "east": 20},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert set(body.keys()) == {"items", "truncated", "limit"}
    item = body["items"][0]
    expected_keys = {
        "page_id",
        "slug",
        "title",
        "latitude",
        "longitude",
        "operator",
        "aircraft_type",
        "country",
        "event_date",
        "fatalities_total",
        "confidence_band",
        "last_published_at",
    }
    assert set(item.keys()) == expected_keys


@pytest.mark.asyncio
async def test_bbox_search_filter_by_operator(async_client_analyst: AsyncClient, in_memory_uow):
    _seed_indexed_point(
        in_memory_uow,
        slug="a",
        title="A",
        lat=1.0,
        lng=1.0,
        operator="ABC Airlines",
    )
    _seed_indexed_point(
        in_memory_uow,
        slug="b",
        title="B",
        lat=2.0,
        lng=2.0,
        operator="XYZ Airlines",
    )
    resp = await async_client_analyst.get(
        "/api/v1/maps/events",
        params={
            "south": -1,
            "west": -1,
            "north": 10,
            "east": 10,
            "operator": "ABC Airlines",
        },
    )
    assert resp.status_code == 200
    slugs = [item["slug"] for item in resp.json()["items"]]
    assert slugs == ["a"]


@pytest.mark.asyncio
async def test_inverted_lat_range_returns_422(
    async_client_analyst: AsyncClient,
):
    """south > north is malformed.  Hits MapBoundingBox validation
    and surfaces as 422 via the generic DomainValidationError
    handler."""
    resp = await async_client_analyst.get(
        "/api/v1/maps/events",
        params={"south": 50, "west": -10, "north": 10, "east": 10},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_out_of_range_lat_returns_422(
    async_client_analyst: AsyncClient,
):
    """Out-of-range latitude is caught by FastAPI's Query bound."""
    resp = await async_client_analyst.get(
        "/api/v1/maps/events",
        params={"south": -91, "west": -10, "north": 10, "east": 10},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_cluster_endpoint_returns_cells_with_counts(
    async_client_analyst: AsyncClient, in_memory_uow
):
    _seed_indexed_point(in_memory_uow, slug="sf1", title="SF1", lat=37.7, lng=-122.4)
    _seed_indexed_point(in_memory_uow, slug="sf2", title="SF2", lat=37.8, lng=-122.5)
    _seed_indexed_point(in_memory_uow, slug="ny", title="NY", lat=40.7, lng=-74.0)
    resp = await async_client_analyst.get(
        "/api/v1/maps/events/cluster",
        params={
            "south": 24,
            "west": -125,
            "north": 49,
            "east": -66,
            "cluster_precision": 8,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["cluster_precision"] == 8
    counts = sorted(c["count"] for c in body["cells"])
    # SF cluster has count == 2; NY cluster has count == 1.
    assert counts == [1, 2]


@pytest.mark.asyncio
async def test_cluster_response_shape(async_client_analyst: AsyncClient, in_memory_uow):
    _seed_indexed_point(in_memory_uow, slug="x", title="X", lat=10.0, lng=10.0)
    resp = await async_client_analyst.get(
        "/api/v1/maps/events/cluster",
        params={
            "south": -1,
            "west": -1,
            "north": 20,
            "east": 20,
            "cluster_precision": 4,
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert set(body.keys()) == {"cells", "truncated", "cluster_precision"}
    cell = body["cells"][0]
    expected_keys = {
        "cell_west",
        "cell_south",
        "cell_east",
        "cell_north",
        "centroid_latitude",
        "centroid_longitude",
        "count",
    }
    assert set(cell.keys()) == expected_keys


@pytest.mark.asyncio
async def test_map_endpoints_require_auth(client: AsyncClient):
    for path in ("/api/v1/maps/events", "/api/v1/maps/events/cluster"):
        resp = await client.get(
            path,
            params={"south": -1, "west": -1, "north": 1, "east": 1},
        )
        assert resp.status_code in (401, 403), path


@pytest.mark.asyncio
async def test_empty_bbox_returns_empty_list(async_client_analyst: AsyncClient, in_memory_uow):
    """A bbox with no matching points returns an empty list, not an
    error."""
    _seed_indexed_point(in_memory_uow, slug="x", title="X", lat=10.0, lng=10.0)
    resp = await async_client_analyst.get(
        "/api/v1/maps/events",
        params={
            "south": 80,
            "west": 80,
            "north": 85,
            "east": 85,
        },
    )
    assert resp.status_code == 200
    assert resp.json()["items"] == []
    assert resp.json()["truncated"] is False
