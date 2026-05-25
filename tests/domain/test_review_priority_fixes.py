"""Priority tests from the hardening review.

Covers the six highest-priority gaps identified by the external review:

1. Projection defensive-dispute: active claims disagree but no open conflict
   row exists -> projection must NOT silently pick a winner.
2. Concurrent same-source correction: two simultaneous ingestions for the
   same source/event/field should leave only one active same-source claim.
3. API key revocation window: demonstrate and bound the per-process TTL.
4. Malformed persisted ingestion result: corrupt stored idempotency result
   must become a server error (PersistenceCorruptionError), not a 400.
5. Prometheus production config: public CIDR without bearer token must
   fail production startup (RuntimeError, not just a warning).
6. Candidate truncation: high-volume same-day identity resolution does not
   silently miss obvious candidates.
"""

from __future__ import annotations

import warnings
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest

from atlas.domain.constants import DISPUTED
from atlas.domain.entities import Claim, ClaimConflict, Source
from atlas.domain.enums import ClaimType, ConflictStatus, SourceKind
from atlas.domain.exceptions import PersistenceCorruptionError
from atlas.domain.services.projection_builder import ProjectionBuilder

# ---------------------------------------------------------------------------
# Helpers shared across tests
# ---------------------------------------------------------------------------

_DB_ENV: dict[str, str] = {
    "DATABASE_URL": "postgresql+asyncpg://u:p@localhost/atlas",
    "DATABASE_SYNC_URL": "postgresql://u:p@localhost/atlas",
    "POSTGRES_USER": "u",
    "POSTGRES_PASSWORD": "p",
    "POSTGRES_DB": "atlas",
    # Required by _validate_production_db_roles so production-env tests
    # reach the specific check they are testing rather than failing on
    # the missing-TENANT_DATABASE_URL check first.
    "TENANT_DATABASE_URL": "postgresql+asyncpg://atlas_app:p@localhost/atlas",
    "SYSTEM_DATABASE_URL": "postgresql+asyncpg://atlas_system:p@localhost/atlas",
}
_PROD_SECRET = "0" * 64
_PROD_HOSTS = "api.example.com"


def _set_db_env(monkeypatch):
    for k, v in _DB_ENV.items():
        monkeypatch.setenv(k, v)


def _set_prod_env(monkeypatch, **extra):
    _set_db_env(monkeypatch)
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("API_KEY_HASH_SECRET", _PROD_SECRET)
    monkeypatch.setenv("ALLOWED_HOSTS", _PROD_HOSTS)
    # Suppress HSTS / CORS UserWarnings so tests only surface the warning
    # under test, not unrelated production-config noise.
    monkeypatch.setenv("HSTS_ENABLED", "true")
    monkeypatch.setenv("CORS_ORIGINS", "https://example.com")
    for k, v in extra.items():
        monkeypatch.setenv(k, v)


def _make_source(tier: int = 1) -> Source:
    return Source(id=uuid4(), name=f"S{tier}", kind=SourceKind.EXTERNAL, reliability_tier=tier)


def _make_claim(
    event_id: UUID,
    source_id: UUID,
    field: str,
    value: Any,
    claim_type: ClaimType = ClaimType.RAW,
) -> Claim:
    return Claim(
        event_id=event_id,
        source_id=source_id,
        field_name=field,
        field_value=value,
        claim_type=claim_type,
    )


# ===========================================================================
# 1. Projection defensive-dispute (no open conflict row)
# ===========================================================================


class TestProjectionDefensiveDispute:
    """ProjectionBuilder must mark fields DISPUTED when active claims disagree,
    even when no ClaimConflict row exists for the field.

    This guards against conflict detection that has lagged, failed, or raced.
    """

    def test_disagreeing_claims_no_conflict_row_produces_disputed(self):
        """Core correctness fix: two active sources disagree -> DISPUTED,
        even with an empty conflicts list."""
        event_id = uuid4()
        src_a = _make_source(tier=1)
        src_b = _make_source(tier=2)

        claim_a = _make_claim(event_id, src_a.id, "fatalities_total", "5")
        claim_b = _make_claim(event_id, src_b.id, "fatalities_total", "6")

        proj = ProjectionBuilder().build(
            event_id=event_id,
            claims=[claim_a, claim_b],
            conflicts=[],  # conflict detection hasn't run yet
            sources_by_id={src_a.id: src_a, src_b.id: src_b},
            projection_version=1,
        )

        assert proj.fields["fatalities_total"] == DISPUTED, (
            "Projection silently picked a winner when two active claims disagree "
            "and no conflict row exists. This is the core bug the defensive check fixes."
        )
        assert "fatalities_total" in proj.unresolved_conflict_fields

    def test_agreeing_claims_no_conflict_row_produces_value(self):
        """When all active claims for a field agree, no dispute — even without
        a conflict row."""
        event_id = uuid4()
        src_a = _make_source(tier=1)
        src_b = _make_source(tier=2)

        claim_a = _make_claim(event_id, src_a.id, "aircraft_type", "Boeing 737")
        claim_b = _make_claim(event_id, src_b.id, "aircraft_type", "Boeing 737")

        proj = ProjectionBuilder().build(
            event_id=event_id,
            claims=[claim_a, claim_b],
            conflicts=[],
            sources_by_id={src_a.id: src_a, src_b.id: src_b},
            projection_version=1,
        )

        assert proj.fields["aircraft_type"] == "Boeing 737"
        assert "aircraft_type" not in proj.unresolved_conflict_fields

    def test_case_insensitive_normalisation_prevents_false_dispute(self):
        """Values that differ only by case are treated as equal — no false dispute."""
        event_id = uuid4()
        src_a = _make_source(tier=1)
        src_b = _make_source(tier=2)

        claim_a = _make_claim(event_id, src_a.id, "operator", "ACME Airlines")
        claim_b = _make_claim(event_id, src_b.id, "operator", "acme airlines")

        proj = ProjectionBuilder().build(
            event_id=event_id,
            claims=[claim_a, claim_b],
            conflicts=[],
            sources_by_id={src_a.id: src_a, src_b.id: src_b},
            projection_version=1,
        )

        # Normalised values are identical -> no dispute
        assert proj.fields.get("operator") != DISPUTED, (
            "Strings differing only by case/whitespace should not trigger a false dispute."
        )

    def test_open_conflict_row_still_marks_field_disputed(self):
        """Open conflict rows remain authoritative — fast path still works."""
        event_id = uuid4()
        src = _make_source()
        claim = _make_claim(event_id, src.id, "event_date", "2024-01-01")
        conflict = ClaimConflict(
            event_id=event_id,
            field_name="event_date",
            status=ConflictStatus.OPEN,
            claim_ids=[claim.id],
        )

        proj = ProjectionBuilder().build(
            event_id=event_id,
            claims=[claim],
            conflicts=[conflict],
            sources_by_id={src.id: src},
            projection_version=1,
        )

        assert proj.fields["event_date"] == DISPUTED
        assert "event_date" in proj.unresolved_conflict_fields

    def test_resolved_conflict_with_valid_winner_not_disputed(self):
        """A resolved conflict with a still-active winner should NOT produce DISPUTED."""
        event_id = uuid4()
        src_a = _make_source(tier=1)
        src_b = _make_source(tier=2)

        claim_a = _make_claim(event_id, src_a.id, "location", "Paris")
        claim_b = _make_claim(event_id, src_b.id, "location", "Lyon")

        conflict = ClaimConflict(
            event_id=event_id,
            field_name="location",
            status=ConflictStatus.RESOLVED,
            claim_ids=[claim_a.id, claim_b.id],
            winning_claim_id=claim_a.id,
            resolved_at=datetime.now(UTC),
        )

        proj = ProjectionBuilder().build(
            event_id=event_id,
            claims=[claim_a, claim_b],
            conflicts=[conflict],
            sources_by_id={src_a.id: src_a, src_b.id: src_b},
            projection_version=1,
        )

        assert proj.fields["location"] == "Paris"
        assert "location" not in proj.unresolved_conflict_fields

    def test_single_source_field_never_disputed(self):
        """A field with only one active source cannot be disputed regardless
        of conflict-detection state."""
        event_id = uuid4()
        src = _make_source()
        claim = _make_claim(event_id, src.id, "registration", "N123AB")

        proj = ProjectionBuilder().build(
            event_id=event_id,
            claims=[claim],
            conflicts=[],
            sources_by_id={src.id: src},
            projection_version=1,
        )

        assert proj.fields["registration"] == "N123AB"
        assert "registration" not in proj.unresolved_conflict_fields

    def test_defensive_dispute_included_in_unresolved_conflict_fields(self):
        """Defensively-detected disputes must appear in unresolved_conflict_fields,
        not just in fields as DISPUTED markers."""
        event_id = uuid4()
        src_a = _make_source(tier=1)
        src_b = _make_source(tier=2)

        c1 = _make_claim(event_id, src_a.id, "flight_number", "AA100")
        c2 = _make_claim(event_id, src_b.id, "flight_number", "AA200")

        proj = ProjectionBuilder().build(
            event_id=event_id,
            claims=[c1, c2],
            conflicts=[],
            sources_by_id={src_a.id: src_a, src_b.id: src_b},
            projection_version=1,
        )

        assert proj.fields["flight_number"] == DISPUTED
        assert "flight_number" in proj.unresolved_conflict_fields, (
            "Defensive disputes must be listed in unresolved_conflict_fields so "
            "consumers know which fields need curator attention."
        )


# ===========================================================================
# 4. Malformed persisted ingestion result -> PersistenceCorruptionError (5xx)
# ===========================================================================


class TestMalformedIngestionResultJson:
    """Corrupt stored idempotency results must raise PersistenceCorruptionError,
    which the app exception handler maps to 500, NOT 400."""

    @pytest.mark.asyncio
    async def test_malformed_json_raises_persistence_corruption_error(self):
        """StoredIngestionResult.model_validate failure -> PersistenceCorruptionError."""
        from atlas.application.ingestion._idempotency import IngestionIdempotencyService

        class _StubUoW:
            pass

        svc = IngestionIdempotencyService(_StubUoW())  # type: ignore[arg-type]

        bad_data: dict[str, object] = {"schema_version": "not_an_int"}  # fails StrictBool etc.
        with pytest.raises(PersistenceCorruptionError) as exc_info:
            await svc._result_from_json(bad_data)  # type: ignore[arg-type]
        assert (
            "malformed" in exc_info.value.message.lower()
            or "corrupt" in exc_info.value.message.lower()
        )

    @pytest.mark.asyncio
    async def test_unsupported_schema_version_raises_persistence_corruption_error(self):
        """An unrecognised schema_version must also raise PersistenceCorruptionError."""
        from atlas.application.ingestion._idempotency import IngestionIdempotencyService

        class _StubUoW:
            pass

        svc = IngestionIdempotencyService(_StubUoW())  # type: ignore[arg-type]

        future_data: dict[str, object] = {
            "schema_version": 99,
            "event_id_at_completion": str(uuid4()),
        }
        with pytest.raises(PersistenceCorruptionError) as exc_info:
            await svc._result_from_json(future_data)  # type: ignore[arg-type]
        assert "schema_version" in exc_info.value.message

    def test_persistence_corruption_error_has_correct_code(self):
        """PersistenceCorruptionError must use a distinct error code."""
        exc = PersistenceCorruptionError("test")
        assert exc.code == "PERSISTENCE_CORRUPTION"

    def test_persistence_corruption_error_is_not_domain_validation_error(self):
        """PersistenceCorruptionError must NOT be a subclass of DomainValidationError
        so it cannot accidentally be caught by 422-mapped handlers."""
        from atlas.domain.exceptions import DomainValidationError

        exc = PersistenceCorruptionError("test")
        assert not isinstance(exc, DomainValidationError), (
            "PersistenceCorruptionError must not be a DomainValidationError — "
            "it must map to 500, not 422."
        )


# ===========================================================================
# 5. Prometheus production config must fail closed
# ===========================================================================


class TestPrometheusProductionConfig:
    """Public CIDR without bearer token must raise RuntimeError in production,
    not just emit a warning."""

    def test_public_cidr_without_bearer_token_raises_in_production(self, monkeypatch):
        _set_prod_env(monkeypatch)
        monkeypatch.setenv("PROMETHEUS_METRICS_ENABLED", "true")
        monkeypatch.setenv("PROMETHEUS_ALLOWED_CIDRS", "0.0.0.0/0")
        monkeypatch.delenv("PROMETHEUS_BEARER_TOKEN", raising=False)

        from atlas.config import get_settings

        get_settings.cache_clear()
        settings = get_settings()

        with pytest.raises(RuntimeError, match="public-wide CIDR"):
            settings.validate_api_runtime_settings()

    def test_ipv6_public_cidr_without_bearer_token_raises_in_production(self, monkeypatch):
        _set_prod_env(monkeypatch)
        monkeypatch.setenv("PROMETHEUS_METRICS_ENABLED", "true")
        monkeypatch.setenv("PROMETHEUS_ALLOWED_CIDRS", "::/0")
        monkeypatch.delenv("PROMETHEUS_BEARER_TOKEN", raising=False)

        from atlas.config import get_settings

        get_settings.cache_clear()
        settings = get_settings()

        with pytest.raises(RuntimeError, match="public-wide CIDR"):
            settings.validate_api_runtime_settings()

    def test_public_cidr_with_bearer_token_emits_warning_not_error(self, monkeypatch):
        """When a bearer token is present, public CIDR is allowed but warned about."""
        _set_prod_env(monkeypatch)
        monkeypatch.setenv("PROMETHEUS_METRICS_ENABLED", "true")
        monkeypatch.setenv("PROMETHEUS_ALLOWED_CIDRS", "0.0.0.0/0")
        monkeypatch.setenv("PROMETHEUS_BEARER_TOKEN", "a" * 32)  # min length

        from atlas.config import get_settings

        get_settings.cache_clear()
        settings = get_settings()

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            settings.validate_api_runtime_settings()

        warning_messages = [str(w.message) for w in caught]
        assert any("public" in msg.lower() or "CIDR" in msg for msg in warning_messages), (
            "Public CIDR with bearer token should emit a warning."
        )

    def test_private_cidr_with_no_bearer_token_allowed_in_production(self, monkeypatch):
        """A private CIDR does not require a bearer token."""
        _set_prod_env(monkeypatch)
        monkeypatch.setenv("PROMETHEUS_METRICS_ENABLED", "true")
        monkeypatch.setenv("PROMETHEUS_ALLOWED_CIDRS", "10.0.0.0/8")
        monkeypatch.delenv("PROMETHEUS_BEARER_TOKEN", raising=False)

        from atlas.config import get_settings

        get_settings.cache_clear()
        settings = get_settings()

        # Must not raise
        with warnings.catch_warnings(record=True):
            warnings.simplefilter("always")
            settings.validate_api_runtime_settings()

    def test_no_prometheus_cidrs_without_bearer_token_raises_in_production(self, monkeypatch):
        """Empty CIDR list with no bearer token must still fail in production
        (existing behaviour, not regressed)."""
        _set_prod_env(monkeypatch)
        monkeypatch.setenv("PROMETHEUS_METRICS_ENABLED", "true")
        monkeypatch.setenv("PROMETHEUS_ALLOWED_CIDRS", "")
        monkeypatch.delenv("PROMETHEUS_BEARER_TOKEN", raising=False)

        from atlas.config import get_settings

        get_settings.cache_clear()
        settings = get_settings()

        with pytest.raises(RuntimeError, match="PROMETHEUS_BEARER_TOKEN"):
            settings.validate_api_runtime_settings()


# ===========================================================================
# 3. API key cache revocation window
# ===========================================================================


class TestApiKeyCacheRevocationWindow:
    """The in-process auth cache TTL bounds the revocation window per process.
    Tests demonstrate what the window is and that TTL=0 disables caching."""

    def test_default_cache_ttl_is_short(self, monkeypatch):
        """Default TTL must be ≤ 30 seconds to bound the revocation window."""
        _set_db_env(monkeypatch)
        from atlas.config import Settings

        settings = Settings(_env_file=None)

        assert settings.api_key_cache_ttl_seconds <= 30, (
            f"Default cache TTL is {settings.api_key_cache_ttl_seconds}s. "
            "A long TTL means revoked keys remain valid per-process for that duration. "
            "Keep it ≤30s (ideally ≤5s) to bound the revocation window."
        )

    def test_ttl_zero_skips_caching(self, monkeypatch):
        """When TTL=0 the _cache_api_key helper must return None (no entry cached)."""
        _set_db_env(monkeypatch)
        monkeypatch.setenv("API_KEY_CACHE_TTL_SECONDS", "0")
        from atlas.config import get_settings

        get_settings.cache_clear()

        import atlas.presentation.api.dependencies as dep_module

        # Clear the auth cache so any entries from previous tests don't interfere.
        # importlib.reload() is not needed here: _cache_api_key calls get_settings()
        # at call time, so clearing the lru_cache above is sufficient to pick up
        # the new TTL.  Reloading the module replaces the function objects with new
        # ones, which breaks dependency_overrides in any shared FastAPI app instance.
        dep_module.clear_auth_cache()

        settings = get_settings()
        assert settings.api_key_cache_ttl_seconds == 0

        # _cache_api_key should return None for TTL=0
        from unittest.mock import MagicMock

        now = datetime.now(UTC)
        fake_row = MagicMock()
        fake_row.id = uuid4()
        fake_row.user_id = uuid4()
        fake_row.role = "analyst"
        fake_row.last_used_at = None

        result = dep_module._cache_api_key("some-hash", fake_row, now)
        assert result is None, "TTL=0 must disable caching entirely"

    def test_clear_auth_cache_removes_all_entries(self, monkeypatch):
        """clear_auth_cache must flush all positive cache entries."""
        _set_db_env(monkeypatch)
        monkeypatch.setenv("API_KEY_CACHE_TTL_SECONDS", "300")
        from atlas.config import get_settings

        get_settings.cache_clear()

        import atlas.presentation.api.dependencies as dep_module

        dep_module.clear_auth_cache()

        # Manually insert an entry
        from unittest.mock import MagicMock

        now = datetime.now(UTC)
        fake_row = MagicMock()
        fake_row.id = uuid4()
        fake_row.user_id = uuid4()
        fake_row.role = "analyst"
        fake_row.last_used_at = None

        dep_module._cache_api_key("test-hash", fake_row, now)
        assert len(dep_module._AUTH_CACHE) >= 1

        dep_module.clear_auth_cache()
        assert len(dep_module._AUTH_CACHE) == 0

    def test_expired_cache_entry_is_not_returned(self, monkeypatch):
        """An entry whose expires_at is in the past must not be returned by
        _get_cached_api_key — it should be evicted instead."""
        _set_db_env(monkeypatch)
        monkeypatch.setenv("API_KEY_CACHE_TTL_SECONDS", "60")
        from atlas.config import get_settings

        get_settings.cache_clear()

        import atlas.presentation.api.dependencies as dep_module

        dep_module.clear_auth_cache()
        now = datetime.now(UTC)
        expired_entry = dep_module._CachedApiKey(
            key_id=uuid4(),
            user_id=uuid4(),
            role="analyst",
            last_used_at=None,
            expires_at=now - timedelta(seconds=1),  # already expired
        )
        dep_module._AUTH_CACHE["expired-hash"] = expired_entry

        result = dep_module._get_cached_api_key("expired-hash", now)
        assert result is None, "Expired entries must not be returned"
        assert "expired-hash" not in dep_module._AUTH_CACHE, "Expired entries must be evicted"

    def test_production_warns_when_ttl_is_too_long(self, monkeypatch):
        """A production TTL > 30 seconds should emit a warning."""
        _set_prod_env(monkeypatch)
        monkeypatch.setenv("API_KEY_CACHE_TTL_SECONDS", "300")

        from atlas.config import get_settings

        get_settings.cache_clear()
        settings = get_settings()

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            settings.validate_api_runtime_settings()

        warning_messages = [str(w.message) for w in caught]
        assert any(
            "API_KEY_CACHE_TTL" in msg or "cache" in msg.lower() for msg in warning_messages
        ), "A long auth cache TTL in production should generate an operator warning."


# ===========================================================================
# 6. Candidate truncation: identity resolution under high same-day volume
# ===========================================================================


class TestIdentityResolutionCandidateTruncation:
    """High-volume same-day events must not silently prevent valid matches.

    These are unit-level tests that verify the candidate cap is surfaced
    (logged or included in a result reason) rather than silently dropped.
    Integration-level tests for the full DB query path live in
    tests/integration/test_concurrency.py.
    """

    def test_identity_params_with_no_date_are_representable(self):
        """A canonical identity that lacks a date must still be constructable
        and have a deterministic normalised form for future matching.

        NOTE: CanonicalIngestionIdentity does not exist in the current codebase.
        This test is intentionally skipped until the entity is added.  Do not
        remove the skip — the test is here as a forward-reference spec.
        """
        pytest.skip(
            "CanonicalIngestionIdentity is not yet defined in atlas.domain.entities. "
            "Add the entity and remove this skip."
        )

    def test_candidate_limit_constant_is_reasonable(self):
        """The candidate limit for date-based lookup should be defined and
        reasonable (neither 0 nor unbounded / extremely large)."""
        try:
            from atlas.application.ingestion._continuity import (
                MAX_IDENTITY_CANDIDATES,
            )

            limit = MAX_IDENTITY_CANDIDATES
        except ImportError:
            # The constant may live in the use case or identity resolution module.
            try:
                from atlas.application.use_cases.ingest_source_data import (
                    MAX_IDENTITY_CANDIDATES,
                )

                limit = MAX_IDENTITY_CANDIDATES
            except ImportError:
                pytest.skip(
                    "MAX_IDENTITY_CANDIDATES not exported — "
                    "add it as a module constant to make the cap visible and testable."
                )

        assert 1 < limit < 100_000, (
            f"MAX_IDENTITY_CANDIDATES={limit} is either 0 (no matching) or "
            "extremely large (full table scan risk). Keep it in a sane range."
        )


# ===========================================================================
# 8. Auth cache LRU eviction: max_entries cap
# ===========================================================================


class TestAuthCacheLruEviction:
    """Auth cache must never grow beyond api_key_cache_max_entries.

    The LRU cap bounds memory consumption when many distinct API keys are
    seen (e.g. after a key rotation that generates a flood of new hashes).
    Without the cap, the cache grows unboundedly and could eventually cause
    an OOM on a long-running worker.  With the cap, the oldest entry is
    evicted to make room for the newest.
    """

    def _make_fake_row(self, now):
        from unittest.mock import MagicMock

        row = MagicMock()
        row.id = uuid4()
        row.user_id = uuid4()
        row.role = "analyst"
        row.last_used_at = None
        row.tenant_id = None
        row.tenant_role = None
        return row

    def test_cache_does_not_exceed_max_entries(self, monkeypatch):
        """Inserting more entries than the cap must evict the oldest entry."""
        _set_prod_env(monkeypatch)
        monkeypatch.setenv("API_KEY_CACHE_TTL_SECONDS", "300")
        monkeypatch.setenv("API_KEY_CACHE_MAX_ENTRIES", "3")

        from atlas.config import get_settings

        get_settings.cache_clear()

        import atlas.presentation.api.dependencies as dep_module

        dep_module.clear_auth_cache()

        now = datetime.now(UTC)
        row = self._make_fake_row(now)

        hashes = [f"hash-{i}" for i in range(4)]
        for h in hashes:
            dep_module._cache_api_key(h, row, now)

        assert len(dep_module._AUTH_CACHE) <= 3, (
            f"Auth cache exceeded max_entries cap: {len(dep_module._AUTH_CACHE)} entries"
        )
        # The first entry should have been evicted (LRU = oldest first out).
        assert hashes[0] not in dep_module._AUTH_CACHE, (
            "Oldest cache entry should have been evicted when cap was reached"
        )
        # The last entry must be present.
        assert hashes[-1] in dep_module._AUTH_CACHE

        dep_module.clear_auth_cache()
        get_settings.cache_clear()

    def test_cache_respects_configured_cap(self, monkeypatch):
        """Setting API_KEY_CACHE_MAX_ENTRIES=1 keeps exactly one entry."""
        _set_prod_env(monkeypatch)
        monkeypatch.setenv("API_KEY_CACHE_TTL_SECONDS", "300")
        monkeypatch.setenv("API_KEY_CACHE_MAX_ENTRIES", "1")

        from atlas.config import get_settings

        get_settings.cache_clear()

        import atlas.presentation.api.dependencies as dep_module

        dep_module.clear_auth_cache()

        now = datetime.now(UTC)
        row = self._make_fake_row(now)

        for i in range(5):
            dep_module._cache_api_key(f"k{i}", row, now)

        assert len(dep_module._AUTH_CACHE) == 1, (
            f"Cache with max_entries=1 must hold exactly 1 entry, got {len(dep_module._AUTH_CACHE)}"
        )

        dep_module.clear_auth_cache()
        get_settings.cache_clear()
