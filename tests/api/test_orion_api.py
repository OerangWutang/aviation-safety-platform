"""Orion v0.1 API endpoint tests."""

from __future__ import annotations

from uuid import uuid4

import pytest


@pytest.mark.asyncio
async def test_extract_returns_summary(async_client_reviewer, seeded_event_with_projection):
    event_id = seeded_event_with_projection
    response = await async_client_reviewer.post(f"/api/v1/orion/events/{event_id}/extract")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["event_id"] == str(event_id)
    assert body["entities_created_count"] > 0
    assert body["relationships_created_count"] > 0


@pytest.mark.asyncio
async def test_get_event_entities_after_extract(
    async_client_reviewer, seeded_event_with_projection
):
    event_id = seeded_event_with_projection
    extract_resp = await async_client_reviewer.post(f"/api/v1/orion/events/{event_id}/extract")
    assert extract_resp.status_code == 200

    response = await async_client_reviewer.get(f"/api/v1/orion/events/{event_id}/entities")
    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body["entities"]) > 0
    assert len(body["relationships"]) > 0


@pytest.mark.asyncio
async def test_entity_search_by_canonical_name(async_client_reviewer, seeded_event_with_projection):
    event_id = seeded_event_with_projection
    await async_client_reviewer.post(f"/api/v1/orion/events/{event_id}/extract")

    response = await async_client_reviewer.get("/api/v1/orion/entities/search?q=klm")
    assert response.status_code == 200, response.text
    body = response.json()
    assert any("klm" in r["canonical_name"].lower() for r in body["results"])


@pytest.mark.asyncio
async def test_entity_search_by_identifier(async_client_reviewer, seeded_event_with_projection):
    event_id = seeded_event_with_projection
    await async_client_reviewer.post(f"/api/v1/orion/events/{event_id}/extract")

    response = await async_client_reviewer.get("/api/v1/orion/entities/search?q=PH-BXA")
    assert response.status_code == 200, response.text
    assert len(response.json()["results"]) >= 1


@pytest.mark.asyncio
async def test_get_entity_relationships(async_client_reviewer, seeded_event_with_projection):
    event_id = seeded_event_with_projection
    extract_resp = await async_client_reviewer.post(f"/api/v1/orion/events/{event_id}/extract")
    assert extract_resp.status_code == 200
    entity_ids = extract_resp.json().get("entity_ids", [])
    assert entity_ids

    response = await async_client_reviewer.get(
        f"/api/v1/orion/entities/{entity_ids[0]}/relationships"
    )
    assert response.status_code == 200, response.text
    assert isinstance(response.json(), list)


@pytest.mark.asyncio
async def test_extract_unknown_event_returns_empty_summary(async_client_reviewer):
    response = await async_client_reviewer.post(f"/api/v1/orion/events/{uuid4()}/extract")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["entities_created_count"] == 0
    assert body["relationships_created_count"] == 0


@pytest.mark.asyncio
async def test_get_nonexistent_entity_returns_404(async_client_reviewer):
    response = await async_client_reviewer.get(f"/api/v1/orion/entities/{uuid4()}")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_analyst_cannot_post_extract(async_client_analyst, seeded_event_with_projection):
    event_id = seeded_event_with_projection
    response = await async_client_analyst.post(f"/api/v1/orion/events/{event_id}/extract")
    assert response.status_code in (401, 403)


@pytest.mark.asyncio
async def test_analyst_can_read_entities(
    async_client_reviewer, async_client_analyst, seeded_event_with_projection
):
    event_id = seeded_event_with_projection
    await async_client_reviewer.post(f"/api/v1/orion/events/{event_id}/extract")

    response = await async_client_analyst.get(f"/api/v1/orion/events/{event_id}/entities")
    assert response.status_code == 200, response.text
