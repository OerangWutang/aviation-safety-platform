"""API-level tests with role-based auth and schema validation.

Strategy:
- Auth-only tests (no use-case calls): override ``get_current_user`` via
  FastAPI dependency overrides. No DB needed.
- Use-case tests (go past auth): override both ``get_current_user`` AND
  ``get_uow`` with the in-memory UoW, so they never touch Postgres.

All tests run without a database.

UUID stability
--------------
UUIDs inside ``@pytest.mark.parametrize`` are declared as module-level
constants so that test node IDs are stable across runs, making ``pytest --lf``
work correctly.
"""

from __future__ import annotations

import importlib
from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient

from atlas.application.dto import CurrentUser

_REQUIRED_ENV = {
    "DATABASE_URL": "postgresql+asyncpg://user:pass@localhost:5432/atlas",
    "DATABASE_SYNC_URL": "postgresql://user:pass@localhost:5432/atlas",
    "POSTGRES_USER": "user",
    "POSTGRES_PASSWORD": "pass",
    "POSTGRES_DB": "atlas",
}

_ADMIN = CurrentUser(user_id=UUID("00000000-0000-0000-0000-000000000001"), role="admin")
_REVIEWER = CurrentUser(user_id=UUID("00000000-0000-0000-0000-000000000002"), role="reviewer")
_ANALYST = CurrentUser(user_id=UUID("00000000-0000-0000-0000-000000000003"), role="analyst")

# Fixed UUIDs for stable parametrize node IDs (uuid4() at import time breaks --lf).
_UUID_A = "00000000-0000-0000-0000-000000000010"
_UUID_B = "00000000-0000-0000-0000-000000000011"
_UUID_C = "00000000-0000-0000-0000-000000000012"
_UUID_D = "00000000-0000-0000-0000-000000000013"
_UUID_E = "00000000-0000-0000-0000-000000000014"


async def _noop(*_a, **_kw):
    return None


def _make_app(monkeypatch=None, request=None):
    """Return the session-shared domain app.

    ``monkeypatch`` and ``request`` are accepted for backward compatibility but
    are no longer used: env vars are set at conftest import time, the startup
    hook is patched session-wide, and settings cleanup is handled by the
    ``_clear_settings_cache_after_domain_test`` autouse fixture.
    """
    from tests.domain.conftest import _DOMAIN_SHARED_APP

    if _DOMAIN_SHARED_APP is None:
        # Fallback for direct invocation outside the normal session lifecycle.
        mod = importlib.import_module("atlas.presentation.api.app")
        return mod.create_app()
    return _DOMAIN_SHARED_APP


def _authed(app, user: CurrentUser) -> AsyncClient:
    from atlas.presentation.api import dependencies

    async def _stamp(request):
        app.dependency_overrides[dependencies.get_current_user] = lambda: user

    return AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        event_hooks={"request": [_stamp]},
    )


def _authed_with_uow(app, user: CurrentUser):
    from atlas.presentation.api import dependencies
    from tests.domain._fake_uow import InMemoryUnitOfWork

    uow = InMemoryUnitOfWork()
    # UoW overrides: stable for the test, set upfront.
    app.dependency_overrides[dependencies.get_uow] = lambda: uow
    app.dependency_overrides[dependencies.get_public_uow] = lambda: uow

    # User identity: stamped per-request so simultaneous clients don't collide.
    async def _stamp(request):
        app.dependency_overrides[dependencies.get_current_user] = lambda: user

    return (
        AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            event_hooks={"request": [_stamp]},
        ),
        uow,
    )


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture()
def app(_domain_shared_app):
    """Return the session-shared domain app; dependency_overrides cleared after each test."""
    return _domain_shared_app


# ── Health / readiness ──────────────────────────────────────────────────────


async def test_health_open(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


async def test_ready_no_auth_required(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/ready")
    assert r.status_code not in (401, 403)


# ── Missing API key -> 401/403 ───────────────────────────────────────────────
# All protected endpoints must reject unauthenticated requests.
# UUIDs are module-level constants so test node IDs are stable across runs.


@pytest.mark.parametrize(
    "method,path",
    [
        # accidents
        ("GET", f"/api/v1/accidents/{_UUID_A}"),
        ("GET", f"/api/v1/accidents/{_UUID_A}/provenance"),
        # conflicts
        ("GET", "/api/v1/conflicts"),
        ("GET", f"/api/v1/conflicts/{_UUID_B}"),
        ("GET", f"/api/v1/conflicts/{_UUID_B}/history"),
        ("POST", f"/api/v1/conflicts/{_UUID_B}/resolve"),
        ("POST", f"/api/v1/conflicts/{_UUID_B}/reopen"),
        # ingestion
        ("POST", f"/api/v1/ingestion/sources/{_UUID_C}"),
        # admin - projections
        ("POST", "/api/v1/admin/projections/rebuild"),
        ("GET", f"/api/v1/admin/projections/verify?event_id={_UUID_D}"),
        # admin - outbox
        ("GET", "/api/v1/admin/outbox"),
        ("POST", "/api/v1/admin/outbox/process"),
        # admin - metrics
        ("GET", "/api/v1/admin/metrics"),
        # admin - reviews
        ("GET", "/api/v1/admin/reviews"),
        ("POST", f"/api/v1/admin/reviews/{_UUID_E}/resolve"),
        # admin - merge
        ("POST", "/api/v1/admin/events/merge"),
    ],
)
async def test_endpoint_rejects_missing_key(app, method, path):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        if method == "GET":
            r = await c.get(path)
        else:
            r = await c.post(path, json={})
    assert r.status_code in (401, 403), f"{method} {path} -> {r.status_code}"


# ── Role enforcement ────────────────────────────────────────────────────────


async def test_analyst_cannot_resolve_conflict(app):
    async with _authed(app, _ANALYST) as c:
        r = await c.post(
            f"/api/v1/conflicts/{_UUID_A}/resolve",
            json={"expected_version": 1, "winning_claim_id": str(_UUID_B)},
        )
    assert r.status_code == 403


async def test_analyst_cannot_reopen_conflict(app):
    async with _authed(app, _ANALYST) as c:
        r = await c.post(f"/api/v1/conflicts/{_UUID_A}/reopen", json={"expected_version": 1})
    assert r.status_code == 403


async def test_analyst_cannot_rebuild(app):
    async with _authed(app, _ANALYST) as c:
        r = await c.post("/api/v1/admin/projections/rebuild", json={"all": False})
    assert r.status_code == 403


async def test_reviewer_cannot_rebuild(app):
    async with _authed(app, _REVIEWER) as c:
        r = await c.post("/api/v1/admin/projections/rebuild", json={"all": False})
    assert r.status_code == 403


# ── Reviewer can reach resolve/reopen (uses in-memory UoW) ─────────────────


async def test_reviewer_can_reach_resolve(app):
    async with _authed_with_uow(app, _REVIEWER)[0] as c:
        r = await c.post(
            f"/api/v1/conflicts/{_UUID_A}/resolve",
            json={"expected_version": 1, "winning_claim_id": str(_UUID_B)},
        )
    assert r.status_code != 403


async def test_reviewer_can_reach_reopen(app):
    async with _authed_with_uow(app, _REVIEWER)[0] as c:
        r = await c.post(f"/api/v1/conflicts/{_UUID_A}/reopen", json={"expected_version": 1})
    assert r.status_code != 403


async def test_admin_can_reach_rebuild(app):
    async with _authed_with_uow(app, _ADMIN)[0] as c:
        r = await c.post("/api/v1/admin/projections/rebuild", json={"all": False})
    assert r.status_code != 403


async def test_analyst_can_read_accident(app):
    async with _authed_with_uow(app, _ANALYST)[0] as c:
        r = await c.get(f"/api/v1/accidents/{_UUID_A}")
    assert r.status_code not in (401, 403)


async def test_analyst_can_read_provenance(app):
    async with _authed_with_uow(app, _ANALYST)[0] as c:
        r = await c.get(f"/api/v1/accidents/{_UUID_A}/provenance")
    assert r.status_code not in (401, 403)


# ── Schema / body validation (Pydantic, no DB) ─────────────────────────────


async def test_ingestion_empty_claims_returns_422(app):
    async with _authed(app, _REVIEWER) as c:
        r = await c.post(
            f"/api/v1/ingestion/sources/{_UUID_A}",
            json={"raw_payload": {}, "claims": []},
        )
    assert r.status_code == 422


async def test_ingestion_missing_claims_returns_422(app):
    async with _authed(app, _REVIEWER) as c:
        r = await c.post(
            f"/api/v1/ingestion/sources/{_UUID_A}",
            json={"raw_payload": {}},
        )
    assert r.status_code == 422


async def test_resolve_missing_resolution_returns_422(app):
    async with _authed(app, _REVIEWER) as c:
        r = await c.post(
            f"/api/v1/conflicts/{_UUID_A}/resolve",
            json={"expected_version": 1},
        )
    assert r.status_code == 422


async def test_resolve_both_claim_and_override_returns_422(app):
    async with _authed(app, _REVIEWER) as c:
        r = await c.post(
            f"/api/v1/conflicts/{_UUID_A}/resolve",
            json={
                "expected_version": 1,
                "winning_claim_id": str(_UUID_B),
                "manual_override_value": "conflict",
            },
        )
    assert r.status_code == 422


async def test_conflicts_list_without_event_id_returns_422(app):
    async with _authed_with_uow(app, _ANALYST)[0] as c:
        r = await c.get("/api/v1/conflicts")
    assert r.status_code == 422


async def test_rebuild_all_without_max_events_returns_422(app):
    async with _authed_with_uow(app, _ADMIN)[0] as c:
        r = await c.post("/api/v1/admin/projections/rebuild", json={"all": True})
    assert r.status_code == 422


# ── Error response shapes ───────────────────────────────────────────────────


async def test_404_accident_uses_error_envelope(app):
    async with _authed_with_uow(app, _ANALYST)[0] as c:
        r = await c.get(f"/api/v1/accidents/{_UUID_A}")
    assert r.status_code == 404
    body = r.json()
    assert body["error"]["code"] == "HTTP_404"
    assert body["error"]["message"]


async def test_503_ready_shape(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/ready")
    if r.status_code == 503:
        assert r.json().get("status") == "not ready"


async def test_merge_requires_body(app):
    """Merge endpoint requires source/target event_id - missing body -> 422."""
    async with _authed(app, _ADMIN) as c:
        r = await c.post("/api/v1/admin/events/merge")
    assert r.status_code == 422


# ── Reopen endpoint is wired (not 501) ─────────────────────────────────────


async def test_reopen_is_not_501(app):
    """The reopen route was previously a flat 501 stub. Verify it's wired."""
    async with _authed_with_uow(app, _REVIEWER)[0] as c:
        r = await c.post(f"/api/v1/conflicts/{_UUID_A}/reopen", json={"expected_version": 1})
    assert r.status_code != 501


# ── Validation error response shape ──────────────────────────────────────────


async def test_validation_error_does_not_include_pydantic_url_fields(app):
    """422 responses must not contain Pydantic doc URL fields.

    Pydantic v2's ``ValidationError.errors()`` includes a ``url`` field
    pointing to ``https://errors.pydantic.dev/…`` in each error dict.
    These expose library-version detail and are noise for API consumers.
    The app handler strips them before serialisation.
    """
    # Trigger a validation error: ingestion requires a non-empty claims list.
    async with _authed(app, _ADMIN) as c:
        r = await c.post(
            f"/api/v1/ingestion/sources/{_UUID_A}",
            json={"claims": []},
        )
    assert r.status_code == 422
    body = r.json()
    assert "error" in body
    errors = body["error"].get("details", {}).get("errors", [])
    assert errors, "Expected at least one validation error entry"
    for err in errors:
        assert "url" not in err, f"Pydantic doc URL leaked into 422 response: {err}"


async def test_validation_error_shape_is_correct_envelope(app):
    """Validation errors are wrapped in the canonical error envelope."""
    async with _authed(app, _ADMIN) as c:
        r = await c.post(
            f"/api/v1/ingestion/sources/{_UUID_A}",
            json={"claims": []},
        )
    assert r.status_code == 422
    body = r.json()
    assert body["error"]["code"] == "VALIDATION_ERROR"
    assert body["error"]["message"] == "Request validation failed"
    assert isinstance(body["error"]["details"]["errors"], list)
