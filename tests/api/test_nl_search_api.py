"""API tests for Phase 7 NL search.

End-to-end HTTP coverage:

- Execute NL search returns parsed echo + items.
- Confidence and free-text remainder make it through to the response.
- Empty/over-length queries → 422 via Pydantic.
- Saved queries CRUD per-user.
- Cross-user delete returns 404.
- Unauthenticated requests → 401/403.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from httpx import AsyncClient

# ── Execute NL search ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_nl_search_happy_path(
    async_client_analyst: AsyncClient,
):
    resp = await async_client_analyst.post(
        "/api/v1/search/nl",
        json={"query": "737 fatal accidents in 2023"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    parsed = body["parsed"]
    assert parsed["aircraft_type"] == "Boeing 737"
    assert parsed["fatal_only"] is True
    assert parsed["event_date_from"] == "2023-01-01"
    assert parsed["event_date_to"] == "2023-12-31"
    assert parsed["confidence"] > 0.0
    assert "items" in body
    assert "log_id" in body


@pytest.mark.asyncio
async def test_nl_search_returns_free_text_remainder(
    async_client_analyst: AsyncClient,
):
    """A query mixing structured filters and free text routes the
    leftover into the FTS layer; the response echoes the remainder
    so the caller can see what FTS was given."""
    resp = await async_client_analyst.post(
        "/api/v1/search/nl",
        json={"query": "approach incidents in 2022"},
    )
    assert resp.status_code == 200
    remainder = resp.json()["parsed"]["free_text_remainder"]
    assert "approach" in remainder.lower()


@pytest.mark.asyncio
async def test_nl_search_empty_query_returns_422(
    async_client_analyst: AsyncClient,
):
    resp = await async_client_analyst.post("/api/v1/search/nl", json={"query": ""})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_nl_search_overlong_query_returns_422(
    async_client_analyst: AsyncClient,
):
    resp = await async_client_analyst.post("/api/v1/search/nl", json={"query": "x" * 501})
    assert resp.status_code == 422


# ── Saved queries ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_save_then_list_then_delete(
    async_client_analyst: AsyncClient,
):
    # Save.
    save_resp = await async_client_analyst.post(
        "/api/v1/search/nl/saved",
        json={
            "label": "737 fatal CRM",
            "raw_query": "737 fatal Crew Resource Management",
            "frozen_filters": {
                "aircraft_type": "Boeing 737",
                "fatal_only": True,
                "hfacs_category_codes": ["PRE-CRM"],
            },
        },
    )
    assert save_resp.status_code == 201, save_resp.text
    saved_id = save_resp.json()["id"]
    assert save_resp.json()["label"] == "737 fatal CRM"

    # List.
    list_resp = await async_client_analyst.get("/api/v1/search/nl/saved")
    assert list_resp.status_code == 200
    items = list_resp.json()["items"]
    assert any(s["id"] == saved_id for s in items)

    # Delete own.
    del_resp = await async_client_analyst.delete(f"/api/v1/search/nl/saved/{saved_id}")
    assert del_resp.status_code == 204


@pytest.mark.asyncio
async def test_delete_unknown_saved_returns_404(
    async_client_analyst: AsyncClient,
):
    resp = await async_client_analyst.delete(f"/api/v1/search/nl/saved/{uuid4()}")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "SAVED_NL_QUERY_NOT_FOUND"


@pytest.mark.asyncio
async def test_cross_user_delete_returns_404(
    async_client_analyst: AsyncClient,
    async_client_reviewer: AsyncClient,
):
    """A user attempting to delete another user's saved query
    gets 404 (rather than 403), preserving existence privacy."""
    # User A (analyst) saves.
    save_resp = await async_client_analyst.post(
        "/api/v1/search/nl/saved",
        json={
            "label": "a's saved",
            "raw_query": "x",
            "frozen_filters": {},
        },
    )
    saved_id = save_resp.json()["id"]
    # User B (reviewer) tries to delete it.
    cross_del = await async_client_reviewer.delete(f"/api/v1/search/nl/saved/{saved_id}")
    assert cross_del.status_code == 404
    # And the row still exists for the owner.
    list_resp = await async_client_analyst.get("/api/v1/search/nl/saved")
    assert any(s["id"] == saved_id for s in list_resp.json()["items"])


# ── Auth gates ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_nl_search_requires_auth(client: AsyncClient):
    resp = await client.post("/api/v1/search/nl", json={"query": "anything"})
    assert resp.status_code in (401, 403)


@pytest.mark.asyncio
async def test_saved_endpoints_require_auth(client: AsyncClient):
    for method, path, body in (
        ("post", "/api/v1/search/nl/saved", {"label": "x", "raw_query": "x", "frozen_filters": {}}),
        ("get", "/api/v1/search/nl/saved", None),
        ("delete", f"/api/v1/search/nl/saved/{uuid4()}", None),
    ):
        if method == "post":
            r = await client.post(path, json=body)
        elif method == "get":
            r = await client.get(path)
        else:
            r = await client.delete(path)
        assert r.status_code in (401, 403), path
