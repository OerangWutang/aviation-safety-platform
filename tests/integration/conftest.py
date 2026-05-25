"""Integration test fixtures.

Tests decorated with @pytest.mark.integration require a running PostgreSQL
instance. In CI, pass --run-integration to enable them; they are skipped by
default so unit tests and API smoke tests always run fast.

Database safety
---------------
Integration tests TRUNCATE the entire schema. To prevent accidentally wiping a
developer's working database when ``DATABASE_URL`` happens to be exported, the
fixtures here:

  1. Use ``TEST_DATABASE_URL`` exclusively. ``DATABASE_URL`` is ignored.
  2. Refuse to run if the URL's database name does not contain ``test``.
  3. Require the explicit opt-in env var ``ATLAS_ALLOW_DB_TRUNCATE=1`` as a
     final belt-and-braces gate.

These guards are conservative on purpose: a TRUNCATE against a populated
database is unrecoverable, so making the failure mode "test refuses to run"
rather than "data is gone" is the correct trade.
"""

from __future__ import annotations

import os
import re

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

TRUNCATE_SQL = """
TRUNCATE TABLE
    api_keys,
    archive_manifests,
    accident_projection_history,
    projected_accident_records,
    conflict_activity_log,
    claim_conflict_claims,
    claim_conflicts,
    claim_history,
    claims,
    raw_snapshots,
    ingestion_runs,
    usage_daily_rollups,
    usage_events,
    nl_query_log,
    saved_nl_queries,
    event_hfacs_attributions,
    shelo_factor_interactions,
    shelo_factors,
    tenant_event_associations,
    tenant_safety_reports,
    tenant_claims,
    tenant_ingestion_runs,
    accident_events,
    pending_duplicate_reviews,
    event_identity_index,
    outbox_worker_heartbeats,
    outbox_events,
    -- Orion / Chronos / Hermes / Argus tables.  CASCADE on the parents takes
    -- care of FK dependencies, but listing them explicitly is more readable
    -- and survives schema changes that drop a parent FK.
    orion_entity_reviews,
    orion_entity_claim_links,
    orion_relationships,
    orion_entity_identifiers,
    orion_entities,
    chronos_event_links,
    chronos_sequence_reviews,
    chronos_timeline_events,
    hermes_source_changes,
    hermes_fetched_documents,
    hermes_fetch_jobs,
    hermes_crawl_targets,
    hermes_sources,
    argus_signal_reviews,
    argus_signal_evidence,
    argus_signals,
    sources
RESTART IDENTITY CASCADE
"""

SEED_CURATOR_OVERRIDE_SQL = """
INSERT INTO sources (id, name, kind, reliability_tier, created_at)
VALUES ('00000000-0000-0000-0000-000000000001', 'CuratorOverride', 'INTERNAL', 1, now())
ON CONFLICT (id) DO NOTHING
"""


# pytest_addoption is defined in tests/conftest.py (root level) so that
# ``pytest --run-integration`` works from the repo root.  Defining it here
# would register it too late and cause "unrecognized arguments" errors.


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "integration: marks tests that require a live PostgreSQL connection"
    )


def pytest_collection_modifyitems(config, items):
    if config.getoption("--run-integration", default=False):
        return
    skip = pytest.mark.skip(reason="Pass --run-integration to run these tests")
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip)


def _extract_db_name(url: str) -> str:
    """Pull the database name off the end of a SQLAlchemy URL.

    Cheap and dialect-agnostic: strips a trailing ``?query`` if present,
    then takes the segment after the last ``/``. Tolerates extra slashes
    on ``+asyncpg`` URLs.
    """
    no_query = url.split("?", 1)[0]
    return no_query.rsplit("/", 1)[-1]


def _ensure_safe_to_truncate(url: str) -> None:
    """Raise unless this URL is unambiguously a test database.

    Checks (any failure aborts):
      - database name contains ``test`` (case-insensitive),
      - ``ATLAS_ALLOW_DB_TRUNCATE=1`` is set in the environment.

    The two-key requirement makes "I exported the wrong env var" non-fatal:
    a misconfigured URL alone won't trigger TRUNCATE, and the explicit
    opt-in won't hurt if the URL is correct.
    """
    db_name = _extract_db_name(url)
    if not re.search(r"test", db_name, re.IGNORECASE):
        pytest.fail(
            f"Integration tests refuse to run against '{db_name}': database name "
            f"must contain 'test'. Set TEST_DATABASE_URL to a dedicated test DB. "
            f"This guard exists because the fixtures TRUNCATE every table on entry "
            f"and exit, which is unrecoverable."
        )
    if os.getenv("ATLAS_ALLOW_DB_TRUNCATE") != "1":
        pytest.fail(
            "Integration tests refuse to run without ATLAS_ALLOW_DB_TRUNCATE=1. "
            "Set it explicitly to opt in to TRUNCATE on the test database. "
            "(CI sets this; local runs must opt in.)"
        )


async def _reset_db(session) -> None:
    await session.execute(text(TRUNCATE_SQL))
    await session.execute(text(SEED_CURATOR_OVERRIDE_SQL))
    await session.commit()


@pytest.fixture(scope="session")
def db_url() -> str:
    """The URL the integration tests will connect to.

    Note: ``DATABASE_URL`` is NOT consulted on purpose - see the module
    docstring. Override via ``TEST_DATABASE_URL`` only.
    """
    url = os.getenv(
        "TEST_DATABASE_URL",
        "postgresql+asyncpg://atlas:atlas@localhost:5432/atlas_test",
    )
    _ensure_safe_to_truncate(url)
    return url


@pytest_asyncio.fixture
async def test_engine(db_url):
    """Engine bound to TEST_DATABASE_URL - never to the production URL.

    A dedicated engine (separate from ``atlas.infrastructure.db.session``'s
    cached one) ensures we cannot accidentally run TRUNCATE against the
    application's configured database.

    Function-scoped on purpose: asyncpg connections are bound to the
    event loop that created them, and ``pytest-asyncio`` (auto mode,
    function loop scope) spins up a fresh loop per test.  A
    session-scoped engine would strand its pooled connection on the
    first test's loop and raise ``InterfaceError`` on the second
    test's ``_reset_db``.  A fresh engine per test — disposed in the
    finally — keeps every connection on its own loop.  Engine creation
    against a local socket is cheap, so the cost is negligible.
    """
    engine = create_async_engine(db_url, echo=False, future=True)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def test_session_factory(test_engine):
    return async_sessionmaker(test_engine, expire_on_commit=False)


@pytest_asyncio.fixture
async def pg_uow(test_session_factory):
    from atlas.infrastructure.db.unit_of_work import SqlAlchemyUnitOfWork

    async with test_session_factory() as session:
        await _reset_db(session)
        uow = SqlAlchemyUnitOfWork(session)
        try:
            yield uow
        finally:
            await _reset_db(session)
