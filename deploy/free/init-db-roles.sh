#!/usr/bin/env sh
set -eu

: "${POSTGRES_USER:?POSTGRES_USER is required}"
: "${POSTGRES_PASSWORD:?POSTGRES_PASSWORD is required}"
: "${POSTGRES_DB:?POSTGRES_DB is required}"
: "${ATLAS_TENANT_DB_USER:?ATLAS_TENANT_DB_USER is required}"
: "${ATLAS_TENANT_DB_PASSWORD:?ATLAS_TENANT_DB_PASSWORD is required}"
: "${ATLAS_SYSTEM_DB_USER:?ATLAS_SYSTEM_DB_USER is required}"
: "${ATLAS_SYSTEM_DB_PASSWORD:?ATLAS_SYSTEM_DB_PASSWORD is required}"

export PGPASSWORD="$POSTGRES_PASSWORD"

psql -h db -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 <<SQL
DO \$\$
DECLARE
  db_name text := '${POSTGRES_DB}';
  tenant_user text := '${ATLAS_TENANT_DB_USER}';
  tenant_password text := '${ATLAS_TENANT_DB_PASSWORD}';
  system_user text := '${ATLAS_SYSTEM_DB_USER}';
  system_password text := '${ATLAS_SYSTEM_DB_PASSWORD}';
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = tenant_user) THEN
    EXECUTE format('CREATE ROLE %I LOGIN PASSWORD %L NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS', tenant_user, tenant_password);
  ELSE
    EXECUTE format('ALTER ROLE %I LOGIN PASSWORD %L NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS', tenant_user, tenant_password);
  END IF;

  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = system_user) THEN
    EXECUTE format('CREATE ROLE %I LOGIN PASSWORD %L NOSUPERUSER NOCREATEDB NOCREATEROLE BYPASSRLS', system_user, system_password);
  ELSE
    EXECUTE format('ALTER ROLE %I LOGIN PASSWORD %L NOSUPERUSER NOCREATEDB NOCREATEROLE BYPASSRLS', system_user, system_password);
  END IF;

  EXECUTE format('GRANT CONNECT ON DATABASE %I TO %I', db_name, tenant_user);
  EXECUTE format('GRANT CONNECT ON DATABASE %I TO %I', db_name, system_user);
END
\$\$;
SQL
