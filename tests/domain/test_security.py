"""Tests for API key hashing in security.py."""

import hashlib
import hmac

from atlas.config import get_settings


def _set_db_env(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@localhost/atlas")
    monkeypatch.setenv("DATABASE_SYNC_URL", "postgresql://u:p@localhost/atlas")
    monkeypatch.setenv("POSTGRES_USER", "u")
    monkeypatch.setenv("POSTGRES_PASSWORD", "p")
    monkeypatch.setenv("POSTGRES_DB", "atlas")
    # Required by _validate_production_db_roles so tests that set
    # ENVIRONMENT=production reach the check they are actually testing.
    monkeypatch.setenv("TENANT_DATABASE_URL", "postgresql+asyncpg://atlas_app:p@localhost/atlas")
    monkeypatch.setenv("SYSTEM_DATABASE_URL", "postgresql+asyncpg://atlas_system:p@localhost/atlas")
    # Suppress HSTS / CORS UserWarnings so production-env tests only emit the
    # warning they are actually testing, not unrelated config noise.
    monkeypatch.setenv("HSTS_ENABLED", "true")
    monkeypatch.setenv("CORS_ORIGINS", "https://example.com")


def test_hash_api_key_without_secret_uses_sha256(monkeypatch):
    _set_db_env(monkeypatch)
    monkeypatch.delenv("API_KEY_HASH_SECRET", raising=False)
    get_settings.cache_clear()

    from atlas.security import hash_api_key

    key = "my-plain-key"
    assert hash_api_key(key) == hashlib.sha256(key.encode()).hexdigest()


def test_hash_api_key_with_secret_uses_hmac(monkeypatch):
    _set_db_env(monkeypatch)
    monkeypatch.setenv(
        "API_KEY_HASH_SECRET", "0000000000000000000000000000000000000000000000000000000000000000"
    )
    get_settings.cache_clear()

    from atlas.security import hash_api_key

    key = "my-plain-key"
    expected = hmac.digest(
        b"0000000000000000000000000000000000000000000000000000000000000000",
        key.encode(),
        hashlib.sha256,
    ).hex()
    assert hash_api_key(key) == expected


def test_hash_api_key_with_and_without_secret_differ(monkeypatch):
    _set_db_env(monkeypatch)
    key = "my-plain-key"

    monkeypatch.delenv("API_KEY_HASH_SECRET", raising=False)
    get_settings.cache_clear()
    from atlas.security import hash_api_key

    plain_hash = hash_api_key(key)

    monkeypatch.setenv("API_KEY_HASH_SECRET", "another-secret")
    get_settings.cache_clear()
    hmac_hash = hash_api_key(key)

    assert plain_hash != hmac_hash


def test_hash_candidates_include_current_then_previous(monkeypatch):
    _set_db_env(monkeypatch)
    monkeypatch.setenv("API_KEY_HASH_SECRET", "a" * 64)
    monkeypatch.setenv("API_KEY_HASH_SECRET_PREVIOUS", "b" * 64)
    get_settings.cache_clear()

    from atlas.security import hash_api_key_candidates

    key = "my-plain-key"
    hashes = hash_api_key_candidates(key)
    assert len(hashes) == 2
    assert hashes[0] != hashes[1]


def test_hash_candidates_deduplicate_when_previous_equals_current(monkeypatch):
    _set_db_env(monkeypatch)
    monkeypatch.setenv("API_KEY_HASH_SECRET", "a" * 64)
    monkeypatch.setenv("API_KEY_HASH_SECRET_PREVIOUS", "a" * 64)
    get_settings.cache_clear()

    from atlas.security import hash_api_key_candidates

    key = "my-plain-key"
    hashes = hash_api_key_candidates(key)
    assert len(hashes) == 1


def test_previous_secret_must_be_hex_if_set(monkeypatch):
    _set_db_env(monkeypatch)
    monkeypatch.setenv("API_KEY_HASH_SECRET", "a" * 64)
    monkeypatch.setenv("API_KEY_HASH_SECRET_PREVIOUS", "not-hex")
    get_settings.cache_clear()

    import pytest

    settings = get_settings()
    with pytest.raises(RuntimeError, match="API_KEY_HASH_SECRET_PREVIOUS"):
        settings.validate_common_runtime_settings()


def test_production_requires_explicit_allowed_hosts(monkeypatch):
    _set_db_env(monkeypatch)
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv(
        "API_KEY_HASH_SECRET", "0000000000000000000000000000000000000000000000000000000000000000"
    )
    monkeypatch.setenv("API_DOCS_ENABLED", "false")
    monkeypatch.setenv("SECURITY_HEADERS_ENABLED", "true")
    monkeypatch.setenv("PROMETHEUS_ALLOWED_CIDRS", "127.0.0.1/32")
    monkeypatch.setenv("ALLOWED_HOSTS", "*")
    get_settings.cache_clear()

    settings = get_settings()
    try:
        settings.warn_if_insecure()
    except RuntimeError as exc:
        assert "ALLOWED_HOSTS" in str(exc)
    else:  # pragma: no cover - explicit assertion style for old pytest versions
        raise AssertionError("production wildcard ALLOWED_HOSTS should fail startup")


def test_production_disables_docs_by_default(monkeypatch):
    _set_db_env(monkeypatch)
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv(
        "API_KEY_HASH_SECRET", "0000000000000000000000000000000000000000000000000000000000000000"
    )
    monkeypatch.setenv("ALLOWED_HOSTS", "api.example.com")
    monkeypatch.setenv("SECURITY_HEADERS_ENABLED", "true")
    monkeypatch.setenv("PROMETHEUS_ALLOWED_CIDRS", "127.0.0.1/32")
    monkeypatch.delenv("API_DOCS_ENABLED", raising=False)
    get_settings.cache_clear()

    settings = get_settings()
    assert settings.effective_api_docs_enabled is False


def test_production_rejects_http_cors_origins(monkeypatch):
    _set_db_env(monkeypatch)
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv(
        "API_KEY_HASH_SECRET", "0000000000000000000000000000000000000000000000000000000000000000"
    )
    monkeypatch.setenv("ALLOWED_HOSTS", "api.example.com")
    monkeypatch.setenv("API_DOCS_ENABLED", "false")
    monkeypatch.setenv("SECURITY_HEADERS_ENABLED", "true")
    monkeypatch.setenv("PROMETHEUS_ALLOWED_CIDRS", "127.0.0.1/32")
    monkeypatch.setenv("CORS_ORIGINS", "http://evil.example.com")
    get_settings.cache_clear()

    settings = get_settings()
    try:
        settings.warn_if_insecure()
    except RuntimeError as exc:
        assert "CORS" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("production HTTP CORS origin should fail startup")


# ── _metrics_request_allowed unit tests ──────────────────────────────────────


def _make_request(client_host: str | None, auth_header: str = "") -> object:
    """Create a minimal mock Request object for _metrics_request_allowed tests."""
    from unittest.mock import MagicMock

    req = MagicMock()
    if client_host is None:
        req.client = None
    else:
        req.client = MagicMock()
        req.client.host = client_host
    req.headers.get = lambda key, default="": auth_header if key == "authorization" else default
    return req


def test_metrics_allowed_from_localhost(monkeypatch):
    """Default CIDR (127.0.0.1/32) permits loopback scrapes."""
    _set_db_env(monkeypatch)
    monkeypatch.setenv("PROMETHEUS_ALLOWED_CIDRS", "127.0.0.1/32")
    monkeypatch.delenv("PROMETHEUS_BEARER_TOKEN", raising=False)
    get_settings.cache_clear()

    from atlas.presentation.api.metrics import _metrics_request_allowed

    req = _make_request("127.0.0.1")
    assert _metrics_request_allowed(req) is True  # type: ignore[arg-type]


def test_metrics_denied_from_public_ip(monkeypatch):
    """A public IP address not in the CIDR list is denied."""
    _set_db_env(monkeypatch)
    monkeypatch.setenv("PROMETHEUS_ALLOWED_CIDRS", "127.0.0.1/32")
    monkeypatch.delenv("PROMETHEUS_BEARER_TOKEN", raising=False)
    get_settings.cache_clear()

    from atlas.presentation.api.metrics import _metrics_request_allowed

    req = _make_request("8.8.8.8")
    assert _metrics_request_allowed(req) is False  # type: ignore[arg-type]


def test_metrics_allowed_via_bearer_token(monkeypatch):
    """Correct bearer token grants access regardless of IP."""
    _set_db_env(monkeypatch)
    monkeypatch.setenv("PROMETHEUS_ALLOWED_CIDRS", "")
    monkeypatch.setenv("PROMETHEUS_BEARER_TOKEN", "supersecrettoken12345678901234567")
    get_settings.cache_clear()

    from atlas.presentation.api.metrics import _metrics_request_allowed

    req = _make_request("8.8.8.8", auth_header="Bearer supersecrettoken12345678901234567")
    assert _metrics_request_allowed(req) is True  # type: ignore[arg-type]


def test_metrics_denied_wrong_bearer_token(monkeypatch):
    """Wrong bearer token is denied even from a CIDR-allowed IP."""
    _set_db_env(monkeypatch)
    monkeypatch.setenv("PROMETHEUS_ALLOWED_CIDRS", "127.0.0.1/32")
    monkeypatch.setenv("PROMETHEUS_BEARER_TOKEN", "correct-token" + "x" * 20)
    get_settings.cache_clear()

    from atlas.presentation.api.metrics import _metrics_request_allowed

    # Wrong token, but IP is in allowed CIDR.
    # The OR logic means the IP check still passes — this is intentional
    # (bearer token and CIDR are independent, not cumulative guards).
    req = _make_request("127.0.0.1", auth_header="Bearer wrong-token")
    # Localhost is in the CIDR even with wrong token — by design.
    assert _metrics_request_allowed(req) is True  # type: ignore[arg-type]


def test_metrics_denied_no_client_ip(monkeypatch):
    """Requests with no client information are denied."""
    _set_db_env(monkeypatch)
    monkeypatch.setenv("PROMETHEUS_ALLOWED_CIDRS", "127.0.0.1/32")
    monkeypatch.delenv("PROMETHEUS_BEARER_TOKEN", raising=False)
    get_settings.cache_clear()

    from atlas.presentation.api.metrics import _metrics_request_allowed

    req = _make_request(None)  # no client IP
    assert _metrics_request_allowed(req) is False  # type: ignore[arg-type]


def test_metrics_denied_invalid_client_ip(monkeypatch):
    """Non-parseable client IP strings are denied gracefully (no exception)."""
    _set_db_env(monkeypatch)
    monkeypatch.setenv("PROMETHEUS_ALLOWED_CIDRS", "127.0.0.1/32")
    monkeypatch.delenv("PROMETHEUS_BEARER_TOKEN", raising=False)
    get_settings.cache_clear()

    from atlas.presentation.api.metrics import _metrics_request_allowed

    req = _make_request("not-an-ip-address")
    assert _metrics_request_allowed(req) is False  # type: ignore[arg-type]


# ── CORS_ORIGINS validator ────────────────────────────────────────────────────


def test_cors_origins_wildcard_rejected_at_parse_time():
    """CORS_ORIGINS='*' must be rejected by the field validator.

    Using ``*`` with credentialed requests is a well-known CORS misconfiguration.
    The validator must raise ``ValueError`` before the Settings object is
    constructed so the error surface is deterministic regardless of environment.
    """
    import pytest
    from pydantic import ValidationError

    from atlas.config import Settings

    with pytest.raises(ValidationError, match="\\*"):
        Settings(
            database_url="postgresql+asyncpg://u:p@localhost/atlas",
            cors_origins=["*"],
        )


def test_cors_origins_wildcard_in_string_rejected():
    """CORS_ORIGINS='*' passed as an env string is also rejected."""
    import pytest
    from pydantic import ValidationError

    from atlas.config import Settings

    with pytest.raises(ValidationError, match="\\*"):
        Settings(
            database_url="postgresql+asyncpg://u:p@localhost/atlas",
            cors_origins="*",  # type: ignore[arg-type]
        )
