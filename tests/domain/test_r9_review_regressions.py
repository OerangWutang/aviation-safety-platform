"""Targeted regression tests for issues found in the r9 code review.

Each test is a focused, fast regression guard.  They all run without a
database (fake UoW + config overrides) so they stay in the normal unit-
test suite and catch regressions immediately on every CI run.

Issues addressed:
  1. README Alembic head was stale — now checked dynamically.
  2. Orion find_by_identifier resolved through expired historical
     identifiers (valid_to IS NOT NULL) — now filters active-only.
  3. Migration 034 preflight must fire before any DDL.
  4. Release artifacts (.egg-info) must not appear in the source tree.
  5. Hermes worker must require HERMES_ALLOWED_HOSTS in production.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# Issue 1: README Alembic head must match actual head (dynamic check)
# ---------------------------------------------------------------------------


def test_readme_alembic_head_matches_actual_head() -> None:
    """README declares the current migration head and it must equal the real
    Alembic head computed from revision chain analysis.

    This is the in-process companion to the CI shell check added in r10.
    Running it locally catches the mismatch before pushing.
    """
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    match = re.search(r"current head is `([^`]+)`", readme)
    assert match, (
        "README.md does not declare the migration head in the expected "
        "format.  Add: 'The current head is `<migration_stem>`.' "
        "to the Migrations section."
    )
    declared = match.group(1)

    versions_dir = REPO_ROOT / "alembic/versions"
    revisions: dict[str, str] = {}
    down_revisions: set[str] = set()
    for path in versions_dir.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        rev = re.search(r'^revision\s*=\s*[\'"]([^\'"]+)[\'"]', text, re.M)
        down = re.search(r'^down_revision\s*=\s*[\'"]?([^\'")\n]+)', text, re.M)
        if rev:
            revisions[rev.group(1)] = path.stem
        if down and down.group(1).strip() not in {"None", "null"}:
            down_revisions.add(down.group(1).strip())

    heads = sorted(set(revisions) - down_revisions)
    assert len(heads) == 1, f"Expected exactly one Alembic head, found: {heads}"
    actual = revisions[heads[0]]

    assert declared == actual, (
        f"README.md declares migration head {declared!r} but the actual "
        f"Alembic head is {actual!r}.  Update the README."
    )


# ---------------------------------------------------------------------------
# Issue 2: Orion find_by_identifier must ignore expired (historical) identifiers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_find_by_identifier_ignores_expired_identifier() -> None:
    """An entity with an expired identifier must not be returned.

    Scenario:
    * Entity A had identifier REG=N12345 but its valid_to is now set
      (it transferred the registration).
    * Entity B now holds identifier REG=N12345 (valid_to=None — active).
    * find_by_identifier("N12345") must return entity B, not entity A.

    This was broken before r10: the SQL and fake both matched ANY identifier
    row without filtering on valid_to IS NULL, so the first entity that ever
    held the identifier would be returned indefinitely.
    """
    from datetime import UTC, datetime, timedelta

    from atlas.domain.entities import OrionEntity, OrionEntityIdentifier
    from atlas.domain.enums import OrionEntityType
    from tests.domain._fake_uow import InMemoryUnitOfWork

    uow = InMemoryUnitOfWork()

    entity_a = OrionEntity(
        id=uuid4(),
        entity_type=OrionEntityType.AIRCRAFT,
        canonical_name="Old Aircraft",
        status="ACTIVE",
    )
    entity_b = OrionEntity(
        id=uuid4(),
        entity_type=OrionEntityType.AIRCRAFT,
        canonical_name="New Aircraft",
        status="ACTIVE",
    )
    await uow.orion_entities.add(entity_a)
    await uow.orion_entities.add(entity_b)

    yesterday = datetime.now(UTC) - timedelta(days=1)

    # A's identifier is expired.
    expired_ident = OrionEntityIdentifier(
        id=uuid4(),
        entity_id=entity_a.id,
        entity_type=OrionEntityType.AIRCRAFT,
        identifier_type="REGISTRATION",
        identifier_value="N12345",
        normalized_value="N12345",
        valid_from=datetime.now(UTC) - timedelta(days=365),
        valid_to=yesterday,  # expired
    )
    # B's identifier is active.
    active_ident = OrionEntityIdentifier(
        id=uuid4(),
        entity_id=entity_b.id,
        entity_type=OrionEntityType.AIRCRAFT,
        identifier_type="REGISTRATION",
        identifier_value="N12345",
        normalized_value="N12345",
        valid_from=yesterday,
        valid_to=None,  # active
    )
    await uow.orion_identifiers.add(expired_ident)
    await uow.orion_identifiers.add(active_ident)

    result = await uow.orion_entities.find_by_identifier(
        OrionEntityType.AIRCRAFT, "REGISTRATION", "N12345"
    )
    assert result is not None, (
        "find_by_identifier returned None — expected to find entity B "
        "(the current active holder of the identifier)."
    )
    assert result.id == entity_b.id, (
        f"find_by_identifier returned entity {result.id} but expected "
        f"entity B ({entity_b.id}).  Entity A's identifier is expired "
        "(valid_to IS NOT NULL) and must not match."
    )


@pytest.mark.asyncio
async def test_find_by_identifier_returns_none_when_only_expired_matches() -> None:
    """If only expired identifiers match, return None.

    When a registration was surrendered and the new holder has not yet been
    indexed, the lookup must not ghost-match the old entity.
    """
    from datetime import UTC, datetime, timedelta

    from atlas.domain.entities import OrionEntity, OrionEntityIdentifier
    from atlas.domain.enums import OrionEntityType
    from tests.domain._fake_uow import InMemoryUnitOfWork

    uow = InMemoryUnitOfWork()

    entity = OrionEntity(
        id=uuid4(),
        entity_type=OrionEntityType.AIRCRAFT,
        canonical_name="Old Aircraft",
        status="ACTIVE",
    )
    await uow.orion_entities.add(entity)

    expired = OrionEntityIdentifier(
        id=uuid4(),
        entity_id=entity.id,
        entity_type=OrionEntityType.AIRCRAFT,
        identifier_type="REGISTRATION",
        identifier_value="RETIRED",
        normalized_value="RETIRED",
        valid_from=datetime.now(UTC) - timedelta(days=365),
        valid_to=datetime.now(UTC) - timedelta(days=1),  # expired
    )
    await uow.orion_identifiers.add(expired)

    result = await uow.orion_entities.find_by_identifier(
        OrionEntityType.AIRCRAFT, "REGISTRATION", "RETIRED"
    )
    assert result is None, (
        "find_by_identifier must return None when every matching identifier "
        "is expired — the registration has no current holder."
    )


# ---------------------------------------------------------------------------
# Issue 3: Migration 034 preflight must execute before any DDL
# ---------------------------------------------------------------------------


def test_migration_034_preflight_runs_before_any_ddl() -> None:
    """_check_for_duplicate_active_identifiers() must be the first statement
    in upgrade() — before any op.add_column or op.create_index call.

    The point is that a failing preflight must leave the database in a
    completely clean state (no schema changes applied) so rerunning after
    deduplication is trivially safe.  A preflight that fires after Hermes
    column DDL forces operators to check whether the migration is re-entrant.
    """
    migration_path = REPO_ROOT / "alembic/versions/034_hermes_leases_and_orion_identity_keys.py"
    source = migration_path.read_text(encoding="utf-8")

    # Find the upgrade() function body.
    upgrade_match = re.search(r"^def upgrade\(\) -> None:(.*?)^def ", source, re.S | re.M)
    assert upgrade_match, "Could not locate upgrade() in migration 034."
    upgrade_body = upgrade_match.group(1)

    preflight_pos = upgrade_body.find("_check_for_duplicate_active_identifiers()")
    first_ddl_pos = min(
        (upgrade_body.find(s) for s in ("op.add_column", "op.create_index") if s in upgrade_body),
        default=-1,
    )

    assert preflight_pos >= 0, (
        "_check_for_duplicate_active_identifiers() call not found in upgrade(). "
        "The preflight must run at the top of the upgrade function."
    )
    assert first_ddl_pos >= 0, "No op.add_column or op.create_index found in upgrade()."
    assert preflight_pos < first_ddl_pos, (
        "The duplicate-identifier preflight must run BEFORE any DDL in "
        "upgrade().  Currently it fires after schema changes, which means a "
        "failing preflight leaves the database in a partially-altered state."
    )


def test_migration_034_bypass_env_var_is_present() -> None:
    """The bypass env-var ALEMBIC_034_SKIP_DUPLICATE_PREFLIGHT must exist in
    the migration source so operators can find it via code search when they
    need to skip the preflight after manual deduplication.
    """
    source = (
        REPO_ROOT / "alembic/versions/034_hermes_leases_and_orion_identity_keys.py"
    ).read_text(encoding="utf-8")
    assert "ALEMBIC_034_SKIP_DUPLICATE_PREFLIGHT" in source, (
        "The documented bypass env-var is missing from the migration file."
    )


# ---------------------------------------------------------------------------
# Issue 4: Release source tree must not contain build artifacts
# ---------------------------------------------------------------------------


@pytest.mark.release
def test_no_egg_info_in_source_tree() -> None:
    """No *.egg-info directories should be present in src/ in a clean checkout.

    .gitignore already lists *.egg-info/ but ``pip install -e .`` silently
    creates it every time.  This test is marked ``release`` and is excluded
    from the default pytest run precisely because CI installs with
    ``pip install -e .``, which produces the artifact this test forbids.

    Run only as part of ``make release-check`` against a pre-install clean
    checkout, or in the release CI job that runs *before* the editable install.

    If this fails locally after ``pip install -e .``, run::

        rm -rf src/*.egg-info

    then re-run the tests.
    """
    egg_info_dirs = list((REPO_ROOT / "src").glob("*.egg-info"))
    assert not egg_info_dirs, (
        f"Found build artifact(s) in src/: {egg_info_dirs}. Remove them with: rm -rf src/*.egg-info"
    )


@pytest.mark.release
def test_gitignore_excludes_build_artifacts() -> None:
    """The .gitignore must list build artifacts so they cannot be committed."""
    gitignore = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "*.egg-info/" in gitignore, ".gitignore must exclude *.egg-info/"
    assert "__pycache__/" in gitignore, ".gitignore must exclude __pycache__/"
    assert "*.py[cod]" in gitignore or "*.pyc" in gitignore, ".gitignore must exclude .pyc files"


@pytest.mark.release
def test_dockerignore_excludes_build_artifacts() -> None:
    """The .dockerignore must exclude build artifacts from Docker images."""
    dockerignore = (REPO_ROOT / ".dockerignore").read_text(encoding="utf-8")
    assert "*.egg-info/" in dockerignore, ".dockerignore must exclude *.egg-info/"
    assert "__pycache__/" in dockerignore
    assert "*.py[cod]" in dockerignore or "*.pyc" in dockerignore


# ---------------------------------------------------------------------------
# Issue 5: Hermes worker must require HERMES_ALLOWED_HOSTS in production
# ---------------------------------------------------------------------------


def _make_settings(**overrides: Any):
    """Construct a Settings object with overrides for unit testing."""
    from atlas.config import Settings

    base = {
        "database_url": "postgresql+asyncpg://u:p@localhost/db",
        "environment": "production",
        "api_key_hash_secret": "a" * 64,
        "allowed_hosts": ["api.example.com"],
        # _validate_production_db_roles requires distinct TENANT/SYSTEM URLs
        # in production; supply them so tests reach their intended assertion.
        "tenant_database_url": "postgresql+asyncpg://atlas_app:p@localhost/db",
        "system_database_url": "postgresql+asyncpg://atlas_system:p@localhost/db",
        **overrides,
    }
    return Settings(**base)


def test_hermes_worker_requires_allowed_hosts_in_production() -> None:
    """validate_hermes_worker_settings() must raise when HERMES_ALLOWED_HOSTS
    is empty in production.

    Relying only on IP-range deny-list SSRF protection is insufficient in
    production: a DNS-rebinding attack can bypass it by returning a public
    IP during the pre-connect check and a private IP when the connection
    actually opens.  An explicit allowlist makes the attacker also control
    a hostname Atlas has explicitly trusted.
    """
    settings = _make_settings(hermes_allowed_hosts=[])
    with pytest.raises(RuntimeError, match="HERMES_ALLOWED_HOSTS"):
        settings.validate_hermes_worker_settings()


def test_hermes_worker_accepts_configured_allowed_hosts_in_production() -> None:
    """validate_hermes_worker_settings() must not raise when HERMES_ALLOWED_HOSTS
    is non-empty in production.
    """
    settings = _make_settings(hermes_allowed_hosts=["aviation-safety.net", "ntsb.gov"])
    # Must not raise.
    settings.validate_hermes_worker_settings()


def test_hermes_worker_does_not_require_allowed_hosts_in_development() -> None:
    """The allowlist requirement is production-only.

    Development deployments use a single known local target and relying on
    the IP-range deny rules is fine.  Requiring the allowlist in development
    would break the default dev workflow.
    """
    from atlas.config import Settings

    settings = Settings(
        database_url="postgresql+asyncpg://u:p@localhost/db",
        environment="development",
        hermes_allowed_hosts=[],
    )
    # Must not raise — no allowlist requirement outside production.
    settings.validate_hermes_worker_settings()


def test_hermes_settings_parsed_from_env(monkeypatch) -> None:
    """HERMES_ALLOWED_HOSTS is correctly parsed from a comma-separated string."""
    from atlas.config import get_settings

    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")
    monkeypatch.setenv("HERMES_ALLOWED_HOSTS", "example.com, ntsb.gov, .asn.flightsafety.org")
    get_settings.cache_clear()  # type: ignore[attr-defined]
    try:
        settings = get_settings()
        assert settings.hermes_allowed_hosts == [
            "example.com",
            "ntsb.gov",
            ".asn.flightsafety.org",
        ]
    finally:
        get_settings.cache_clear()  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# r11 regressions: captured_at type preservation and snapshot-race replay
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_captured_at_datetime_survives_validate_and_hash() -> None:
    """captured_at must flow through _validate_and_hash as a datetime, not a str.

    r10 regression: _validate_and_hash stored captured_at as str(captured_at)
    inside the hashes dict.  _load_and_normalise then passed that string to
    _ingestion_submission_fingerprint, which called .isoformat() on it and
    crashed with::

        AttributeError: 'str' object has no attribute 'isoformat'

    The fix stores captured_at as the original datetime so downstream callers
    receive the correct type.
    """
    from datetime import UTC, datetime

    from atlas.application.dto import IngestionClaimDTO
    from atlas.application.use_cases.ingest_source_data import IngestSourceData
    from atlas.domain.entities import Source
    from atlas.domain.enums import SourceKind
    from tests.domain._fake_uow import InMemoryUnitOfWork, make_settings

    uow = InMemoryUnitOfWork()
    src = Source(name="Test", kind=SourceKind.EXTERNAL, reliability_tier=1)
    await uow.sources.add(src)

    captured = datetime(2024, 6, 15, 12, 30, 0, tzinfo=UTC)

    # Must not raise AttributeError; would crash in r10 whenever captured_at
    # was non-None.
    result = await IngestSourceData(uow, settings=make_settings()).execute_with_result(
        source_id=src.id,
        raw_payload={"id": "evt-001"},
        ingestion_run_id=uuid4(),
        claims_data=[IngestionClaimDTO(field_name="event_date", field_value="2024-06-15")],
        captured_at=captured,
    )
    assert result.event_id is not None

    # The stored snapshot should carry the original datetime, not a str repr.
    snapshot = next(iter(uow.store.snapshots.values()))
    assert snapshot.captured_at == captured


@pytest.mark.asyncio
async def test_snapshot_race_replay_returns_ingestion_result_not_crash() -> None:
    """_insert_snapshot returning an IngestionResult must be handled by the caller.

    r10 regression: when _insert_snapshot detected an already-completed snapshot
    via the snapshot-race guard, it returned an IngestionResult directly.  The
    caller then accessed ``.id`` on it (expecting a RawSnapshot), crashing with::

        AttributeError: 'IngestionResult' object has no attribute 'id'

    The fix adds an isinstance guard immediately after _insert_snapshot so an
    IngestionResult replay is returned early rather than used as a snapshot.
    """

    from atlas.application.dto import IngestionClaimDTO, IngestionResult
    from atlas.application.use_cases.ingest_source_data import (
        IngestSourceData,
    )
    from atlas.domain.entities import RawSnapshot, Source
    from atlas.domain.enums import SourceKind
    from tests.domain._fake_uow import InMemoryUnitOfWork, make_settings

    uow = InMemoryUnitOfWork()
    src = Source(name="RaceTest", kind=SourceKind.EXTERNAL, reliability_tier=1)
    await uow.sources.add(src)

    run_id = uuid4()
    payload = {"id": "race-evt"}
    claims = [IngestionClaimDTO(field_name="event_date", field_value="2024-01-01")]

    # --- First ingestion: complete normally to get a real snapshot + result. ---
    first = await IngestSourceData(uow, settings=make_settings()).execute_with_result(
        source_id=src.id,
        raw_payload=payload,
        ingestion_run_id=run_id,
        claims_data=claims,
    )
    assert first.event_id is not None

    # --- Simulate the race: monkeypatch try_add_unique to always return False,
    #     so _insert_snapshot falls into the "concurrent insert" branch.  The
    #     snapshot already has ingestion_result_json written by the first run,
    #     so replay_from_snapshot should return the stored IngestionResult.
    original_try_add_unique = uow.snapshots.try_add_unique

    async def always_collision(snapshot: RawSnapshot) -> bool:
        return False  # pretend a concurrent writer won the insert race

    uow.snapshots.try_add_unique = always_collision  # type: ignore[method-assign]
    try:
        # Must not crash with AttributeError — must return the idempotent replay.
        replay = await IngestSourceData(uow, settings=make_settings()).execute_with_result(
            source_id=src.id,
            raw_payload=payload,
            ingestion_run_id=run_id,
            claims_data=claims,
        )
    finally:
        uow.snapshots.try_add_unique = original_try_add_unique  # type: ignore[method-assign]

    assert isinstance(replay, IngestionResult)
    assert replay.event_id == first.event_id
    assert replay.idempotent_replay is True
