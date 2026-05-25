from __future__ import annotations

from uuid import uuid4

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_reviewer_can_create_source(async_client_reviewer: AsyncClient):
    resp = await async_client_reviewer.post(
        "/api/v1/hermes/sources",
        json={"name": "Test Agency", "source_type": "OFFICIAL_AGENCY"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["name"] == "Test Agency"


@pytest.mark.asyncio
async def test_analyst_can_list_sources(async_client_analyst: AsyncClient):
    resp = await async_client_analyst.get("/api/v1/hermes/sources")
    assert resp.status_code == 200, resp.text
    assert isinstance(resp.json(), list)


@pytest.mark.asyncio
async def test_analyst_cannot_create_source(async_client_analyst: AsyncClient):
    resp = await async_client_analyst.post(
        "/api/v1/hermes/sources",
        json={"name": "X", "source_type": "NEWS"},
    )
    assert resp.status_code in (401, 403)


@pytest.mark.asyncio
async def test_reviewer_can_create_target(async_client_reviewer: AsyncClient):
    src = await async_client_reviewer.post(
        "/api/v1/hermes/sources",
        json={"name": "Agency2", "source_type": "DATABASE"},
    )
    source_id = src.json()["id"]
    resp = await async_client_reviewer.post(
        "/api/v1/hermes/targets",
        json={"source_id": source_id, "url": "https://example.org/data"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["normalized_url"] == "https://example.org/data"


@pytest.mark.asyncio
async def test_analyst_can_list_targets(async_client_analyst: AsyncClient):
    resp = await async_client_analyst.get("/api/v1/hermes/targets")
    assert resp.status_code == 200, resp.text
    assert isinstance(resp.json(), list)


@pytest.mark.asyncio
async def test_analyst_cannot_create_target(async_client_analyst: AsyncClient):
    resp = await async_client_analyst.post(
        "/api/v1/hermes/targets",
        json={"source_id": str(uuid4()), "url": "https://x.com"},
    )
    assert resp.status_code in (401, 403)


@pytest.mark.asyncio
async def test_reviewer_can_enqueue_job(async_client_reviewer: AsyncClient):
    src = await async_client_reviewer.post(
        "/api/v1/hermes/sources",
        json={"name": "Agency3", "source_type": "ARCHIVE"},
    )
    source_id = src.json()["id"]
    tgt = await async_client_reviewer.post(
        "/api/v1/hermes/targets",
        json={"source_id": source_id, "url": "https://archive.example.com/feed"},
    )
    target_id = tgt.json()["id"]
    resp = await async_client_reviewer.post(
        f"/api/v1/hermes/targets/{target_id}/enqueue",
        json={"priority": 50},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "QUEUED"


@pytest.mark.asyncio
async def test_analyst_can_list_jobs(async_client_analyst: AsyncClient):
    resp = await async_client_analyst.get("/api/v1/hermes/jobs")
    assert resp.status_code == 200, resp.text
    assert isinstance(resp.json(), list)


@pytest.mark.asyncio
async def test_analyst_cannot_enqueue(async_client_analyst: AsyncClient):
    resp = await async_client_analyst.post(
        f"/api/v1/hermes/targets/{uuid4()}/enqueue",
        json={},
    )
    assert resp.status_code in (401, 403)


@pytest.mark.asyncio
async def test_recent_changes_returns_list(async_client_analyst: AsyncClient):
    resp = await async_client_analyst.get("/api/v1/hermes/changes/recent")
    assert resp.status_code == 200, resp.text
    assert isinstance(resp.json(), list)
