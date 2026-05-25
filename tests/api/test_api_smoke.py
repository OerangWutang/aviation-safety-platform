"""Smoke tests for the FastAPI app that do not require PostgreSQL.

These live under tests/api/ (not tests/integration/) so they run in the
default pytest invocation without --run-integration and without a live DB.
"""

from __future__ import annotations

import importlib
from uuid import uuid4

from httpx import ASGITransport, AsyncClient

from tests.api.conftest import REQUIRED_ENV


async def test_health(client):
    resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


async def test_unknown_route_returns_404(client):
    resp = await client.get("/does-not-exist")
    assert resp.status_code == 404


async def test_ingestion_requires_auth(client):
    resp = await client.post(
        f"/api/v1/ingestion/sources/{uuid4()}",
        json={"claims": [], "raw_payload": {}, "captured_at": "2024-01-01T00:00:00Z"},
    )
    assert resp.status_code in (401, 403)


async def test_admin_requires_auth(client):
    resp = await client.post("/api/v1/admin/projections/rebuild", json={"all": True})
    assert resp.status_code in (401, 403)


async def test_provenance_requires_auth(client):
    """Regression: the provenance route previously had no auth dependency."""
    resp = await client.get(f"/api/v1/accidents/{uuid4()}/provenance")
    assert resp.status_code in (401, 403)


async def test_provenance_include_archive_returns_501(async_client_reviewer):
    """Documented include_archive flag must return a clean 501 until implemented."""
    resp = await async_client_reviewer.get(
        f"/api/v1/accidents/{uuid4()}/provenance?include_archive=true"
    )
    assert resp.status_code == 501
    assert "Archive retrieval is not supported yet" in resp.text


async def test_conflict_history_include_archive_returns_501(async_client_reviewer):
    """Documented include_archive flag must return a clean 501 until implemented."""
    resp = await async_client_reviewer.get(
        f"/api/v1/conflicts/{uuid4()}/history?include_archive=true"
    )
    assert resp.status_code == 501
    assert "Archive retrieval is not supported yet" in resp.text


async def test_accidents_get_requires_auth(client):
    resp = await client.get(f"/api/v1/accidents/{uuid4()}")
    assert resp.status_code in (401, 403)


async def test_conflicts_list_requires_auth(client):
    resp = await client.get("/api/v1/conflicts")
    assert resp.status_code in (401, 403)


async def test_resolve_conflict_requires_auth(client):
    resp = await client.post(
        f"/api/v1/conflicts/{uuid4()}/resolve",
        json={"expected_version": 1, "winning_claim_id": str(uuid4())},
    )
    assert resp.status_code in (401, 403)


async def test_reopen_conflict_endpoint_requires_auth(client):
    """Reopen route used to return a flat 501; verify it now exists and is gated."""
    resp = await client.post(
        f"/api/v1/conflicts/{uuid4()}/reopen",
        json={"expected_version": 1},
    )
    assert resp.status_code in (401, 403)


async def test_reopen_conflict_endpoint_validates_body(client):
    """Missing expected_version should never hit the old not-implemented path."""
    resp = await client.post(
        f"/api/v1/conflicts/{uuid4()}/reopen",
        json={},
    )
    assert resp.status_code != 501


async def test_resolve_request_requires_exactly_one_resolution_choice(client):
    """Schema-level validator rejects requests with both winner and override."""
    resp = await client.post(
        f"/api/v1/conflicts/{uuid4()}/resolve",
        json={
            "expected_version": 1,
            "winning_claim_id": str(uuid4()),
            "manual_override_value": "X",
        },
    )
    assert resp.status_code in (401, 403, 422)


async def test_invalid_api_key_returns_403(client):
    resp = await client.get(
        f"/api/v1/accidents/{uuid4()}",
        headers={"X-API-Key": "definitely-not-a-real-key"},
    )
    assert resp.status_code == 403


async def test_missing_event_id_on_conflicts_list_is_422(client):
    """Regression: this used to return 400; no-auth path should never return 400."""
    resp = await client.get("/api/v1/conflicts")
    assert resp.status_code != 400


async def test_merge_endpoint_requires_auth(client):
    """The merge endpoint is implemented and must reject unauthenticated callers."""
    resp = await client.post(
        "/api/v1/admin/events/merge",
        json={
            "source_event_id": str(uuid4()),
            "target_event_id": str(uuid4()),
        },
    )
    assert resp.status_code in (401, 403), (
        f"expected auth challenge (401/403), got {resp.status_code}: {resp.text}"
    )
    assert resp.status_code != 501, "501 Not Implemented is stale - merge is now wired"


async def test_oversized_request_body_rejected_before_auth(monkeypatch):
    """413 with the error envelope is returned before auth runs."""
    env = {
        **REQUIRED_ENV,
        "MAX_RAW_PAYLOAD_BYTES": "32",
        "REQUEST_BODY_OVERHEAD_BYTES": "0",
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    import atlas.config as config

    config.get_settings.cache_clear()
    app_module = importlib.import_module("atlas.presentation.api.app")

    async def _noop(*_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr(app_module, "assert_curator_override_source", _noop)
    test_app = app_module.create_app()
    async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test") as ac:
        resp = await ac.post(
            f"/api/v1/ingestion/sources/{uuid4()}",
            json={"raw_payload": {"blob": "x" * 100}, "claims": []},
        )

    assert resp.status_code == 413
    assert resp.json()["error"]["code"] == "REQUEST_BODY_TOO_LARGE"


async def test_set_source_field_mapping_route_is_wired_and_requires_auth(client):
    resp = await client.put(
        f"/api/v1/admin/sources/{uuid4()}/field-mapping",
        json={"field_mapping": {"date": "event_date"}},
    )
    assert resp.status_code in (401, 403), (
        f"expected auth challenge, got {resp.status_code}: {resp.text}"
    )
    assert resp.status_code not in (404, 405)


async def test_set_source_field_mapping_validates_body_before_auth_is_neutral(client):
    resp = await client.put(
        f"/api/v1/admin/sources/{uuid4()}/field-mapping",
        json={"field_mapping": "not-a-dict"},
    )
    assert resp.status_code != 500


async def test_security_headers_present_on_health(client):
    resp = await client.get("/health")
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["x-frame-options"] == "DENY"
    assert resp.headers["referrer-policy"] == "no-referrer"
    assert "permissions-policy" in resp.headers


async def test_trusted_host_rejects_unlisted_hosts(monkeypatch):
    env = {
        **REQUIRED_ENV,
        "ALLOWED_HOSTS": "api.example.com",
        "SECURITY_HEADERS_ENABLED": "true",
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    import atlas.config as config

    config.get_settings.cache_clear()
    app_module = importlib.import_module("atlas.presentation.api.app")

    async def _noop(*_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr(app_module, "assert_curator_override_source", _noop)
    test_app = app_module.create_app()
    async with AsyncClient(
        transport=ASGITransport(app=test_app), base_url="http://evil.example.com"
    ) as ac:
        resp = await ac.get("/health")

    assert resp.status_code == 400
    assert "Invalid host" in resp.text


async def test_production_docs_are_disabled_by_default(monkeypatch):
    env = {
        **REQUIRED_ENV,
        "ENVIRONMENT": "production",
        "API_KEY_HASH_SECRET": "0000000000000000000000000000000000000000000000000000000000000000",
        "ALLOWED_HOSTS": "api.example.com",
        "SECURITY_HEADERS_ENABLED": "true",
        "PROMETHEUS_ALLOWED_CIDRS": "127.0.0.1/32",
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("API_DOCS_ENABLED", raising=False)

    import atlas.config as config

    config.get_settings.cache_clear()
    app_module = importlib.import_module("atlas.presentation.api.app")

    async def _noop(*_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr(app_module, "assert_curator_override_source", _noop)
    test_app = app_module.create_app()
    async with AsyncClient(
        transport=ASGITransport(app=test_app), base_url="http://api.example.com"
    ) as ac:
        docs = await ac.get("/docs")
        openapi = await ac.get("/openapi.json")

    assert docs.status_code == 404
    assert openapi.status_code == 404


# ── /metrics endpoint access control ──────────────────────────────────────


async def test_metrics_returns_404_for_external_ip(monkeypatch):
    """/metrics must return 404 (not 403) for requests from non-allowed IPs.

    Returning 403 would reveal the endpoint exists and is protected, giving
    attackers a map of internal operational surfaces.  404 is the correct
    security-by-obscurity posture for a metrics endpoint that should only
    be accessible from trusted infrastructure CIDRs.

    httpx's ASGITransport uses 127.0.0.1 as the default client (in the default
    CIDR allowlist).  This test overrides the CIDR to exclude the test
    client IP to exercise the denial path.
    """
    env = {
        **REQUIRED_ENV,
        "PROMETHEUS_ALLOWED_CIDRS": "10.0.0.0/8",  # exclude 127.0.0.1
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("PROMETHEUS_BEARER_TOKEN", raising=False)

    import atlas.config as config

    config.get_settings.cache_clear()
    app_module = importlib.import_module("atlas.presentation.api.app")

    async def _noop(*_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr(app_module, "assert_curator_override_source", _noop)
    test_app = app_module.create_app()
    # ASGITransport default client is 127.0.0.1, not in 10.0.0.0/8 → denied.
    async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test") as ac:
        resp = await ac.get("/metrics")

    # 404 means "this endpoint does not exist for you" — correct.
    # 403 would reveal the endpoint exists and is protected — wrong.
    assert resp.status_code == 404, (
        f"Expected 404 (security-by-obscurity), got {resp.status_code}. "
        "If 403, the endpoint is leaking its existence to non-allowed callers."
    )


async def test_metrics_not_cached_on_denial(monkeypatch):
    """/metrics response carries no-store cache control even when denied.

    Stale cache entries for 404 responses could mask a later configuration
    change that enables legitimate scraping.
    """
    env = {
        **REQUIRED_ENV,
        "PROMETHEUS_ALLOWED_CIDRS": "10.0.0.0/8",  # deny test client
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    import atlas.config as config

    config.get_settings.cache_clear()
    app_module = importlib.import_module("atlas.presentation.api.app")

    async def _noop(*_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr(app_module, "assert_curator_override_source", _noop)
    test_app = app_module.create_app()
    async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test") as ac:
        resp = await ac.get("/metrics")

    assert "no-store" in resp.headers.get("cache-control", ""), (
        f"Expected 'no-store' in Cache-Control for /metrics denial, "
        f"got: {resp.headers.get('cache-control', '')!r}"
    )
