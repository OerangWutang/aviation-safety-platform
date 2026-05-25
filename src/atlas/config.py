from __future__ import annotations

import logging
import re
import warnings
from functools import lru_cache
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

logger = logging.getLogger(__name__)


class Settings(BaseSettings):
    database_url: str
    # Optional least-privilege runtime URLs.  DATABASE_URL remains the
    # development/default URL for backwards compatibility.  In production,
    # TENANT_DATABASE_URL must point at a NOBYPASSRLS application role and
    # SYSTEM_DATABASE_URL must point at a separate system/worker role.
    tenant_database_url: str | None = None
    system_database_url: str | None = None
    # Only the API/worker runtime needs DATABASE_URL. Alembic reads
    # DATABASE_SYNC_URL directly from the environment in alembic/env.py so
    # runtime containers do not have to carry migration-only secrets.
    database_sync_url: str | None = None
    postgres_user: str | None = None
    postgres_password: str | None = None
    postgres_db: str | None = None

    # ── Topology: separate public Atlas database ─────────────────────────────
    # When set, public projection reads (Echo corpus loader, public event
    # queries) connect to this database instead of DATABASE_URL.  This
    # implements the "fully separate, sync public→private" topology: the
    # public Atlas DB holds canonical event projections; the SMS (tenant) DB
    # holds tenant_* tables and a synced copy of the corpus.
    #
    # Leave unset (the default) to use a single shared database — the topology
    # is logical rather than physical, which is correct for development and for
    # deployments that have not yet split the databases.
    #
    # Set PUBLIC_DATABASE_URL to the async connection string for the public DB.
    # Set PUBLIC_DATABASE_SYNC_URL for Alembic migrations on the public DB.
    public_database_url: str | None = None
    public_database_sync_url: str | None = None

    redis_url: str | None = None
    # Current key-hash secret. New keys are always hashed with this value.
    api_key_hash_secret: str | None = None
    # Optional rotation bridge secret accepted for verification only.
    # When set, auth accepts keys hashed with either current or previous secret.
    api_key_hash_secret_previous: str | None = None

    # Validated API keys are cached briefly in-process to keep hot authenticated
    # traffic from turning into identical auth SELECTs on every request. Set TTL
    # to 0 to disable local caching when key revocation must be immediate.
    #
    # Production note: the cache is per-process and per-instance, so revoked or
    # role-changed keys remain valid until the TTL expires on every running
    # process. For high-assurance deployments, set API_KEY_CACHE_TTL_SECONDS=0
    # (fail closed) or keep it very short (e.g. 5 seconds).
    # The default of 5 seconds is a compromise: low revocation window (≤5 s per
    # process), eliminates thundering-herd auth DB load on busy deployments.
    api_key_cache_ttl_seconds: int = Field(default=5, ge=0, le=3600)
    api_key_cache_max_entries: int = Field(default=10_000, gt=0, le=1_000_000)

    # ``environment`` gates production-only checks (see ``warn_if_insecure``).
    # Allowed values: development | staging | production | test. A Literal
    # type makes Pydantic reject typos like "prod" or "Production" at startup
    # instead of silently bypassing the ``is_production`` guard.
    environment: Literal["development", "staging", "production", "test"] = "development"

    max_claims_per_request: int = Field(default=500, gt=0, le=10_000)
    max_raw_payload_bytes: int = Field(default=1_048_576, gt=0, le=104_857_600)
    # A single ambiguous identity match can tie against multiple canonical
    # events. Fan out enough reviews to preserve useful curator context, but
    # cap it so sparse/noisy submissions cannot flood the review queue.
    max_duplicate_reviews_per_ingestion: int = Field(default=10, gt=0, le=50)
    # Whole HTTP request body cap.  The raw_payload cap is still enforced in
    # the use case; this framework-level cap stops huge JSON envelopes before
    # Pydantic materialises them in memory.
    request_body_overhead_bytes: int = Field(default=65_536, ge=0, le=5_242_880)

    # Baseline in-process limiter.  It is enabled by default outside
    # production for a safe local/dev guardrail, but production must opt in
    # explicitly because this limiter is per-process and IP-address based.
    rate_limit_requests: int = Field(default=600, ge=0)
    rate_limit_window_seconds: int = Field(default=60, gt=0)
    rate_limit_in_memory_enabled: bool | None = None

    outbox_max_attempts: int = Field(default=5, gt=0, le=100)
    outbox_stale_lock_minutes: int = Field(default=10, gt=0)

    curator_override_source_name: str = "CuratorOverride"
    curator_override_source_id: UUID = UUID("00000000-0000-0000-0000-000000000001")

    log_level: str = "INFO"
    cors_origins: Annotated[list[str], NoDecode] = Field(default_factory=list)

    # Host-header protection. Leave wildcard in development for local tooling,
    # but set this explicitly in production, e.g. api.example.com,localhost.
    allowed_hosts: Annotated[list[str], NoDecode] = Field(default_factory=lambda: ["*"])

    # Interactive API docs are useful locally but should not be exposed by
    # default in production. Set API_DOCS_ENABLED=true only behind auth/VPN.
    api_docs_enabled: bool | None = None

    # Browser/security response headers. HSTS is safe only when the deployment
    # is actually served over HTTPS, so it is separately configurable.
    security_headers_enabled: bool = True
    hsts_enabled: bool = False
    hsts_max_age_seconds: int = Field(default=31_536_000, ge=0)

    # Prometheus scraping is intentionally separate from normal API-key auth so
    # scrapes do not create DB-backed auth traffic.  Keep /metrics network-
    # private via CIDR allow-list and/or set a static bearer token for scrapers.
    prometheus_metrics_enabled: bool = True
    prometheus_domain_metrics_enabled: bool = True
    # Exact historical totals (processed outbox, resolved conflicts, total
    # claims/projections) can become expensive on very large installations.
    # Keep them disabled for Prometheus by default; the authenticated admin JSON
    # metrics endpoint still returns exact values on demand.
    prometheus_expensive_domain_metrics_enabled: bool = False
    # Cache DB-backed gauges so a Prometheus scrape interval cannot become a
    # table-count benchmark. Set to 0 to refresh on every authorized scrape.
    prometheus_domain_metrics_ttl_seconds: int = Field(default=15, ge=0, le=3600)
    prometheus_allowed_cidrs: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["127.0.0.1/32", "::1/128"]
    )
    prometheus_bearer_token: str | None = None

    # Synchronous admin rebuilds are convenient for maintenance, but an
    # unlimited rebuild through HTTP can tie up an API worker or be retried by
    # clients/proxies. Keep production bounded unless explicitly overridden.
    admin_allow_unbounded_projection_rebuilds: bool = False
    admin_max_projection_rebuild_events: int = Field(default=10_000, gt=0)

    db_pool_size: int = Field(default=10, gt=0, le=200)
    db_max_overflow: int = Field(default=20, ge=0, le=200)
    db_pool_recycle_seconds: int = Field(default=3600, gt=0)
    # When Atlas sits behind PgBouncer in transaction-pooling mode, SQLAlchemy
    # should not hold its own persistent pool. Leave False for direct Postgres.
    db_use_null_pool: bool = False

    # ── Echo cross-reference corpus cache ───────────────────────────────────
    # The public precedent corpus (~30k events) is loaded into memory on the
    # first cross-reference run and then cached for this many seconds.
    # Subsequent runs reuse the cached corpus, avoiding the ~8s full table scan.
    # Set to 0 to disable caching (reload on every run — useful in tests or
    # when the corpus is updated frequently).
    echo_corpus_cache_ttl_seconds: int = Field(default=3600, ge=0, le=86400)

    # ── Hermes crawler ──────────────────────────────────────────────────────
    # Comma-separated host/domain allowlist for the Hermes server-side fetcher.
    # Each entry is matched as an exact hostname or a dot-bounded parent domain
    # (so ``example.com`` covers ``news.example.com`` but not
    # ``evil-example.com``).  Empty list means "no allowlist — IP-range deny
    # rules only", which is acceptable in development.  In production, setting
    # this to the crawl-target domain set provides defense-in-depth against DNS
    # rebinding and accidental crawling of internal services.
    # REQUIRED in production when the Hermes worker runs (see
    # validate_hermes_worker_settings).
    hermes_allowed_hosts: Annotated[list[str], NoDecode] = Field(default_factory=list)

    @field_validator("hermes_allowed_hosts", mode="before")
    @classmethod
    def parse_hermes_allowed_hosts(cls, value: Any) -> list[str] | Any:
        if isinstance(value, str):
            if not value.strip():
                return []
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    @field_validator("cors_origins", mode="before")
    @classmethod
    def parse_cors_origins(cls, value: Any) -> list[str] | Any:
        if isinstance(value, str):
            if not value.strip():
                return []
            origins = [item.strip() for item in value.split(",") if item.strip()]
        else:
            origins = value
        if isinstance(origins, list) and "*" in origins:
            raise ValueError("CORS_ORIGINS must not contain * when credentials are enabled")
        return origins

    @field_validator("allowed_hosts", mode="before")
    @classmethod
    def parse_allowed_hosts(cls, value: Any) -> list[str] | Any:
        if isinstance(value, str):
            if not value.strip():
                return []
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @field_validator("prometheus_allowed_cidrs", mode="before")
    @classmethod
    def parse_prometheus_allowed_cidrs(cls, value: Any) -> list[str] | Any:
        if isinstance(value, str):
            if not value.strip():
                return []
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @property
    def is_production(self) -> bool:
        return self.environment.lower() == "production"

    @property
    def effective_api_docs_enabled(self) -> bool:
        if self.api_docs_enabled is None:
            return not self.is_production
        return self.api_docs_enabled

    @property
    def effective_system_database_url(self) -> str:
        return self.system_database_url or self.database_url

    @property
    def effective_tenant_database_url(self) -> str:
        return self.tenant_database_url or self.database_url

    @property
    def effective_public_database_url(self) -> str:
        return self.public_database_url or self.effective_system_database_url

    def _warn_if_null_pool_without_pgbouncer(self, *, name: str, url: str) -> None:
        if self.is_production and self.db_use_null_pool and "pgbouncer" not in url.lower():
            warnings.warn(
                "DB_USE_NULL_POOL=true is intended for PgBouncer/transaction-pooling "
                f"deployments. Verify {name} points at PgBouncer, not directly at Postgres.",
                stacklevel=2,
            )

    @property
    def effective_rate_limit_requests(self) -> int:
        if self.rate_limit_in_memory_enabled is None:
            return 0 if self.is_production else self.rate_limit_requests
        return self.rate_limit_requests if self.rate_limit_in_memory_enabled else 0

    def _validate_production_db_roles(self) -> None:
        if not self.is_production:
            return
        if not self.tenant_database_url:
            raise RuntimeError(
                "TENANT_DATABASE_URL is required when ENVIRONMENT=production. "
                "It must use a least-privilege role with NOSUPERUSER NOBYPASSRLS."
            )
        if not self.system_database_url:
            raise RuntimeError(
                "SYSTEM_DATABASE_URL is required when ENVIRONMENT=production. "
                "Use a separate role for system/worker duties; do not reuse the "
                "tenant HTTP role."
            )
        if self.tenant_database_url == self.system_database_url:
            raise RuntimeError(
                "TENANT_DATABASE_URL and SYSTEM_DATABASE_URL must be distinct in "
                "production so tenant HTTP traffic cannot accidentally run with "
                "system/admin database privileges."
            )

    def validate_common_runtime_settings(self) -> None:
        """Validate settings shared by API, worker, and CLI runtimes.

        Keep this free of API-only checks such as CORS/ALLOWED_HOSTS so worker
        containers fail fast for truly shared production risks without needing
        presentation-layer configuration.
        """
        if not self.database_url:
            raise RuntimeError("DATABASE_URL is required.")

        if not self.api_key_hash_secret:
            if self.is_production:
                raise RuntimeError(
                    "API_KEY_HASH_SECRET is required when ENVIRONMENT=production. "
                    "Generate one with `python -c 'import secrets; print(secrets.token_hex(32))'` "
                    "and set it before starting Atlas."
                )
            logger.warning(
                "API_KEY_HASH_SECRET is not set. API keys are stored as plain SHA-256. "
                "Set API_KEY_HASH_SECRET in production to enable HMAC key hashing."
            )
        elif self.is_production and not re.fullmatch(r"[0-9a-fA-F]{64,}", self.api_key_hash_secret):
            raise RuntimeError(
                "API_KEY_HASH_SECRET must be at least 64 hexadecimal characters when "
                "ENVIRONMENT=production. Generate one with: "
                "python -c 'import secrets; print(secrets.token_hex(32))'"
            )
        if self.api_key_hash_secret_previous and not re.fullmatch(
            r"[0-9a-fA-F]{64,}", self.api_key_hash_secret_previous
        ):
            raise RuntimeError(
                "API_KEY_HASH_SECRET_PREVIOUS must be at least 64 hexadecimal characters "
                "when set. Use this only for bounded secret-rotation cutovers."
            )

        self._validate_production_db_roles()

        self._warn_if_null_pool_without_pgbouncer(
            name="DATABASE_URL",
            url=self.database_url,
        )
        self._warn_if_null_pool_without_pgbouncer(
            name="SYSTEM_DATABASE_URL",
            url=self.effective_system_database_url,
        )
        self._warn_if_null_pool_without_pgbouncer(
            name="TENANT_DATABASE_URL",
            url=self.effective_tenant_database_url,
        )
        self._warn_if_null_pool_without_pgbouncer(
            name="PUBLIC_DATABASE_URL",
            url=self.effective_public_database_url,
        )

    def validate_api_runtime_settings(self) -> None:
        """Validate API-specific startup settings and emit actionable warnings."""
        self.validate_common_runtime_settings()

        if not self.cors_origins:
            warnings.warn(
                "CORS_ORIGINS is empty - all cross-origin requests will be blocked. "
                "Set CORS_ORIGINS in your .env if the frontend and API are on different origins.",
                stacklevel=2,
            )
        if self.is_production:
            if not self.allowed_hosts or "*" in self.allowed_hosts:
                raise RuntimeError(
                    "ALLOWED_HOSTS must be set to explicit hostnames when ENVIRONMENT=production. "
                    "Example: ALLOWED_HOSTS=api.example.com,127.0.0.1"
                )
            if self.effective_api_docs_enabled:
                raise RuntimeError(
                    "Interactive API docs are disabled by default in production. "
                    "Set API_DOCS_ENABLED=false, or expose docs only behind private auth/VPN."
                )
            if not self.security_headers_enabled:
                raise RuntimeError("SECURITY_HEADERS_ENABLED must remain true in production.")
            if not self.hsts_enabled:
                warnings.warn(
                    "HSTS_ENABLED is false. Enable it after HTTPS is configured end-to-end.",
                    stacklevel=2,
                )
            for origin in self.cors_origins:
                if origin.startswith("http://") and not (
                    origin.startswith("http://localhost") or origin.startswith("http://127.0.0.1")
                ):
                    raise RuntimeError(
                        "Production CORS origins must use https:// except localhost. "
                        f"Unsafe origin: {origin}"
                    )

        if (
            self.is_production
            and self.rate_limit_in_memory_enabled
            and self.rate_limit_requests > 0
        ):
            warnings.warn(
                "RATE_LIMIT_IN_MEMORY_ENABLED=true opts into a per-process limiter "
                "that is not sufficient by itself for multi-instance production. "
                "Prefer a Redis-backed limiter or delegate to your API gateway.",
                stacklevel=2,
            )
        if self.is_production and self.api_key_cache_ttl_seconds > 30:
            warnings.warn(
                f"API_KEY_CACHE_TTL_SECONDS={self.api_key_cache_ttl_seconds}. "
                "The in-process auth cache is per-instance; a revoked or role-changed "
                "key remains valid for up to this many seconds on each running process. "
                "Consider setting API_KEY_CACHE_TTL_SECONDS=0 for immediate revocation, "
                "or keep it at ≤5 seconds to bound the revocation window.",
                stacklevel=2,
            )
        if self.is_production and self.prometheus_metrics_enabled:
            if not self.prometheus_bearer_token and not self.prometheus_allowed_cidrs:
                raise RuntimeError(
                    "PROMETHEUS_METRICS_ENABLED=true requires either "
                    "PROMETHEUS_ALLOWED_CIDRS or PROMETHEUS_BEARER_TOKEN in production."
                )
            if self.prometheus_bearer_token and len(self.prometheus_bearer_token) < 32:
                raise RuntimeError(
                    "PROMETHEUS_BEARER_TOKEN must be at least 32 characters in production. "
                    "Generate one with: python -c 'import secrets; print(secrets.token_hex(32))'"
                )
            if any(cidr in {"0.0.0.0/0", "::/0"} for cidr in self.prometheus_allowed_cidrs):
                if not self.prometheus_bearer_token:
                    raise RuntimeError(
                        "PROMETHEUS_ALLOWED_CIDRS contains a public-wide CIDR (0.0.0.0/0 "
                        "or ::/0) without a bearer token. This exposes internal metrics "
                        "publicly. Either restrict PROMETHEUS_ALLOWED_CIDRS to internal "
                        "VPC/cluster CIDRs, or set PROMETHEUS_BEARER_TOKEN to secure "
                        "public scraping. To explicitly allow public unauthenticated "
                        "scraping, set PROMETHEUS_BEARER_TOKEN to any non-empty value "
                        "and configure your scraper accordingly."
                    )
                warnings.warn(
                    "PROMETHEUS_ALLOWED_CIDRS allows public scraping. "
                    "A bearer token is configured, but prefer internal VPC/cluster CIDRs "
                    "to reduce the metrics attack surface.",
                    stacklevel=2,
                )

    def validate_worker_runtime_settings(self) -> None:
        """Validate worker-specific startup settings."""
        self.validate_common_runtime_settings()

    def validate_hermes_worker_settings(self) -> None:
        """Validate settings required when the Hermes crawler worker starts.

        Called by ``atlas hermes-worker`` at startup.  Enforces that production
        deployments set ``HERMES_ALLOWED_HOSTS`` so the fetcher has an explicit
        allowlist rather than relying on IP-range deny rules alone.  DNS
        rebinding attacks can bypass deny-list-only SSRF protections; an
        allowlist means the attacker must also control a hostname Atlas has
        explicitly trusted.
        """
        self.validate_common_runtime_settings()

        if self.is_production and not self.hermes_allowed_hosts:
            raise RuntimeError(
                "HERMES_ALLOWED_HOSTS must be set when running the Hermes worker "
                "in production (ENVIRONMENT=production).  "
                "Set it to a comma-separated list of domains Atlas is permitted "
                "to crawl, e.g. HERMES_ALLOWED_HOSTS=aviation-safety.net,ntsb.gov.  "
                "Without an allowlist, Atlas relies on pinned-DNS public-IP "
                "validation and egress controls alone.  "
                "Set HERMES_ALLOWED_HOSTS= (empty) to explicitly opt out of this "
                "check in non-production environments."
            )

    def warn_if_insecure(self) -> None:
        """Backward-compatible alias for API startup validation."""
        self.validate_api_runtime_settings()


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
