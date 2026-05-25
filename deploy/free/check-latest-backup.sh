#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
BACKUP_DIR="${1:-backups}"
MAX_AGE_HOURS="${ATLAS_BACKUP_MAX_AGE_HOURS:-48}"
if [[ ! -d "$BACKUP_DIR" ]]; then
  echo "ERROR: backup directory '$BACKUP_DIR' does not exist." >&2
  exit 2
fi
latest=$(find "$BACKUP_DIR" -maxdepth 1 -type f -name 'atlas-*.sql.gz' -printf '%T@ %p\n' | sort -nr | head -1 | awk '{print $2}')
if [[ -z "${latest:-}" ]]; then
  echo "ERROR: no atlas-*.sql.gz backups found in '$BACKUP_DIR'." >&2
  exit 2
fi
now=$(date +%s)
mtime=$(stat -c %Y "$latest")
age_hours=$(( (now - mtime) / 3600 ))
if (( age_hours > MAX_AGE_HOURS )); then
  echo "ERROR: newest backup is ${age_hours}h old, exceeding ${MAX_AGE_HOURS}h: $latest" >&2
  exit 2
fi
echo "Latest backup is fresh (${age_hours}h old): $latest"
