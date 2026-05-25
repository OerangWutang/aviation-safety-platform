#!/usr/bin/env sh
set -eu

THRESHOLD_PERCENT="${DISK_ALERT_PERCENT:-85}"
TARGET_PATH="${1:-.}"

usage_percent=$(df -P "$TARGET_PATH" | awk 'NR==2 {gsub("%", "", $5); print $5}')
if [ -z "$usage_percent" ]; then
  echo "ERROR: unable to read disk usage for $TARGET_PATH" >&2
  exit 1
fi

if [ "$usage_percent" -ge "$THRESHOLD_PERCENT" ]; then
  echo "ERROR: disk usage for $TARGET_PATH is ${usage_percent}% (threshold ${THRESHOLD_PERCENT}%)." >&2
  echo "Run docker system df, rotate/move backups, or expand disk before restarting Atlas." >&2
  exit 1
fi

echo "Disk usage for $TARGET_PATH is ${usage_percent}% (threshold ${THRESHOLD_PERCENT}%)."
