"""Chronos v0.1 API tests."""

from __future__ import annotations

from uuid import uuid4

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_reviewer_can_extract(
    async_client_reviewer: AsyncClient, seeded_chronos_event_with_projection
):
    event_id = seeded_chronos_event_with_projection
    resp = await async_client_reviewer.post(f"/api/v1/chronos/events/{event_id}/extract")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["event_id"] == str(event_id)
    assert data["timeline_events_created_count"] >= 1


@pytest.mark.asyncio
async def test_reviewer_can_get_timeline(
    async_client_reviewer: AsyncClient, seeded_chronos_event_with_projection
):
    event_id = seeded_chronos_event_with_projection
    await async_client_reviewer.post(f"/api/v1/chronos/events/{event_id}/extract")
    resp = await async_client_reviewer.get(f"/api/v1/chronos/events/{event_id}/timeline")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["event_id"] == str(event_id)
    assert isinstance(data["timeline_events"], list)
    assert isinstance(data["event_links"], list)


@pytest.mark.asyncio
async def test_analyst_cannot_extract(
    async_client_analyst: AsyncClient, seeded_chronos_event_with_projection
):
    event_id = seeded_chronos_event_with_projection
    resp = await async_client_analyst.post(f"/api/v1/chronos/events/{event_id}/extract")
    assert resp.status_code in (401, 403)


@pytest.mark.asyncio
async def test_analyst_can_get_timeline_after_reviewer_extracts(
    async_client_reviewer: AsyncClient,
    async_client_analyst: AsyncClient,
    seeded_chronos_event_with_projection,
):
    event_id = seeded_chronos_event_with_projection
    await async_client_reviewer.post(f"/api/v1/chronos/events/{event_id}/extract")
    resp = await async_client_analyst.get(f"/api/v1/chronos/events/{event_id}/timeline")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert len(data["timeline_events"]) >= 1


@pytest.mark.asyncio
async def test_unknown_event_extraction_returns_empty_summary(async_client_reviewer: AsyncClient):
    event_id = uuid4()
    resp = await async_client_reviewer.post(f"/api/v1/chronos/events/{event_id}/extract")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["timeline_events_created_count"] == 0
    assert data["event_links_created_count"] == 0


@pytest.mark.asyncio
async def test_pending_reviews_endpoint_returns_list(async_client_reviewer: AsyncClient):
    resp = await async_client_reviewer.get("/api/v1/chronos/reviews/pending")
    assert resp.status_code == 200, resp.text
    assert isinstance(resp.json(), list)
