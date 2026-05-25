#!/usr/bin/env sh
set -eu

BACKUP_DIR="${1:-backups}"
RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-14}"

if [ ! -d "$BACKUP_DIR" ]; then
  echo "No backup directory found at $BACKUP_DIR; nothing to prune."
  exit 0
fi

case "$RETENTION_DAYS" in
  ''|*[!0-9]*)
    echo "ERROR: BACKUP_RETENTION_DAYS must be a positive integer." >&2
    exit 1
    ;;
esac

find "$BACKUP_DIR" -type f -name 'atlas-*.sql.gz' -mtime "+$RETENTION_DAYS" -print -delete
