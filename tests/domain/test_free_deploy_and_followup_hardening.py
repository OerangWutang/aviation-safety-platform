from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from atlas.application.use_cases.query_conflict_history import QueryConflictHistory
from atlas.domain.entities import ClaimConflict, ConflictActivityLogEntry
from atlas.domain.enums import ConflictStatus, ModifierType
from tests.domain._fake_uow import InMemoryUnitOfWork

ROOT = Path(__file__).resolve().parents[2]


def read(path: str) -> str:
    return (ROOT / path).read_text()


def _read_repositories_text() -> str:
    """Concatenated source of every file in the repositories package.

    The repository code was split out of a single ``repositories.py`` file
    in r9.  Static-shape tests that used to ``read("src/atlas/.../repositories.py")``
    the monolith now read every ``.py`` file in the package directory so
    the same assertions cover the split code without changing.
    """
    pkg = ROOT / "src/atlas/infrastructure/db/repositories"
    return "\n".join(p.read_text(encoding="utf-8") for p in sorted(pkg.glob("*.py")))


def test_local_compose_binds_db_and_redis_to_loopback_only() -> None:
    compose = read("docker-compose.yml")
    assert '"127.0.0.1:5432:5432"' in compose
    assert '"127.0.0.1:6379:6379"' in compose
    assert '"5432:5432"' not in compose.replace('"127.0.0.1:5432:5432"', "")
    assert '"6379:6379"' not in compose.replace('"127.0.0.1:6379:6379"', "")


def test_free_deployment_exposes_only_caddy_publicly() -> None:
    compose = read("deploy/free/docker-compose.yml")
    assert "caddy:" in compose
    assert '"80:80"' in compose
    assert '"443:443"' in compose
    assert "db:" in compose
    assert "redis:" in compose
    assert "pgbouncer:" in compose
    # DB and Redis should not have host port mappings in the free-prod stack.
    assert '"5432:5432"' not in compose
    assert '"6379:6379"' not in compose
    assert 'DB_USE_NULL_POOL: "true"' in compose


def test_metrics_refresh_is_ttl_cached() -> None:
    config = read("src/atlas/config.py")
    metrics = read("src/atlas/presentation/api/metrics.py")
    env = read(".env.example")
    assert "prometheus_domain_metrics_ttl_seconds" in config
    assert "_domain_metrics_last_refresh_monotonic" in metrics
    assert "time.monotonic()" in metrics
    assert "prometheus_domain_metrics_ttl_seconds" in metrics
    assert "PROMETHEUS_DOMAIN_METRICS_TTL_SECONDS" in env


def test_remaining_large_in_paths_are_chunked() -> None:
    repositories = _read_repositories_text()
    assert "unique_claim_ids = list(dict.fromkeys(claim_ids))" in repositories
    assert "for chunk in _chunked(unique_claim_ids):" in repositories
    assert "ClaimConflictModel.winning_claim_id.in_(chunk)" in repositories
    assert "ClaimConflictModel.winning_claim_id.in_(claim_ids)" not in repositories
    assert "select(ClaimModel).where(ClaimModel.id.in_(claim_ids))" not in repositories


@pytest.mark.asyncio
async def test_conflict_history_is_keyset_paginated() -> None:
    uow = InMemoryUnitOfWork()
    conflict_id = uuid4()
    event_id = uuid4()
    await uow.conflicts.add(ClaimConflict(id=conflict_id, event_id=event_id, field_name="operator"))

    ids = []
    for sequence in range(1, 5):
        entry = ConflictActivityLogEntry(
            conflict_id=conflict_id,
            event_id=event_id,
            sequence=sequence,
            from_status=None if sequence == 1 else ConflictStatus.OPEN,
            to_status=ConflictStatus.OPEN,
            modifier_type=ModifierType.SYSTEM,
            reason=f"step {sequence}",
            version_at_moment=sequence,
        )
        ids.append(entry.id)
        await uow.conflict_activity.add(entry)

    first = await QueryConflictHistory(uow).execute(conflict_id, limit=2)
    assert [row["sequence"] for row in first["transitions"]] == [1, 2]
    assert first["pagination"]["next_cursor"] == ids[1]

    second = await QueryConflictHistory(uow).execute(
        conflict_id,
        limit=2,
        cursor=first["pagination"]["next_cursor"],
    )
    assert [row["sequence"] for row in second["transitions"]] == [3, 4]
    assert second["pagination"]["next_cursor"] is None


def test_outbox_hot_path_indexes_are_declared_in_orm_and_migration() -> None:
    orm = read("src/atlas/infrastructure/db/orm_models.py")
    migration = read("alembic/versions/022_outbox_polling_indexes.py")
    for name in (
        "ix_outbox_events_pending_created",
        "ix_outbox_events_failed_retry_created",
        "ix_outbox_events_processing_locked",
    ):
        assert name in orm
        assert name in migration


def test_json_logging_uses_json_dumps_and_preserves_existing_handlers() -> None:
    logging_config = read("src/atlas/logging_config.py")
    assert "class JsonLogFormatter" in logging_config
    assert "json.dumps" in logging_config
    assert "root.handlers.clear" not in logging_config


def test_cors_methods_cover_mutating_api_routes() -> None:
    app = read("src/atlas/presentation/api/app.py")
    assert 'allow_methods=["DELETE", "GET", "OPTIONS", "POST", "PUT"]' in app
    assert '"Authorization"' in app


def test_free_deploy_has_env_preflight_and_log_rotation() -> None:
    compose = read("deploy/free/docker-compose.yml")
    makefile = read("Makefile")
    check_env = read("deploy/free/check-env.sh")
    assert "x-default-logging" in compose
    assert 'max-size: "10m"' in compose
    assert 'max-file: "5"' in compose
    assert "./check-env.sh .env && docker compose" in makefile
    assert "POSTGRES_PASSWORD must be changed" in check_env
    assert "ALLOWED_HOSTS must not be '*'" in check_env
    assert "PROMETHEUS_ALLOWED_CIDRS must not allow public scraping" in check_env
    assert "EDGE_RATE_LIMIT_OWNER must name who enforces production edge rate limiting" in check_env
    assert "HSTS_ENABLED must be true in deploy/free production preflight" in check_env


def test_admin_unbounded_rebuilds_are_blocked_in_production_by_default() -> None:
    config = read("src/atlas/config.py")
    admin = read("src/atlas/presentation/api/routers/admin.py")
    env = read(".env.example")
    assert "admin_allow_unbounded_projection_rebuilds" in config
    assert "admin_max_projection_rebuild_events" in config
    assert "Unlimited projection rebuilds are disabled in production" in admin
    assert "ADMIN_ALLOW_UNBOUNDED_PROJECTION_REBUILDS=false" in env


def test_pending_duplicate_reviews_have_unordered_pending_unique_index() -> None:
    orm = read("src/atlas/infrastructure/db/orm_models.py")
    migration = read("alembic/versions/023_pending_duplicate_review_pair_uniqueness.py")
    repositories = _read_repositories_text()
    assert "uq_pending_duplicate_reviews_pending_pair" in orm
    assert "LEAST(event_id_a, event_id_b)" in orm
    assert "GREATEST(event_id_a, event_id_b)" in orm
    assert "WHERE status = 'PENDING'" in migration
    assert "on_conflict_do_nothing" in repositories


def test_outbox_polling_avoids_single_or_heavy_candidate_query() -> None:
    repositories = _read_repositories_text()
    assert "pending_candidates AS" in repositories
    assert "failed_candidates AS" in repositories
    assert "UNION ALL" in repositories
    old_shape = "(status = :pending_status)\n                    OR ("
    assert old_shape not in repositories


def test_ci_has_supply_chain_scans_and_dependabot() -> None:
    ci = read(".github/workflows/ci.yml")
    dependabot = read(".github/dependabot.yml")
    assert "gitleaks/gitleaks-action" in ci
    assert "aquasecurity/trivy-action" in ci
    assert "image-ref: atlas-backend:ci" in ci
    assert 'package-ecosystem: "pip"' in dependabot
    assert 'package-ecosystem: "github-actions"' in dependabot
    assert 'package-ecosystem: "docker"' in dependabot


def test_json_logging_preserves_extra_fields_and_background_errors_have_exc_info() -> None:
    logging_config = read("src/atlas/logging_config.py")
    deps = read("src/atlas/presentation/api/dependencies.py")
    assert 'payload["extra"] = extra' in logging_config
    assert "_json_safe" in logging_config
    assert "exc_info=True" in deps
    assert "api_key_id" in deps


def test_free_deploy_worker_command_and_healthchecks_are_service_specific() -> None:
    compose = read("deploy/free/docker-compose.yml")
    dockerfile = read("Dockerfile")
    assert "command: atlas outbox-worker --sleep-seconds 5" in compose
    assert "command: python -m atlas.infrastructure.event_bus.outbox_worker" not in compose
    assert "HEALTHCHECK" not in dockerfile
    assert "worker:" in compose
    assert "healthcheck:\n      disable: true" in compose
    assert "urllib.request.urlopen" in compose


def test_free_deploy_preflight_is_enforced_by_compose_and_url_safe_passwords() -> None:
    compose = read("deploy/free/docker-compose.yml")
    check_env = read("deploy/free/check-env.sh")
    env = read("deploy/free/.env.example")
    assert "preflight:" in compose
    assert "condition: service_completed_successfully" in compose
    assert "./.env:/run/atlas/.env:ro" in compose
    assert "require_url_safe" in check_env
    assert "RATE_LIMIT_REQUESTS must be 0" in check_env
    assert "EDGE_RATE_LIMIT_OWNER" in check_env
    assert "RATE_LIMIT_REQUESTS=0" in env
    assert "EDGE_RATE_LIMIT_OWNER=caddy" in env


def test_free_deploy_docs_include_first_admin_bootstrap() -> None:
    docs = read("deploy/free/README.md")
    assert "Create the first admin API key" in docs
    assert "docker compose --env-file .env run --rm api atlas bootstrap --role admin" in docs
    assert "URL-safe values" in docs


def test_outbox_polling_uses_deterministic_id_tie_breakers() -> None:
    repositories = _read_repositories_text()
    orm = read("src/atlas/infrastructure/db/orm_models.py")
    migration = read("alembic/versions/022_outbox_polling_indexes.py")
    assert "ORDER BY created_at, id" in repositories
    assert "ORDER BY next_attempt_at NULLS FIRST, created_at, id" in repositories
    assert '"created_at",\n            "id"' in orm
    assert '["created_at", "id"]' in migration
    assert "next_attempt_at ASC NULLS FIRST" in migration


@pytest.mark.asyncio
async def test_pending_duplicate_review_fallback_prefers_pending_row() -> None:
    from atlas.domain.entities import PendingDuplicateReview
    from atlas.domain.enums import DuplicateReviewStatus

    uow = InMemoryUnitOfWork()
    event_a = uuid4()
    event_b = uuid4()
    rejected = PendingDuplicateReview(
        event_id_a=event_a,
        event_id_b=event_b,
        status=DuplicateReviewStatus.REJECTED,
        match_score=0.5,
        matched_fields=["registration"],
    )
    pending = PendingDuplicateReview(
        event_id_a=event_b,
        event_id_b=event_a,
        status=DuplicateReviewStatus.PENDING,
        match_score=0.6,
        matched_fields=["registration"],
    )
    await uow.duplicate_reviews.add(rejected)
    stored = await uow.duplicate_reviews.add(pending)
    assert stored is pending
    assert await uow.duplicate_reviews.find_pending_pair(event_a, event_b) is pending
    assert await uow.duplicate_reviews.find_existing_pair(event_a, event_b) is pending


def test_free_deploy_has_bootstrap_target_and_backup_examples() -> None:
    makefile = read("Makefile")
    docs = read("deploy/free/README.md")
    cron = read("deploy/free/examples/backup-cron.example")
    drill = read("deploy/free/examples/RESTORE_DRILL.md")
    assert "free-bootstrap" in makefile
    assert "atlas bootstrap --role admin" in makefile
    assert "examples/backup-cron.example" in docs
    assert "restore drill" in docs.lower()
    assert "./backup-postgres.sh" in cron
    assert "./restore-postgres.sh" in drill


def test_free_deploy_healthcheck_uses_allowed_host_and_env_lists_local_hosts() -> None:
    compose = read("deploy/free/docker-compose.yml")
    env = read("deploy/free/.env.example")
    assert 'headers={"Host": host}' in compose
    assert 'os.environ.get("ATLAS_DOMAIN")' in compose
    assert "ALLOWED_HOSTS=api.example.com,127.0.0.1,localhost" in env


def test_free_deploy_preflight_enforces_secret_strength_and_domain_host_match() -> None:
    check_env = read("deploy/free/check-env.sh")
    assert 'require_hex_secret "API_KEY_HASH_SECRET"' in check_env
    assert 'require_hex_secret "API_KEY_HASH_SECRET_PREVIOUS"' in check_env
    assert 'require_min_length "POSTGRES_PASSWORD"' in check_env
    assert 'require_min_length "PROMETHEUS_BEARER_TOKEN"' in check_env
    assert 'require_csv_contains "ALLOWED_HOSTS"' in check_env
    assert "HERMES_ALLOWED_HOSTS must be set in production" in check_env
    assert "secrets.token_hex(32)" in check_env


@pytest.mark.asyncio
async def test_pending_duplicate_reviews_are_keyset_paginated() -> None:
    from atlas.application.use_cases.list_pending_reviews import ListPendingDuplicateReviews
    from atlas.domain.entities import PendingDuplicateReview
    from atlas.domain.enums import DuplicateReviewStatus

    uow = InMemoryUnitOfWork()
    now = datetime(2026, 5, 13, tzinfo=UTC)
    for offset in range(4):
        await uow.duplicate_reviews.add(
            PendingDuplicateReview(
                event_id_a=uuid4(),
                event_id_b=uuid4(),
                status=DuplicateReviewStatus.PENDING,
                match_score=0.5,
                matched_fields=["registration"],
                created_at=now + timedelta(minutes=offset),
            )
        )

    first = await ListPendingDuplicateReviews(uow).execute_page(limit=2)
    assert len(first.items) == 2
    assert first.next_cursor == first.items[-1].id
    assert first.items[0].created_at > first.items[1].created_at

    second = await ListPendingDuplicateReviews(uow).execute_page(
        limit=2,
        cursor=first.next_cursor,
    )
    assert len(second.items) == 2
    assert second.next_cursor is None
    assert second.items[0].created_at > second.items[1].created_at
    assert first.items[-1].created_at > second.items[0].created_at


def test_pending_review_pagination_index_and_redundant_index_drop_are_declared() -> None:
    orm = read("src/atlas/infrastructure/db/orm_models.py")
    migration = read("alembic/versions/024_selfhost_runtime_polish_indexes.py")
    assert "ix_pending_duplicate_reviews_pending_created_id" in orm
    assert "ix_pending_duplicate_reviews_pending_created_id" in migration
    assert "created_at DESC" in migration
    assert "id DESC" in migration
    assert "DROP INDEX IF EXISTS ix_claim_conflict_claims_conflict_id" in migration
    assert "DROP INDEX IF EXISTS ix_conflict_activity_log_conflict_id" in migration
    assert 'ForeignKey("claim_conflicts.id"), nullable=False, index=True' not in orm


def test_prometheus_expensive_domain_metrics_are_optional() -> None:
    config = read("src/atlas/config.py")
    metrics = read("src/atlas/presentation/api/metrics.py")
    use_case = read("src/atlas/application/use_cases/query_operational_metrics.py")
    env = read(".env.example")
    assert "prometheus_expensive_domain_metrics_enabled" in config
    assert "include_expensive_totals: bool = True" in use_case
    assert "include_expensive = settings.prometheus_expensive_domain_metrics_enabled" in metrics
    assert "PROMETHEUS_EXPENSIVE_DOMAIN_METRICS_ENABLED=false" in env


def test_free_deploy_has_backup_retention_and_offserver_examples() -> None:
    docs = read("deploy/free/README.md")
    cron = read("deploy/free/examples/backup-cron.example")
    offsite = read("deploy/free/examples/OFFSERVER_BACKUPS.md")
    prune = read("deploy/free/prune-backups.sh")
    assert "prune-backups.sh" in docs
    assert "OFFSERVER_BACKUPS.md" in docs
    assert "BACKUP_RETENTION_DAYS=14 ./prune-backups.sh" in cron
    assert "rclone copy" in offsite
    assert "restic backup" in offsite
    assert 'find "$BACKUP_DIR"' in prune


def test_lockfiles_are_marked_for_python_312_runtime() -> None:
    assert "pip-compile with Python 3.12" in read("requirements.txt")
    assert "pip-compile with Python 3.12" in read("requirements-dev.txt")


def test_python_settings_enforce_strong_api_key_secret_in_production(monkeypatch) -> None:
    from atlas.config import get_settings

    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@localhost/atlas")
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("API_KEY_HASH_SECRET", "abc123")
    monkeypatch.setenv("ALLOWED_HOSTS", "api.example.com")
    monkeypatch.setenv("API_DOCS_ENABLED", "false")
    monkeypatch.setenv("SECURITY_HEADERS_ENABLED", "true")
    monkeypatch.setenv("PROMETHEUS_ALLOWED_CIDRS", "127.0.0.1/32")
    get_settings.cache_clear()

    with pytest.raises(RuntimeError, match="64 hexadecimal"):
        get_settings().warn_if_insecure()


@pytest.mark.asyncio
async def test_conflict_history_missing_conflict_raises_not_found() -> None:
    from atlas.domain.exceptions import ConflictNotFoundError

    uow = InMemoryUnitOfWork()
    with pytest.raises(ConflictNotFoundError):
        await QueryConflictHistory(uow).execute(uuid4())


@pytest.mark.asyncio
async def test_provenance_missing_event_raises_not_found() -> None:
    from atlas.application.use_cases.query_provenance import QueryProvenance
    from atlas.domain.exceptions import EventNotFoundError

    uow = InMemoryUnitOfWork()
    with pytest.raises(EventNotFoundError):
        await QueryProvenance(uow).execute(uuid4())


def test_restore_script_requires_explicit_confirmation_and_docs_match_filename() -> None:
    restore = read("deploy/free/restore-postgres.sh")
    drill = read("deploy/free/examples/RESTORE_DRILL.md")
    check_env = read("deploy/free/check-env.sh")
    assert "ATLAS_RESTORE_CONFIRM" in restore
    assert (
        "ATLAS_RESTORE_CONFIRM=1 ./restore-postgres.sh ./backups/atlas-YYYYMMDDTHHMMSSZ.sql.gz"
        in drill
    )
    assert "CORS_ORIGINS still contains the example.com placeholder" in check_env


def test_outbox_worker_health_metric_is_exposed() -> None:
    interfaces = read("src/atlas/domain/interfaces/repositories.py")
    use_case = read("src/atlas/application/use_cases/query_operational_metrics.py")
    metrics = read("src/atlas/presentation/api/metrics.py")
    assert "oldest_unprocessed_age_seconds" in interfaces
    assert "outbox_oldest_unprocessed_age_seconds" in use_case
    assert "atlas_outbox_oldest_unprocessed_age_seconds" in metrics


def test_free_deploy_preflight_requires_hermes_allowed_hosts() -> None:
    check_env = read("deploy/free/check-env.sh")
    env = read("deploy/free/.env.example")
    docs = read("deploy/free/README.md")
    compose = read("deploy/free/docker-compose.yml")
    assert "hermes-worker:" in compose
    assert "HERMES_ALLOWED_HOSTS: ${HERMES_ALLOWED_HOSTS:-}" in compose
    assert (
        "HERMES_ALLOWED_HOSTS must be set in production when hermes-worker is enabled" in check_env
    )
    assert "HERMES_ALLOWED_HOSTS=aviation-safety.net,ntsb.gov" in env
    assert "preflight" in docs and "rejects an empty value" in docs


def test_production_rejects_short_prometheus_bearer_token(monkeypatch) -> None:
    """PROMETHEUS_BEARER_TOKEN < 32 chars must be rejected at production startup.

    ``check-env.sh`` enforces this at deploy time; this test ensures the
    Python settings validator is an independent defence so the error is
    caught even if the shell preflight is not run (e.g. direct ``docker run``
    invocations or cloud-provider entrypoints that bypass the script).
    """
    from atlas.config import get_settings

    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@localhost/atlas")
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("API_KEY_HASH_SECRET", "a" * 64)
    monkeypatch.setenv("ALLOWED_HOSTS", "api.example.com")
    monkeypatch.setenv("API_DOCS_ENABLED", "false")
    monkeypatch.setenv("SECURITY_HEADERS_ENABLED", "true")
    monkeypatch.setenv("TENANT_DATABASE_URL", "postgresql+asyncpg://t:p@localhost/atlas")
    monkeypatch.setenv("SYSTEM_DATABASE_URL", "postgresql+asyncpg://s:p@localhost/atlas")
    monkeypatch.setenv("PROMETHEUS_BEARER_TOKEN", "tooshort")
    monkeypatch.setenv("PROMETHEUS_ALLOWED_CIDRS", "")
    get_settings.cache_clear()

    with pytest.raises(RuntimeError, match="32 characters"):
        get_settings().validate_api_runtime_settings()

    get_settings.cache_clear()


def test_production_accepts_strong_prometheus_bearer_token(monkeypatch) -> None:
    """A 32-char bearer token satisfies the production validator."""
    from atlas.config import get_settings

    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@localhost/atlas")
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("API_KEY_HASH_SECRET", "a" * 64)
    monkeypatch.setenv("ALLOWED_HOSTS", "api.example.com")
    monkeypatch.setenv("API_DOCS_ENABLED", "false")
    monkeypatch.setenv("SECURITY_HEADERS_ENABLED", "true")
    monkeypatch.setenv("TENANT_DATABASE_URL", "postgresql+asyncpg://t:p@localhost/atlas")
    monkeypatch.setenv("SYSTEM_DATABASE_URL", "postgresql+asyncpg://s:p@localhost/atlas")
    # Exactly 32 chars is the minimum — must not raise.
    monkeypatch.setenv("PROMETHEUS_BEARER_TOKEN", "a" * 32)
    monkeypatch.setenv("PROMETHEUS_ALLOWED_CIDRS", "")
    get_settings.cache_clear()

    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        # Should complete without RuntimeError.
        get_settings().validate_api_runtime_settings()

    get_settings.cache_clear()
