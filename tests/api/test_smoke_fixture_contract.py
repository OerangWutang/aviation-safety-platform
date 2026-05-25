"""Regression: the smoke-test fixtures must only reference symbols that exist.

Background
----------
A prior refactor renamed the request-DB-session dependency to ``get_uow`` but
left ``conftest.py`` overriding ``deps.get_session``. The fixture crashed at
setup with ``AttributeError``, which silently disabled all 20 API smoke tests
on every CI run until someone read the error log carefully.

This test pins the exact names ``conftest.py`` reaches into, so the same class
of bug cannot recur silently — a future rename of any of these symbols will
fail this one targeted test instead of disabling the whole smoke suite.
"""

from __future__ import annotations


def test_dependencies_module_exposes_symbols_used_by_smoke_fixtures() -> None:
    """conftest.py reaches into deps.async_session_factory; pin that contract."""
    import atlas.presentation.api.dependencies as deps

    # Patched by conftest._fake_session_factory.
    assert hasattr(deps, "async_session_factory"), (
        "atlas.presentation.api.dependencies must expose `async_session_factory` "
        "so the smoke-test conftest can monkey-patch it. Removing or renaming "
        "this symbol disables all 20 API smoke tests until someone notices."
    )
    # Used by routes; if this disappears the dependency overrides path breaks.
    assert hasattr(deps, "get_uow"), (
        "atlas.presentation.api.dependencies must expose `get_uow` — every "
        "router depends on it for request-scoped UnitOfWork management."
    )


def test_session_module_exposes_factory_used_by_smoke_fixtures() -> None:
    """conftest.py also reaches into db.session.async_session_factory."""
    import atlas.infrastructure.db.session as db_session

    assert hasattr(db_session, "async_session_factory"), (
        "atlas.infrastructure.db.session must expose `async_session_factory` "
        "so the smoke-test conftest can monkey-patch it on both modules. "
        "(The dependencies module imports it FROM the session module; both "
        "must be patched because Python's `from X import Y` rebinds the name.)"
    )


def test_app_module_exposes_lifespan_assertion_hook(monkeypatch) -> None:
    """conftest.py monkey-patches assert_curator_override_source to a noop."""
    # Importing atlas.presentation.api.app triggers app = create_app() at
    # module load, which calls Settings() — provide the same env that the
    # smoke-test fixture provides so the import doesn't fail on missing
    # DATABASE_URL.
    from tests.api.conftest import REQUIRED_ENV

    for key, value in REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)

    import atlas.config as config

    config.get_settings.cache_clear()
    import atlas.presentation.api.app as app_module

    assert hasattr(app_module, "assert_curator_override_source"), (
        "atlas.presentation.api.app must expose `assert_curator_override_source` "
        "so smoke tests can skip the CuratorOverride seed-row check without a DB."
    )
    assert hasattr(app_module, "create_app"), (
        "atlas.presentation.api.app must expose `create_app` — smoke tests build "
        "fresh app instances per fixture invocation."
    )
