# Aviation Safety Platform

A Django-based platform for managing aviation safety reports and workflows.

## Features

- **Safety Report Management**: Create, track, and manage aviation safety reports
- **Workflow Automation**: Automated report processing with Celery tasks
- **Multi-Organization Support**: Manage multiple aviation organizations
- **Role-Based Access Control**: Fine-grained permissions per organization
- **Audit Trail**: Complete audit logging for compliance
- **REST API**: Full API with JWT authentication

## Tech Stack

- **Backend**: Django 4.2 + Django REST Framework
- **Database**: PostgreSQL
- **Cache/Queue**: Redis + Celery
- **Auth**: JWT via djangorestframework-simplejwt
- **Containerization**: Docker + Docker Compose

## Quick Start

```bash
# Clone the repository
git clone https://github.com/yourusername/aviation-safety-platform.git
cd aviation-safety-platform

# Copy environment variables
cp .env.example .env

# Start with Docker Compose
docker-compose up --build

# Run migrations
docker-compose exec web python manage.py migrate

# Create superuser
docker-compose exec web python manage.py createsuperuser
```

## API Endpoints

- `GET /api/reports/` - List safety reports
- `POST /api/reports/` - Create a new report
- `GET /api/reports/{id}/` - Get report details
- `PATCH /api/reports/{id}/` - Update a report
- `POST /api/reports/{id}/submit/` - Submit report for review
- `POST /api/reports/{id}/approve/` - Approve a report
- `GET /api/organizations/` - List organizations
- `GET /health/` - Health check endpoint

## Running Tests

```bash
docker-compose exec web pytest
```

## License

MIT
