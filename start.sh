#!/usr/bin/env bash
set -e

cd "$(dirname "$0")"

export PYTHONPATH="src"

# Build the asyncpg URL from whatever DATABASE_URL is set in the environment.
# Replit sets DATABASE_URL as postgresql://, but SQLAlchemy async needs postgresql+asyncpg://.
# asyncpg does not accept ?sslmode=disable — strip it; local Helium DB doesn't need SSL.
RAW_URL="${DATABASE_URL}"
ASYNC_URL="${RAW_URL/postgresql:\/\//postgresql+asyncpg://}"
ASYNC_URL="${ASYNC_URL/postgresql+psycopg2:\/\//postgresql+asyncpg://}"
# Strip ?sslmode=... and &sslmode=... parameters (asyncpg uses ssl= not sslmode=)
ASYNC_URL=$(echo "$ASYNC_URL" | sed 's/[?&]sslmode=[^&]*//' | sed 's/\?&/\?/' | sed 's/[?&]$//')

export DATABASE_URL="$ASYNC_URL"

# Sync URL for Alembic (psycopg2, with sslmode intact)
SYNC_URL="${RAW_URL/postgresql+asyncpg:\/\//postgresql://}"
SYNC_URL="${SYNC_URL/postgresql+psycopg2:\/\//postgresql://}"
export DATABASE_SYNC_URL="$SYNC_URL"

# Use PORT env if set (Replit workflow), otherwise default to 8000
PORT="${PORT:-8000}"

echo "Starting Atlas backend on port $PORT"
echo "DATABASE_URL driver: $(echo "$DATABASE_URL" | cut -d: -f1)"

exec uvicorn atlas.presentation.api.app:app \
  --host 0.0.0.0 \
  --port "$PORT" \
  --log-level warning \
  --no-access-log
