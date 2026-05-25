"""Session-scoped shared FastAPI app for domain tests that exercise HTTP routes.

``tests/domain/test_api_auth.py``, ``test_hardening_pass.py``, and
``test_p1_correctness_fixes.py`` each call ``create_app()`` once per test
(~190 ms/call).  This conftest creates the app once at session scope and
exposes it as ``_DOMAIN_SHARED_APP``, cutting those ~83 tests from ~11 s to
under 1 s.

Design notes
------------
* ``_DOMAIN_SHARED_APP`` is a module-level variable set by the session fixture
  so plain helper functions (``_authed``, ``_authed_with_uow``) can read it
  without requiring a fixture parameter.
* User identity is injected via httpx ``event_hooks`` (same technique as
  ``tests/api/conftest.py``) so two clients with different roles can coexist
  in the same test body without the second setup call overwriting the first.
* ``dependency_overrides`` is cleared after every domain test that touches the
  shared app, via the ``_clear_domain_app_overrides`` autouse fixture.
* Settings cache is cleared after every test so domain tests that temporarily
  monkeypatch env vars don't leave stale settings for later tests.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager

import pytest

# ── Env setup at import time ──────────────────────────────────────────────────
# Set before ``atlas.presentation.api.app`` is first imported (its module-level
# ``app = create_app()`` fires on first import and needs DATABASE_URL).

_DOMAIN_ENV = {
    "DATABASE_URL": "postgresql+asyncpg://user:pass@localhost:5432/atlas",
    "DATABASE_SYNC_URL": "postgresql://user:pass@localhost:5432/atlas",
    "POSTGRES_USER": "user",
    "POSTGRES_PASSWORD": "pass",
    "POSTGRES_DB": "atlas",
}
for _k, _v in _DOMAIN_ENV.items():
    os.environ.setdefault(_k, _v)
del _k, _v  # don't pollute module namespace

# Module-level handle; set by ``_domain_shared_app`` session fixture.
_DOMAIN_SHARED_APP = None


# ── Session-level setup ───────────────────────────────────────────────────────


@pytest.fixture(scope="session", autouse=True)
def _domain_api_session_env():
    """Patch DB session factory and startup hook once for all domain API tests."""
    import atlas.config as cfg
    import atlas.infrastructure.db.session as db_session
    import atlas.presentation.api.app as app_module
    import atlas.presentation.api.dependencies as deps

    cfg.get_settings.cache_clear()

    class _NoDbSession:
        async def execute(self, _stmt):
            return type("R", (), {"scalar_one_or_none": lambda s: None})()

        async def rollback(self) -> None:
            pass

        async def commit(self) -> None:
            pass

        def in_transaction(self) -> bool:
            return False

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
def _domain_shared_app(_domain_api_session_env):
    """Create the domain test FastAPI app once per session.

    ``autouse=True`` guarantees this is ready before the first test that
    calls ``make_domain_app()``, which reads ``_DOMAIN_SHARED_APP``.
    """
    global _DOMAIN_SHARED_APP
    import atlas.presentation.api.app as app_module

    _DOMAIN_SHARED_APP = app_module.create_app()
    return _DOMAIN_SHARED_APP


# ── Per-test cleanup ─────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _clear_domain_app_overrides(_domain_shared_app):
    """Clear dependency_overrides on the shared domain app after each test.

    Most domain tests don't touch the app at all; for those this is a
    microsecond no-op.  For tests in the three slow files it prevents
    one test's user/UoW overrides from leaking into the next.
    """
    yield
    if _domain_shared_app is not None:
        _domain_shared_app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def _clear_settings_cache_after_domain_test():
    """Re-read Settings after each domain test.

    Some domain tests use monkeypatch to temporarily set production-style
    env vars and call ``get_settings.cache_clear()``.  Without this fixture
    the stale production Settings object stays cached until the next explicit
    clear, which can make later tests behave unexpectedly.
    """
    yield
    import atlas.config as cfg

    cfg.get_settings.cache_clear()
