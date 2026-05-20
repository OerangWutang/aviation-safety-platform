# Aviation Safety Platform — Backend

A multi-tenant aviation safety incident reporting API built with Django and Django REST Framework.

This version treats reports as controlled safety workflow records rather than generic CRUD objects. It includes tenant scoping, aviation-specific report fields, role-gated workflow transitions, immutable report events, read-model refreshes, and Docker-based local/prod deployment options.

## Stack

| Layer | Technology |
|---|---|
| API | Django 5.x + Django REST Framework |
| Auth | Simple JWT with refresh-token rotation and blacklist support |
| Async | Celery + Redis |
| Database | PostgreSQL 16 in Docker; SQLite fallback for local/test use |
| Cache | Redis |
| Tests | pytest + pytest-django |
| Production serving | Gunicorn |

## Quick start

```bash
cp .env.example .env
docker-compose up --build
```

In another shell:

```bash
docker-compose exec api python manage.py migrate
docker-compose exec api python manage.py createsuperuser
docker-compose exec api pytest
```

## Local non-Docker checks

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python manage.py migrate
pytest -q
python manage.py check
```

`DATABASE_URL` now defaults to a local SQLite database if no `.env` file is present, which makes local checks and tests easier to run.

## Production compose example

A production-oriented compose file is included:

```bash
docker-compose -f docker-compose.prod.yml up --build
```

It uses Gunicorn for the API container and avoids exposing Postgres/Redis directly on host ports.

## API endpoints

| Method | Endpoint | Description |
|---|---|---|
| GET | `/api/v1/health/` | Public liveness check |
| GET | `/api/v1/health/live/` | Public liveness check |
| GET | `/api/v1/health/ready/` | Readiness check for database/cache |
| POST | `/api/v1/token/` | Obtain JWT token pair |
| POST | `/api/v1/token/refresh/` | Refresh JWT access token |
| GET | `/api/v1/reports/` | List tenant-scoped reports |
| POST | `/api/v1/reports/` | Create a draft report |
| GET | `/api/v1/reports/{id}/` | Report detail |
| PATCH | `/api/v1/reports/{id}/` | Update editable report content |
| POST | `/api/v1/reports/{id}/transition/` | Transition report status |
| GET | `/api/v1/reports/{id}/events/` | Immutable audit event history |

## Report workflow

Supported statuses:

- `draft`
- `ingested`
- `validation_failed`
- `under_review`
- `requires_revision`
- `approved_queued`
- `published`
- `rejected`
- `archived`

Workflow transitions are validated against `STATUS_TRANSITIONS` in `reports/models.py` and then role-gated:

| Transition type | Allowed users |
|---|---|
| Submit own draft/revision for review | Creator, safety officer, admin |
| Review outcomes such as approve/reject/requires revision | Safety officer, admin |
| Archive published/rejected reports | Admin |

Direct content edits are allowed only while a report is in `draft` or `requires_revision`. Every content edit increments the report version, refreshes the read model, and writes an immutable `ReportEvent`.

## Aviation report fields

Reports now include domain-specific safety metadata:

- occurrence category
- severity
- risk level
- phase of flight
- location
- ICAO airport code
- aircraft registration
- aircraft type
- flight number
- confidentiality flag
- assigned reviewer
- review due date
- investigation summary
- corrective actions

## Tracking numbers

New reports use organization/year scoped tracking numbers:

```text
ASP-{ORGSLUG}-{YEAR}-{SEQUENCE}
```

Example:

```text
ASP-TESTORG-2026-000001
```

The sequence is stored in the database and locked during allocation to reduce collision risk under concurrent report creation.

## Audit events

Each important report action records a `ReportEvent`, including:

- creation
- content updates
- assignment changes
- status transitions

Events store actor, previous status, new status, comment, metadata, report version, and timestamp.

## Tests

Current test coverage includes:

- tenant scoping
- report creation
- aviation fields
- role-gated approval
- invalid transition rejection
- submit validation
- content update versioning
- audit event creation
- event endpoint scoping
- health endpoint
- custom user admin forms

Run:

```bash
pytest -q
```

Expected result for this version:

```text
15 passed
```
