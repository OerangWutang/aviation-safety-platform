# Aviation Safety Platform Backend

Multi-tenant Django REST backend for aviation safety incident reporting with ingestion, CQRS read models, outbox events, and async workers.

## Stack

- Django 5 + Django REST Framework
- PostgreSQL (through PgBouncer), Redis, Celery, MinIO, Debezium
- JWT auth (`djangorestframework-simplejwt`)
- pgvector model support + HNSW migration

## Project layout

- `core/`: shared model base, tenant middleware, permissions, cache key helper
- `organizations/`: organization tenant model
- `users/`: custom `AppUser` auth model
- `ingestion/`: payload ingestion + outbox + parsing tasks
- `reports/`: reports, reviews, attachments, read models, review APIs
- `aircraft/`: aircraft reference models and report linkage
- `taxonomy/`: taxonomy/location hierarchies
- `vectors/`: report vector chunks + embeddings task
- `config/settings/`: `base.py`, `local.py`, `production.py`, `test.py`

## Environment

Copy `.env.example` to `.env` and adjust values.

Important variables:

- `DATABASE_URL` should point to PgBouncer (`pgbouncer:5432` inside compose, `localhost:5433` from host)
- `REDIS_URL`
- `S3_*` MinIO/S3 config
- `DJANGO_SETTINGS_MODULE` (default: `config.settings.local`)

## Run locally with Docker Compose

```bash
docker-compose up --build
```

Then run migrations in another shell:

```bash
docker-compose exec api python manage.py makemigrations
docker-compose exec api python manage.py migrate
docker-compose exec api python manage.py createsuperuser
```

API base URL: `http://localhost:8000/api/v1/`

## Local (without Docker)

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python manage.py makemigrations
python manage.py migrate
python manage.py runserver
```

## Auth

- `POST /api/v1/auth/token/` -> obtain access/refresh JWT
- JWT includes `organization_id` and `role` claims

## Key endpoints

- `GET /api/v1/health/`
- `POST /api/v1/ingest/`
- `GET|POST /api/v1/reports/`
- `GET|PATCH /api/v1/reports/{id}/`
- `POST /api/v1/reports/{id}/reviews/`
- `GET /api/v1/taxonomy/`
- `GET /api/v1/locations/`
- `GET /api/v1/aircraft/`

## Celery

- Worker: `celery -A config worker -l info`
- Beat: `celery -A config beat -l info`
- Includes recovery poller task (`requeue_stuck_outbox_events`) every 2 minutes

## Tests

Pytest is configured (`pytest.ini`) with Django settings module `config.settings.test`.

Run:

```bash
pytest
```

Included baseline tests:

- health-check endpoint test
- authenticated report creation test
