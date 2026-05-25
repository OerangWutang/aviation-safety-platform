# Atlas Backend

Claims-based aviation accident data-integrity backend. See [ARCHITECTURE.md](ARCHITECTURE.md) for a detailed design overview.

> **Core philosophy:** The projected public record is not the truth. The evidence chain is the truth. The projection is the current best explanation of that evidence.

## Stack

- Python 3.12+ · FastAPI · SQLAlchemy async · PostgreSQL/PostGIS · Alembic · Typer CLI · Pytest

---

## Quick start (Docker)

```bash
cp .env.example .env          # Fill in POSTGRES_PASSWORD, API_KEY_HASH_SECRET, etc.
docker compose up -d db       # Start PostGIS container
alembic upgrade head          # Apply all migrations
atlas bootstrap               # Seed CuratorOverride source + print a dev API key
uvicorn atlas.presentation.api.app:app --reload
```

Or run the API container too. Apply migrations and bootstrap before starting
the API, because startup readiness checks require the schema and
`CuratorOverride` source to exist:

```bash
docker compose up -d db
docker compose --profile full build api
docker compose --profile full run --rm api alembic upgrade head
docker compose --profile full run --rm api atlas bootstrap
docker compose --profile full up -d api
```

---

## Local development (no Docker)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'
cp .env.example .env          # Fill in DATABASE_URL pointing to a local PG instance
alembic upgrade head
atlas bootstrap               # Seed data + API key
make test-unit                # Fast unit tests, no DB required
```

---

## Common tasks

| Task | Command |
|---|---|
| Run unit tests | `make test-unit` |
| Run all tests | `make test` |
| Run integration tests (needs DB) | `make test-integration` |
| Lint | `make lint` |
| Type check | `make typecheck` |
| All checks | `make check` |
| Apply migrations | `make migrate` |
| Create API key | `atlas bootstrap --role admin` |
| Process outbox once | `atlas outbox-process --limit 100` |
| Start outbox worker | `atlas outbox-worker --sleep-seconds 5` |
| Start Hermes fetch worker | `atlas hermes-worker --sleep-seconds 5 --batch-limit 5` |
| Rebuild projection | `atlas projections-rebuild --event-id <uuid>` |
| Rebuild all | `atlas projections-rebuild --all` |

---

## API

All endpoints except `/health` and `/ready` require `X-API-Key`, including routes under `/api/v1/public`. In this codebase, "public" means the published/read-only Atlas record, not anonymous internet access.

| Method | Path | Description |
|---|---|---|
| `GET` | `/health` | Liveness check |
| `GET` | `/ready` | Readiness check (tests DB connectivity) |
| `POST` | `/api/v1/ingestion/sources/{source_id}` | Ingest claims from a source |
| `GET` | `/api/v1/accidents/{event_id}` | Public projection view |
| `GET` | `/api/v1/accidents/{event_id}/provenance` | Full evidence chain |
| `GET` | `/api/v1/conflicts` | List conflicts for an event (`?event_id=…`) |
| `GET` | `/api/v1/conflicts/{conflict_id}` | Get conflict detail |
| `GET` | `/api/v1/conflicts/{conflict_id}/history` | Conflict activity log, keyset-paginated with `?limit=50&cursor=...` |
| `POST` | `/api/v1/conflicts/{conflict_id}/resolve` | Resolve a conflict |
| `POST` | `/api/v1/conflicts/{conflict_id}/reopen` | Reopen a resolved conflict |
| `POST` | `/api/v1/admin/projections/rebuild` | Rebuild projections |

Interactive docs are available at `/docs` (Swagger) and `/redoc` in development. They are disabled by default when `ENVIRONMENT=production`.

---

## Free self-hosted deployment

For a zero-cost VPS or homelab deployment, start with `deploy/free/`. It exposes
only Caddy on ports 80/443 and keeps Postgres, Redis, PgBouncer, the API, and
the outbox worker private on the Docker network. See `deploy/free/README.md`.


## Environment variables

See [`.env.example`](.env.example) for a full annotated reference.

Required for API/worker runtime:

| Variable | Description |
|---|---|
| `DATABASE_URL` | Development fallback async SQLAlchemy URL (`postgresql+asyncpg://…`) |
| `TENANT_DATABASE_URL` | Production request-path DB URL. Must use a `NOSUPERUSER NOBYPASSRLS` role so tenant RLS is enforceable. Required in production. |
| `SYSTEM_DATABASE_URL` | Production system/worker/admin DB URL. Must be distinct from `TENANT_DATABASE_URL`. Required in production. |
| `PUBLIC_DATABASE_URL` | Optional separate public Atlas event-store URL for split-topology deployments. Falls back to `SYSTEM_DATABASE_URL`/`DATABASE_URL` when unset. |

Required for migrations / local Docker Compose:

| Variable | Description |
|---|---|
| `DATABASE_SYNC_URL` | Sync URL for default/system/SMS Alembic migrations (`postgresql://…`) |
| `PUBLIC_DATABASE_SYNC_URL` | Sync URL for public DB migrations when running `ATLAS_MIGRATION_TARGET=public alembic upgrade head` |
| `POSTGRES_USER` | PG username for Docker Compose bootstrap |
| `POSTGRES_PASSWORD` | PG password for Docker Compose bootstrap |
| `POSTGRES_DB` | PG database name for Docker Compose bootstrap |

Important optional:

| Variable | Default | Description |
|---|---|---|
| `API_KEY_HASH_SECRET` | *(none)* | Enables HMAC-SHA256 key hashing. **Set in production.** |
| `API_KEY_HASH_SECRET_PREVIOUS` | *(none)* | Optional verification-only bridge for bounded key-secret rotation cutovers. |
| `CORS_ORIGINS` | *(empty)* | Comma-separated allowed origins |
| `LOG_LEVEL` | `INFO` | Python log level |
| `OUTBOX_MAX_ATTEMPTS` | `5` | Delivery attempts before dead-letter |

---

## Migrations

```bash
# Apply all migrations (idempotent)
alembic upgrade head

# Create a new migration
alembic revision -m "describe_the_change"

# Verify from scratch against a disposable database only
ATLAS_ALLOW_DB_RESET=1 make migrate-check
```

Migrations are in `alembic/versions/`. The current head is `049_fk_covering_indexes`.

---

## Tests

```bash
make test-unit         # Fast: unit + API smoke (no DB, ~1 s)
make test-integration  # Requires running postgres + make migrate
make test-cov          # With HTML coverage report → htmlcov/
```

Integration tests are skipped by default. Enable them with `pytest --run-integration`.

---

## Operations

Production process/container guidance, first-boot sequencing, Prometheus metrics, PgBouncer notes, and k6 stress tests are documented in [OPERATIONS.md](OPERATIONS.md). Baseline alert rules live in `ops/alerts/prometheus-atlas-rules.yml`, API key secret rotation procedures are documented in `ops/runbooks/api-key-hash-secret-rotation.md`, and full cutover sequencing/sign-off is in `ops/runbooks/production-cutover-checklist.md`.

## Architecture

See [ARCHITECTURE.md](ARCHITECTURE.md) for:
- Ingestion flow
- Conflict lifecycle (OPEN → RESOLVED → OPEN)
- Outbox worker state machine with exponential backoff
- Projection builder logic
- Transaction boundary rules
- Auth role matrix


## Free self-hosted deployment safety

For a completely free VPS/homelab deployment, use `deploy/free/` rather than the local development compose file. Before starting, run:

```bash
cd deploy/free
./check-env.sh .env
docker compose --env-file .env up -d
```

The preflight check refuses placeholder secrets, wildcard production hosts, public Prometheus CIDRs, and non-HTTPS CORS origins. The compose files also cap Docker JSON logs so a noisy container cannot silently fill the disk.
