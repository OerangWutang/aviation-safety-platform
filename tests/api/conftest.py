"""Fixtures for no-DB API tests.

These tests run the FastAPI app in-process with a mocked lifespan.
They do not require PostgreSQL - no DB is started, no migrations are run.

Performance note
----------------
``create_app()`` takes ~190ms because it registers 22 routers.  Creating a
new app per test (224 API tests) previously accounted for nearly the entire
API-suite runtime (~43 s of 51 s).

The fix: create the app **once** at session scope in ``_shared_api_app`` and
reuse it for all per-test clients.  Each test gets a fresh ``InMemoryUnitOfWork``
(function-scoped) for isolation; the app's ``dependency_overrides`` dict is
updated per test and cleared in a ``finally`` block so no overrides leak between
tests.  Tests that require a clean app (e.g. auth/smoke tests using ``client``)
call ``dependency_overrides.clear()`` at the start of the fixture.
"""

from __future__ import annotations

import os
import uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from atlas.application.dto import CurrentUser
from atlas.domain.enums import Role
from tests.domain._fake_uow import InMemoryUnitOfWork

# Module-level handle to the session-shared app; set by ``_shared_api_app``.
# ``make_tenant_client_for`` reads it so call sites need no fixture changes.
_SHARED_APP = None

REQUIRED_ENV = {
    "DATABASE_URL": "postgresql+asyncpg://user:pass@localhost:5432/atlas",
    "DATABASE_SYNC_URL": "postgresql://user:pass@localhost:5432/atlas",
    "POSTGRES_USER": "user",
    "POSTGRES_PASSWORD": "pass",
    "POSTGRES_DB": "atlas",
}

# Set env vars at import time (collection phase) so that when
# ``atlas.presentation.api.app`` is first imported it can call
# ``create_app() -> get_settings()`` without hitting a missing-DATABASE_URL error.
# Using setdefault preserves any pre-existing values (e.g. a developer running
# with a real .env), so this only injects the minimal test stubs.
for _k, _v in REQUIRED_ENV.items():
    os.environ.setdefault(_k, _v)
del _k, _v  # clean up loop variables from module namespace


# ── Session-level setup ───────────────────────────────────────────────────────


class _EmptyResult:
    def scalar_one_or_none(self) -> None:
        return None

    def scalars(self):
        return self

    def all(self) -> list[None]:
        return []


class _NoDbSession:
    async def execute(self, _stmt):
        return _EmptyResult()

    async def rollback(self) -> None:
        return None

    async def commit(self) -> None:
        return None

    def in_transaction(self) -> bool:
        return False


@pytest.fixture(scope="session", autouse=True)
def _api_test_session_env():
    """Patch DB/startup hooks once for the entire test session.

    * Sets REQUIRED_ENV in the process environment (no monkeypatch so the
      values survive for the whole session).
    * Replaces ``async_session_factory`` in the session and dependencies
      modules with a no-op that returns ``_NoDbSession``.  This is safe for
      all API tests: auth-path tests rely on it directly; role-based tests
      override ``get_uow`` via ``dependency_overrides`` and never reach the
      factory.
    * Replaces ``assert_curator_override_source`` in the app module so the
      ASGI lifespan does not attempt a real DB check on each client open.

    Originals are restored on session teardown.
    """
    from contextlib import asynccontextmanager

    import atlas.config as cfg
    import atlas.infrastructure.db.session as db_session
    import atlas.presentation.api.app as app_module
    import atlas.presentation.api.dependencies as deps

    cfg.get_settings.cache_clear()

    @asynccontextmanager
    async def _fake_session_factory():
        yield _NoDbSession()

    _orig_db = db_session.async_session_factory
    _orig_deps = deps.async_session_factory
    _orig_startup = app_module.assert_curator_override_source

    db_session.async_session_factory = _fake_session_factory
    deps.async_session_factory = _fake_session_factory

    async def _noop(*_, **__):
        return None

    app_module.assert_curator_override_source = _noop

    yield

    db_session.async_session_factory = _orig_db
    deps.async_session_factory = _orig_deps
    app_module.assert_curator_override_source = _orig_startup


@pytest.fixture(scope="session", autouse=True)
def _shared_api_app(_api_test_session_env):
    """Create the FastAPI app **once** per test session.

    ``autouse=True`` guarantees this fixture runs before every test in
    ``tests/api/``, including tests that call ``make_tenant_client_for``
    directly (which reads ``_SHARED_APP``).  Without autouse, a test that
    does not declare this fixture as a dependency would see ``_SHARED_APP
    is None`` and fall back to the slower ``create_app()`` path, which can
    also expose test-isolation bugs if the module-level ``async_session_factory``
    patch has not yet been applied by ``_api_test_session_env``.

    All per-test client fixtures reuse this app.  The app is stateless between
    requests; only ``dependency_overrides`` change per test, and those are
    cleared in fixture teardown.
    """
    import atlas.presentation.api.app as app_module

    global _SHARED_APP
    _SHARED_APP = app_module.create_app()
    return _SHARED_APP


@pytest.fixture(autouse=True)
def _clear_settings_cache_after_test():
    """Re-read Settings after each test.

    Some tests (e.g. production-config smoke tests) use ``monkeypatch.setenv``
    to temporarily alter the environment and call ``get_settings.cache_clear()``
    themselves.  Clearing the cache here ensures those temporary overrides do
    not persist into the next test after monkeypatch has restored the env.
    """
    yield
    import atlas.config as cfg

    cfg.get_settings.cache_clear()


# ── Per-test fixtures ─────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def in_memory_uow():
    """Fresh InMemoryUnitOfWork per test.

    Env vars and settings are set at session scope; no monkeypatch needed here.
    """
    return InMemoryUnitOfWork()


def _fake_user(role: Role) -> CurrentUser:
    return CurrentUser(user_id=uuid.uuid4(), role=role.value)


@pytest_asyncio.fixture()
async def client(_shared_api_app):
    """HTTP client for auth/smoke tests that exercise the real auth path.

    Clears ``dependency_overrides`` so the app is in its pristine state: no
    mocked ``get_current_user`` or ``get_uow``.  Auth logic falls through to
    ``_NoDbSession`` (patched at session scope), which returns no API-key row
    and therefore rejects all requests with missing/invalid keys.
    """
    _shared_api_app.dependency_overrides.clear()
    async with AsyncClient(
        transport=ASGITransport(app=_shared_api_app), base_url="http://test"
    ) as ac:
        yield ac


# ── Role-based in-memory clients ─────────────────────────────────────────────
#
# User identity is injected via an httpx ``event_hooks["request"]`` callback
# that fires immediately before each request dispatch, rather than at fixture
# setup time.  This lets two role-based clients coexist in the same test (e.g.
# a test that uses both ``async_client_admin`` and ``async_client_analyst``)
# without the second fixture's setup overwriting the first's
# ``dependency_overrides[get_current_user]`` entry.
#
# UoW overrides ARE set at setup time because they point at the test's shared
# ``in_memory_uow`` and do not conflict between simultaneous clients.


def _role_client(app, uow, role: Role):
    """Return (AsyncClient, cleanup_fn) for the given role against the shared app."""
    from atlas.presentation.api.dependencies import (
        get_current_user,
        get_public_uow,
        get_tenant_uow,
        get_uow,
    )

    fake_user = _fake_user(role)

    # Use async overrides so FastAPI doesn't route through threadpool execution
    # for sync callables during dependency resolution.
    async def _uow_override():
        return uow

    async def _tenant_uow_override(_tenant_id: uuid.UUID):
        return uow

    async def _current_user_override():
        return fake_user

    # UoW overrides: stable for the whole test, safe to set at setup.
    app.dependency_overrides[get_uow] = _uow_override
    app.dependency_overrides[get_public_uow] = _uow_override
    app.dependency_overrides[get_tenant_uow] = _tenant_uow_override

    # User identity: injected per-request so multiple simultaneous clients
    # each stamp the correct user just before asyncio dispatches their request.
    # Must be async: httpx.AsyncClient awaits every event hook.
    async def _stamp_user(request):
        app.dependency_overrides[get_current_user] = _current_user_override

    return AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
        event_hooks={"request": [_stamp_user]},
    )


@pytest_asyncio.fixture
async def async_client_reviewer(_shared_api_app, in_memory_uow):
    try:
        async with _role_client(_shared_api_app, in_memory_uow, Role.REVIEWER) as ac:
            yield ac
    finally:
        _shared_api_app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def async_client_analyst(_shared_api_app, in_memory_uow):
    try:
        async with _role_client(_shared_api_app, in_memory_uow, Role.ANALYST) as ac:
            yield ac
    finally:
        _shared_api_app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def async_client_admin(_shared_api_app, in_memory_uow):
    try:
        async with _role_client(_shared_api_app, in_memory_uow, Role.ADMIN) as ac:
            yield ac
    finally:
        _shared_api_app.dependency_overrides.clear()


def make_tenant_client_for(
    tenant_id,
    tenant_role: str,
    uow: InMemoryUnitOfWork,
    monkeypatch=None,
    *,
    system_role: Role = Role.ANALYST,
):
    """Return an app configured for tenant-scoped API tests.

    Uses the session-shared app when ``shared_app`` is provided (the fast
    path).  Falls back to ``create_app()`` when called without one (backward
    compatibility for ad-hoc use or tests that do not request the session
    fixture).  ``monkeypatch`` is accepted but ignored when ``shared_app`` is
    provided; it is still patching ``assert_curator_override_source`` in the
    fall-back path.

    The caller is responsible for using the returned app within a single test:
    since tests run sequentially the overrides set here will be overwritten by
    the next call before they can interfere.
    """
    # Use the session-shared app when available (fast path: no create_app()).
    # Fall back to creating a fresh app for backward compatibility when called
    # outside the normal test session (e.g. standalone scripts or one-off tests
    # that do not have the session fixture active).
    import sys  # noqa: F401

    import atlas.presentation.api.app as app_module
    from atlas.application.dto import CurrentTenantUser
    from atlas.domain.tenancy.entities import TenantMembership, TenantRole
    from atlas.presentation.api.dependencies import (
        get_current_tenant_user,
        get_current_user,
        get_public_uow,
        get_tenant_uow,
        get_uow,
    )

    if _SHARED_APP is not None:
        app = _SHARED_APP
    else:

        async def _noop(*_args, **_kwargs):
            return None

        if monkeypatch is not None:
            monkeypatch.setattr(app_module, "assert_curator_override_source", _noop, raising=False)
        app = app_module.create_app()

    fake_user = _fake_user(system_role)
    role_value = tenant_role.value if hasattr(tenant_role, "value") else str(tenant_role)
    if not any(
        m.tenant_id == tenant_id and m.user_id == fake_user.user_id
        for m in uow.store.tenancy.memberships
    ):
        uow.store.tenancy.memberships.append(
            TenantMembership(
                tenant_id=tenant_id,
                user_id=fake_user.user_id,
                tenant_role=TenantRole(role_value),
            )
        )
    fake_tenant_user = CurrentTenantUser(
        user_id=fake_user.user_id,
        role=fake_user.role,
        tenant_id=tenant_id,
        tenant_role=role_value,
    )
    app.dependency_overrides[get_uow] = lambda: uow
    app.dependency_overrides[get_public_uow] = lambda: uow
    app.dependency_overrides[get_tenant_uow] = lambda: uow
    app.dependency_overrides[get_current_user] = lambda: fake_user
    app.dependency_overrides[get_current_tenant_user] = lambda: fake_tenant_user
    return app


# ── Seeded-data fixtures ──────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def seeded_event_with_projection(in_memory_uow):
    from atlas.domain.entities import AccidentEvent, ProjectedAccidentRecord

    event_id = uuid.uuid4()
    in_memory_uow._store.events[event_id] = AccidentEvent(id=event_id)
    in_memory_uow._store.projections[event_id] = ProjectedAccidentRecord(
        event_id=event_id,
        fields={
            "registration": "PH-BXA",
            "operator": "KLM Royal Dutch Airlines",
            "aircraft_type": "Boeing 737-800",
            "manufacturer": "Boeing",
            "airport": "EHAM",
            "location": "Amsterdam Schiphol",
            "country": "Netherlands",
            "investigation_agency": "Dutch Safety Board",
        },
    )
    return event_id


@pytest_asyncio.fixture
async def seeded_chronos_event_with_projection(in_memory_uow):
    from atlas.domain.entities import AccidentEvent, ProjectedAccidentRecord

    event_id = uuid.uuid4()
    in_memory_uow._store.events[event_id] = AccidentEvent(id=event_id)
    in_memory_uow._store.projections[event_id] = ProjectedAccidentRecord(
        event_id=event_id,
        fields={
            "takeoff_time": "2023-06-15T08:30:00",
            "emergency_time": "2023-06-15T08:45:00",
            "accident_time": "2023-06-15T08:47:00",
            "rescue_time": "2023-06-15T09:15:00",
            "investigation_start_date": "2023-06-16",
            "final_report_date": "2024-03-01",
        },
    )
    return event_id
