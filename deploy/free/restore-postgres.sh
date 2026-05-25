#!/usr/bin/env bash
set -euo pipefail
if [[ $# -ne 1 ]]; then
  echo "Usage: ATLAS_RESTORE_CONFIRM=1 $0 backups/atlas-YYYYMMDDTHHMMSSZ.sql.gz" >&2
  exit 2
fi
if [[ "${ATLAS_RESTORE_CONFIRM:-}" != "1" ]]; then
  cat >&2 <<'MSG'
ERROR: restore-postgres.sh writes directly into the configured Postgres database.

Set ATLAS_RESTORE_CONFIRM=1 when you are intentionally restoring, and prefer
running restore drills against a fresh disposable database/VM first.

Example:
  ATLAS_RESTORE_CONFIRM=1 ./restore-postgres.sh backups/atlas-YYYYMMDDTHHMMSSZ.sql.gz
MSG
  exit 2
fi
cd "$(dirname "$0")"

USER_TABLE_COUNT=$(docker compose --env-file .env exec -T db sh -c \
  'psql -U "$POSTGRES_USER" "$POSTGRES_DB" -Atc "SELECT count(*) FROM information_schema.tables WHERE table_schema = '\''public'\'' AND table_type = '\''BASE TABLE'\'' AND table_name NOT IN ('\''spatial_ref_sys'\'')"')

if [[ "${USER_TABLE_COUNT}" != "0" && "${ATLAS_RESTORE_ALLOW_DIRTY_DB:-}" != "1" ]]; then
  cat >&2 <<MSG
ERROR: target database is not empty (${USER_TABLE_COUNT} public base tables found).

Restoring into a dirty database can create duplicate rows or mixed schema/data
state. Prefer restoring into a fresh database/VM. If you intentionally want to
restore into the current database, rerun with:

  ATLAS_RESTORE_CONFIRM=1 ATLAS_RESTORE_ALLOW_DIRTY_DB=1 $0 $1
MSG
  exit 2
fi

gzip -dc "$1" | docker compose --env-file .env exec -T db sh -c 'psql -U "$POSTGRES_USER" "$POSTGRES_DB"'
