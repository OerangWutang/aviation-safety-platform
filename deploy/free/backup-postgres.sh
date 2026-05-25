#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p backups
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
docker compose --env-file .env exec -T db sh -c 'pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB"' | gzip > "backups/atlas-${stamp}.sql.gz"
echo "Wrote backups/atlas-${stamp}.sql.gz"
