from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def read(path: str) -> str:
    return (ROOT / path).read_text()


def test_gunicorn_config_has_safe_production_defaults() -> None:
    config = read("gunicorn_conf.py")
    assert "uvicorn.workers.UvicornWorker" in config
    assert "WEB_CONCURRENCY" in config
    assert "GUNICORN_KEEPALIVE_SECONDS" in config
    assert "GUNICORN_MAX_REQUESTS" in config
    assert "GUNICORN_MAX_REQUESTS_JITTER" in config
    assert "worker_tmp_dir" in config


def test_dockerfile_uses_non_root_gunicorn_runtime() -> None:
    dockerfile = read("Dockerfile")
    assert "FROM python:3.12-slim" in dockerfile
    assert " AS builder" in dockerfile
    assert "USER atlas" in dockerfile
    assert "HEALTHCHECK" not in dockerfile
    assert (
        'CMD ["gunicorn", "atlas.presentation.api.app:app", "--config", "gunicorn_conf.py", "--worker-tmp-dir", "/dev/shm"]'
        in dockerfile
    )
    assert "--reload" not in dockerfile


def test_prometheus_endpoint_and_domain_gauges_are_registered() -> None:
    app = read("src/atlas/presentation/api/app.py")
    metrics = read("src/atlas/presentation/api/metrics.py")
    middleware = read("src/atlas/presentation/api/middleware.py")
    assert "install_prometheus(app)" in app
    assert '"/metrics"' in metrics
    assert "_metrics_request_allowed" in metrics
    assert "prometheus_allowed_cidrs" in metrics
    assert "prometheus_bearer_token" in metrics
    assert "atlas_outbox_events_total" in metrics
    assert "atlas_conflicts_total" in metrics
    assert "QueryOperationalMetrics" in metrics
    assert 'path in {"/health", "/ready", "/metrics"}' in middleware


def test_dependencies_include_production_runtime_and_metrics_packages() -> None:
    pyproject = read("pyproject.toml")
    requirements = read("requirements.in")
    lock = read("requirements.txt")
    for package in ("gunicorn", "prometheus-fastapi-instrumentator"):
        assert package in pyproject
        assert package in requirements
        assert package in lock


def test_load_test_and_pgbouncer_artifacts_exist() -> None:
    k6 = read("ops/load/atlas_k6_load_test.js")
    pgbouncer = read("ops/pgbouncer/pgbouncer.ini")
    operations = read("OPERATIONS.md")
    assert "constant-vus" in k6
    assert "duplicate_ingestion_spike" in k6
    assert "/api/v1/ingestion/sources/" in k6
    assert "uniqueSequence" in k6
    assert "source_record_id: `k6-${recordNumber}-${uniqueSequence}`" in k6
    assert "pool_mode = transaction" in pgbouncer
    assert "DB_USE_NULL_POOL=true" in operations
    assert "/metrics" in operations
    assert "PROMETHEUS_ALLOWED_CIDRS" in operations
    assert "PROMETHEUS_BEARER_TOKEN" in operations


def test_easy_security_hardening_artifacts_are_configured() -> None:
    app = read("src/atlas/presentation/api/app.py")
    config = read("src/atlas/config.py")
    middleware = read("src/atlas/presentation/api/middleware.py")
    env_example = read(".env.example")

    assert "TrustedHostMiddleware" in app
    assert "SecurityHeadersMiddleware" in app
    assert 'docs_url="/docs" if settings.effective_api_docs_enabled else None' in app
    assert "allowed_hosts" in config
    assert "effective_api_docs_enabled" in config
    assert "SECURITY_HEADERS_ENABLED must remain true in production" in config
    assert "Production CORS origins must use https://" in config
    assert "x-content-type-options" in middleware
    assert "strict-transport-security" in middleware
    assert "ALLOWED_HOSTS" in env_example
    assert "SECURITY_HEADERS_ENABLED" in env_example
    assert "HSTS_ENABLED" in env_example
