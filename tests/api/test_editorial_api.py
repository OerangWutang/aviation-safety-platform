"""API tests for the editorial workflow router (Phase 9).

These hit the FastAPI app in process with the in-memory UoW, so the
router, request validation, response serialization, role gates, and
the 409 (modified) / 410 (retracted) exception handlers all run in
production code paths.

Test policy
-----------

- One test per state transition that walks the happy path with a
  fresh page, instead of seeding mid-state.  Reads cleaner and pins
  the audit trail behaviour as well.
- Role-gating tests assert 401/403 — analyst cannot mutate; reviewer
  cannot retract.
- Optimistic-concurrency surfaces as 409 with the actual_version in
  the body.
- ``extra='forbid'`` rejection of projection-shaped keys is tested
  on the create endpoint, which is the broadest write surface.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from httpx import AsyncClient

from atlas.domain.entities import AccidentEvent
from tests.domain._fake_uow import InMemoryUnitOfWork

# ── Helpers ──────────────────────────────────────────────────────────────────


def _seed_event(uow: InMemoryUnitOfWork):
    event = AccidentEvent()
    uow.store.events[event.id] = event
    return event.id


async def _create_via_api(
    client: AsyncClient,
    uow: InMemoryUnitOfWork,
    *,
    slug: str = "test-page",
    title: str = "Test",
) -> dict:
    event_id = _seed_event(uow)
    resp = await client.post(
        "/api/v1/editorial/pages",
        json={"event_id": str(event_id), "slug": slug, "title": title},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _walk_via_api(
    client: AsyncClient,
    page_id: str,
    expected_version: int,
    path: str,
    *,
    body: dict | None = None,
) -> dict:
    resp = await client.post(
        f"/api/v1/editorial/pages/{page_id}/{path}",
        json={"expected_version": expected_version, **(body or {})},
    )
    assert resp.status_code == 200, (path, resp.status_code, resp.text)
    return resp.json()


# ── Create ───────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reviewer_can_create_draft(async_client_reviewer: AsyncClient, in_memory_uow):
    body = await _create_via_api(
        async_client_reviewer,
        in_memory_uow,
        slug="My New Page",
        title="My new page",
    )
    assert body["slug"] == "my-new-page"
    assert body["status"] == "DRAFT"
    assert body["version"] == 1
    assert body["allowed_next_statuses"] == ["IN_REVIEW"]


@pytest.mark.asyncio
async def test_analyst_cannot_create(async_client_analyst: AsyncClient, in_memory_uow):
    event_id = _seed_event(in_memory_uow)
    resp = await async_client_analyst.post(
        "/api/v1/editorial/pages",
        json={"event_id": str(event_id), "slug": "x", "title": "x"},
    )
    assert resp.status_code in (401, 403)


@pytest.mark.asyncio
async def test_unauth_create_is_rejected(client: AsyncClient):
    resp = await client.post(
        "/api/v1/editorial/pages",
        json={"event_id": str(uuid4()), "slug": "x", "title": "x"},
    )
    assert resp.status_code in (401, 403)


@pytest.mark.asyncio
async def test_create_rejects_projection_shaped_extras(
    async_client_reviewer: AsyncClient, in_memory_uow
):
    """``extra='forbid'`` blocks projection-shaped keys at the boundary.

    Phase 9's editorial surface intentionally cannot set operator /
    aircraft type / fatalities — those are evidence-backed.  The
    request schema must reject them with 422, not silently drop.
    """
    event_id = _seed_event(in_memory_uow)
    resp = await async_client_reviewer.post(
        "/api/v1/editorial/pages",
        json={
            "event_id": str(event_id),
            "slug": "leak",
            "title": "Title",
            "operator": "Editorial-set operator",
        },
    )
    assert resp.status_code == 422


# ── Read ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_page_returns_full_editorial_view(
    async_client_reviewer: AsyncClient, in_memory_uow
):
    body = await _create_via_api(async_client_reviewer, in_memory_uow)
    resp = await async_client_reviewer.get(f"/api/v1/editorial/pages/{body['id']}")
    assert resp.status_code == 200
    page = resp.json()
    # Editorial view exposes status + version, which the public detail
    # response does not.
    assert "status" in page
    assert "version" in page
    assert "allowed_next_statuses" in page


@pytest.mark.asyncio
async def test_get_missing_page_returns_404(
    async_client_reviewer: AsyncClient,
):
    resp = await async_client_reviewer.get(f"/api/v1/editorial/pages/{uuid4()}")
    assert resp.status_code == 404


# ── Update ───────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_patch_draft_updates_in_place(async_client_reviewer: AsyncClient, in_memory_uow):
    body = await _create_via_api(async_client_reviewer, in_memory_uow)
    resp = await async_client_reviewer.patch(
        f"/api/v1/editorial/pages/{body['id']}",
        json={"expected_version": 1, "title": "Revised"},
    )
    assert resp.status_code == 200
    updated = resp.json()
    assert updated["title"] == "Revised"
    assert updated["version"] == 2


@pytest.mark.asyncio
async def test_patch_with_stale_version_returns_409(
    async_client_reviewer: AsyncClient, in_memory_uow
):
    body = await _create_via_api(async_client_reviewer, in_memory_uow)
    # First writer wins.
    first = await async_client_reviewer.patch(
        f"/api/v1/editorial/pages/{body['id']}",
        json={"expected_version": 1, "title": "First"},
    )
    assert first.status_code == 200
    # Second writer is still on version=1.
    second = await async_client_reviewer.patch(
        f"/api/v1/editorial/pages/{body['id']}",
        json={"expected_version": 1, "title": "Second"},
    )
    assert second.status_code == 409
    payload = second.json()
    assert payload["error"]["code"] == "PUBLIC_EVENT_PAGE_MODIFIED"
    assert payload["error"]["details"]["actual_version"] == 2


# ── State transitions ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_full_happy_path_to_published(async_client_reviewer: AsyncClient, in_memory_uow):
    """Drive a fresh page through DRAFT -> PUBLISHED via the API.

    Each step asserts the response's status; the cumulative version
    bump (1 -> 2 -> 3 -> 4) is a tight check that no transition is
    silently a no-op.
    """
    body = await _create_via_api(async_client_reviewer, in_memory_uow)
    page_id = body["id"]
    assert body["version"] == 1

    submitted = await _walk_via_api(async_client_reviewer, page_id, 1, "submit")
    assert submitted["status"] == "IN_REVIEW"
    assert submitted["version"] == 2

    approved = await _walk_via_api(async_client_reviewer, page_id, 2, "approve")
    assert approved["status"] == "APPROVED"
    assert approved["version"] == 3

    published = await _walk_via_api(async_client_reviewer, page_id, 3, "publish")
    assert published["status"] == "PUBLISHED"
    assert published["version"] == 4
    assert published["last_published_at"] is not None
    assert published["first_published_at"] is not None


@pytest.mark.asyncio
async def test_request_changes_returns_to_draft(async_client_reviewer: AsyncClient, in_memory_uow):
    body = await _create_via_api(async_client_reviewer, in_memory_uow)
    page_id = body["id"]
    submitted = await _walk_via_api(async_client_reviewer, page_id, 1, "submit")
    returned = await _walk_via_api(
        async_client_reviewer,
        page_id,
        submitted["version"],
        "request-changes",
        body={"transition_reason": "needs more sources"},
    )
    assert returned["status"] == "DRAFT"


@pytest.mark.asyncio
async def test_archive_and_republish_round_trip(async_client_reviewer: AsyncClient, in_memory_uow):
    body = await _create_via_api(async_client_reviewer, in_memory_uow)
    page_id = body["id"]
    v = 1
    for path in ("submit", "approve", "publish"):
        result = await _walk_via_api(async_client_reviewer, page_id, v, path)
        v = result["version"]
    first_publish_ts = result["first_published_at"]

    archived = await _walk_via_api(async_client_reviewer, page_id, v, "archive")
    assert archived["status"] == "ARCHIVED"

    republished = await _walk_via_api(
        async_client_reviewer, page_id, archived["version"], "publish"
    )
    assert republished["status"] == "PUBLISHED"
    # first_published_at preserved across the round trip.
    assert republished["first_published_at"] == first_publish_ts


@pytest.mark.asyncio
async def test_invalid_transition_returns_4xx(async_client_reviewer: AsyncClient, in_memory_uow):
    """Submitting an already-IN_REVIEW page is not a valid transition."""
    body = await _create_via_api(async_client_reviewer, in_memory_uow)
    page_id = body["id"]
    submitted = await _walk_via_api(async_client_reviewer, page_id, 1, "submit")
    resp = await async_client_reviewer.post(
        f"/api/v1/editorial/pages/{page_id}/submit",
        json={"expected_version": submitted["version"]},
    )
    # DomainValidationError -> 400 via the generic handler in app.py.
    assert resp.status_code in (400, 409, 422)
    assert resp.json()["error"]["code"] == "INVALID_PUBLICATION_TRANSITION"


# ── Retract (admin only) ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_admin_can_retract_published_page(async_client_admin: AsyncClient, in_memory_uow):
    body = await _create_via_api(async_client_admin, in_memory_uow)
    page_id = body["id"]
    v = 1
    for path in ("submit", "approve", "publish"):
        result = await _walk_via_api(async_client_admin, page_id, v, path)
        v = result["version"]

    resp = await async_client_admin.post(
        f"/api/v1/editorial/pages/{page_id}/retract",
        json={"expected_version": v, "retraction_note": "Editorial correction"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "RETRACTED"
    assert body["retraction_note"] == "Editorial correction"
    # RETRACTED is terminal — allowed_next_statuses is empty.
    assert body["allowed_next_statuses"] == []


@pytest.mark.asyncio
async def test_reviewer_cannot_retract(async_client_reviewer: AsyncClient, in_memory_uow):
    """Retract is admin-only.  Reviewer must be rejected."""
    body = await _create_via_api(async_client_reviewer, in_memory_uow)
    page_id = body["id"]
    v = 1
    for path in ("submit", "approve", "publish"):
        result = await _walk_via_api(async_client_reviewer, page_id, v, path)
        v = result["version"]
    resp = await async_client_reviewer.post(
        f"/api/v1/editorial/pages/{page_id}/retract",
        json={"expected_version": v},
    )
    assert resp.status_code in (401, 403)


@pytest.mark.asyncio
async def test_retracted_page_410_carries_note(
    async_client_admin: AsyncClient,
    async_client_analyst: AsyncClient,
    in_memory_uow,
):
    """After admin retracts, public detail returns 410 with the note.

    Composes Phase 9 (retract) with Phase 1 (public detail) and
    verifies the wire contract end-to-end.
    """
    body = await _create_via_api(async_client_admin, in_memory_uow)
    page_id = body["id"]
    slug = body["slug"]
    v = 1
    for path in ("submit", "approve", "publish"):
        result = await _walk_via_api(async_client_admin, page_id, v, path)
        v = result["version"]
    await async_client_admin.post(
        f"/api/v1/editorial/pages/{page_id}/retract",
        json={"expected_version": v, "retraction_note": "Wrong operator."},
    )
    # Phase 1 public surface should now return 410.
    public_resp = await async_client_analyst.get(f"/api/v1/public/events/{slug}")
    assert public_resp.status_code == 410
    payload = public_resp.json()
    assert payload["error"]["details"]["retraction_note"] == "Wrong operator."


# ── Revisions ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_revisions_endpoint_returns_chronological_audit_trail(
    async_client_reviewer: AsyncClient, in_memory_uow
):
    body = await _create_via_api(async_client_reviewer, in_memory_uow)
    page_id = body["id"]
    v = 1
    for path in ("submit", "approve", "publish"):
        result = await _walk_via_api(async_client_reviewer, page_id, v, path)
        v = result["version"]

    resp = await async_client_reviewer.get(f"/api/v1/editorial/pages/{page_id}/revisions")
    assert resp.status_code == 200
    audit = resp.json()
    # 1 create + 3 transitions = 4 revisions.
    assert len(audit["revisions"]) == 4
    versions = [r["version_at_moment"] for r in audit["revisions"]]
    assert versions == sorted(versions)
    # First revision is the creation (NULL from_status).
    assert audit["revisions"][0]["from_status"] is None
    assert audit["revisions"][0]["to_status"] == "DRAFT"
    # Last revision is the publish.
    assert audit["revisions"][-1]["to_status"] == "PUBLISHED"


# ── Editorial list ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_editorial_list_excludes_retracted_by_default(
    async_client_admin: AsyncClient, in_memory_uow
):
    """Default list view excludes RETRACTED — those have their own
    audit path and shouldn't clutter the active worklist."""
    # Page A stays DRAFT.
    await _create_via_api(async_client_admin, in_memory_uow, slug="alive")

    # Page B walked through to RETRACTED.
    b = await _create_via_api(async_client_admin, in_memory_uow, slug="dead")
    page_id = b["id"]
    v = 1
    for path in ("submit", "approve", "publish"):
        result = await _walk_via_api(async_client_admin, page_id, v, path)
        v = result["version"]
    await async_client_admin.post(
        f"/api/v1/editorial/pages/{page_id}/retract",
        json={"expected_version": v},
    )

    resp = await async_client_admin.get("/api/v1/editorial/pages")
    assert resp.status_code == 200
    slugs = {item["slug"] for item in resp.json()["items"]}
    assert "alive" in slugs
    assert "dead" not in slugs


@pytest.mark.asyncio
async def test_editorial_list_status_filter(async_client_reviewer: AsyncClient, in_memory_uow):
    """Filter by an explicit status set."""
    await _create_via_api(async_client_reviewer, in_memory_uow, slug="d1")
    b = await _create_via_api(async_client_reviewer, in_memory_uow, slug="d2")
    await _walk_via_api(async_client_reviewer, b["id"], 1, "submit")

    resp = await async_client_reviewer.get("/api/v1/editorial/pages?statuses=DRAFT")
    assert resp.status_code == 200
    slugs = {item["slug"] for item in resp.json()["items"]}
    assert slugs == {"d1"}


@pytest.mark.asyncio
async def test_analyst_cannot_read_editorial_list(
    async_client_analyst: AsyncClient,
):
    """Editorial endpoints are reviewer/admin only.

    Analyst is the public-side reader role — they should NOT see
    DRAFT/IN_REVIEW content via the editorial surface.
    """
    resp = await async_client_analyst.get("/api/v1/editorial/pages")
    assert resp.status_code in (401, 403)
