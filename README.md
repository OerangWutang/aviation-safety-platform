# Aviation Safety Platform — Backend

A multi-tenant aviation safety incident reporting platform.

## Stack

| Layer | Technology |
|---|---|
| API | Django 5.x + Django REST Framework |
| Auth | Simple JWT |
| Async | Celery + Redis |
| Database | PostgreSQL 16 |
| Cache | Redis |
| Tests | pytest + pytest-django |

## Quick start

```bash
cp .env.example .env
docker-compose up --build
docker-compose exec api python manage.py migrate
docker-compose exec api python manage.py createsuperuser
docker-compose exec api pytest
```

## API endpoints

| Method | Endpoint | Description |
|---|---|---|
| GET | `/api/v1/health/` | Health check (public) |
| POST | `/api/v1/token/` | Obtain JWT token pair |
| POST | `/api/v1/token/refresh/` | Refresh JWT access token |
| GET | `/api/v1/reports/` | List tenant reports |
| POST | `/api/v1/reports/` | Create a draft report |
| GET | `/api/v1/reports/{id}/` | Report detail |
| POST | `/api/v1/reports/{id}/transition/` | Transition report status |

## Phase roadmap

- **Phase 1** (current): Auth, organizations, users, reports, read models, Celery, tests
- **Phase 2**: IngestionPayload, OutboxEvent, S3/MinIO, PgBouncer
- **Phase 3**: Debezium CDC, event-driven cache invalidation
- **Phase 4**: pgvector embeddings, HNSW index, semantic search
- **Phase 5**: Taxonomy nodes, location hierarchy, ltree + PostGIS
