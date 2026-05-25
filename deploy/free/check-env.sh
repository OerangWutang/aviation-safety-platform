#!/usr/bin/env sh
set -eu

ENV_FILE="${1:-.env}"
case "$ENV_FILE" in
  */*) ;;
  *) ENV_FILE="./$ENV_FILE" ;;
esac

if [ ! -f "$ENV_FILE" ]; then
  echo "ERROR: $ENV_FILE not found. Copy .env.example to .env and edit it first." >&2
  exit 1
fi

# shellcheck disable=SC1090
set -a
. "$ENV_FILE"
set +a

fail() {
  echo "ERROR: $1" >&2
  exit 1
}

is_placeholder() {
  case "${1:-}" in
    ""|change-me|change-me-*|*example.com*|atlas|password|secret|changeme)
      return 0
      ;;
    *)
      return 1
      ;;
  esac
}

require_url_safe() {
  name="$1"
  value="$2"
  case "$value" in
    *[!A-Za-z0-9._~.-]*)
      fail "$name must contain only URL-safe characters [A-Za-z0-9._~.-]. Database URLs are assembled from these values. Generate a safe password with: python -c 'import secrets, string; a=string.ascii_letters+string.digits; print(\"\".join(secrets.choice(a) for _ in range(32)))'"
      ;;
  esac
}

require_min_length() {
  name="$1"
  value="$2"
  min="$3"
  if [ "${#value}" -lt "$min" ]; then
    fail "$name must be at least $min characters long."
  fi
}

require_hex_secret() {
  name="$1"
  value="$2"
  min="$3"
  require_min_length "$name" "$value" "$min"
  non_hex="$(printf '%s' "$value" | tr -d '0123456789abcdefABCDEF')"
  [ -z "$non_hex" ] || fail "$name must be hexadecimal. Generate it with: python -c 'import secrets; print(secrets.token_hex(32))'"
}

require_csv_contains() {
  name="$1"
  csv="$2"
  expected="$3"
  old_ifs="$IFS"
  IFS=','
  for item in $csv; do
    trimmed="$(printf '%s' "$item" | tr -d '[:space:]')"
    if [ "$trimmed" = "$expected" ]; then
      IFS="$old_ifs"
      return 0
    fi
  done
  IFS="$old_ifs"
  fail "$name must include $expected."
}

[ "${ENVIRONMENT:-production}" = "production" ] || fail "ENVIRONMENT must be production for deploy/free."

is_placeholder "${ATLAS_DOMAIN:-}" && fail "ATLAS_DOMAIN must be set to your real domain."
is_placeholder "${ACME_EMAIL:-}" && fail "ACME_EMAIL must be set to your real email."
is_placeholder "${POSTGRES_PASSWORD:-}" && fail "POSTGRES_PASSWORD must be changed from the example value."
is_placeholder "${ATLAS_TENANT_DB_USER:-}" && fail "ATLAS_TENANT_DB_USER must be set."
is_placeholder "${ATLAS_TENANT_DB_PASSWORD:-}" && fail "ATLAS_TENANT_DB_PASSWORD must be changed from the example value."
is_placeholder "${ATLAS_SYSTEM_DB_USER:-}" && fail "ATLAS_SYSTEM_DB_USER must be set."
is_placeholder "${ATLAS_SYSTEM_DB_PASSWORD:-}" && fail "ATLAS_SYSTEM_DB_PASSWORD must be changed from the example value."
is_placeholder "${API_KEY_HASH_SECRET:-}" && fail "API_KEY_HASH_SECRET must be changed from the example value."
is_placeholder "${EDGE_RATE_LIMIT_OWNER:-}" && fail "EDGE_RATE_LIMIT_OWNER must name who enforces production edge rate limiting (for example: caddy, cloudflare, nginx, or api-gateway)."

[ "${POSTGRES_PASSWORD:-}" != "${POSTGRES_USER:-}" ] || fail "POSTGRES_PASSWORD must not equal POSTGRES_USER."
[ "${POSTGRES_USER:-}" != "${ATLAS_TENANT_DB_USER:-}" ] || fail "POSTGRES_USER and ATLAS_TENANT_DB_USER must be distinct."
[ "${POSTGRES_USER:-}" != "${ATLAS_SYSTEM_DB_USER:-}" ] || fail "POSTGRES_USER and ATLAS_SYSTEM_DB_USER must be distinct."
[ "${ATLAS_TENANT_DB_USER:-}" != "${ATLAS_SYSTEM_DB_USER:-}" ] || fail "ATLAS_TENANT_DB_USER and ATLAS_SYSTEM_DB_USER must be distinct."
[ "${ATLAS_TENANT_DB_PASSWORD:-}" != "${ATLAS_SYSTEM_DB_PASSWORD:-}" ] || fail "Tenant and system DB passwords must be distinct."
require_url_safe "POSTGRES_USER" "${POSTGRES_USER:-}"
require_url_safe "POSTGRES_PASSWORD" "${POSTGRES_PASSWORD:-}"
require_url_safe "POSTGRES_DB" "${POSTGRES_DB:-}"
require_url_safe "ATLAS_TENANT_DB_USER" "${ATLAS_TENANT_DB_USER:-}"
require_url_safe "ATLAS_TENANT_DB_PASSWORD" "${ATLAS_TENANT_DB_PASSWORD:-}"
require_url_safe "ATLAS_SYSTEM_DB_USER" "${ATLAS_SYSTEM_DB_USER:-}"
require_url_safe "ATLAS_SYSTEM_DB_PASSWORD" "${ATLAS_SYSTEM_DB_PASSWORD:-}"
require_min_length "POSTGRES_PASSWORD" "${POSTGRES_PASSWORD:-}" 24
require_min_length "ATLAS_TENANT_DB_PASSWORD" "${ATLAS_TENANT_DB_PASSWORD:-}" 24
require_min_length "ATLAS_SYSTEM_DB_PASSWORD" "${ATLAS_SYSTEM_DB_PASSWORD:-}" 24
require_hex_secret "API_KEY_HASH_SECRET" "${API_KEY_HASH_SECRET:-}" 64
if [ -n "${API_KEY_HASH_SECRET_PREVIOUS:-}" ]; then
  require_hex_secret "API_KEY_HASH_SECRET_PREVIOUS" "${API_KEY_HASH_SECRET_PREVIOUS:-}" 64
fi
if [ -n "${PROMETHEUS_BEARER_TOKEN:-}" ]; then
  require_min_length "PROMETHEUS_BEARER_TOKEN" "${PROMETHEUS_BEARER_TOKEN:-}" 32
fi
[ "${RATE_LIMIT_REQUESTS:-0}" = "0" ] || fail "RATE_LIMIT_REQUESTS must be 0 in deploy/free; use Caddy or Cloudflare for public rate limiting."
case "${RATE_LIMIT_IN_MEMORY_ENABLED:-false}" in
  false|False|FALSE|0|no|NO) ;;
  *) fail "RATE_LIMIT_IN_MEMORY_ENABLED must stay false in deploy/free; use Caddy or Cloudflare for public rate limiting." ;;
esac
case "${EDGE_RATE_LIMIT_OWNER:-}" in
  *[[:space:]]*) fail "EDGE_RATE_LIMIT_OWNER must be a short identifier without spaces (for example: caddy, cloudflare, nginx)." ;;
esac
case "${HSTS_ENABLED:-}" in
  true|True|TRUE|1|yes|YES) ;;
  *) fail "HSTS_ENABLED must be true in deploy/free production preflight once HTTPS is configured end-to-end." ;;
esac
[ "${ALLOWED_HOSTS:-}" != "*" ] || fail "ALLOWED_HOSTS must not be '*' in production."
[ -n "${ALLOWED_HOSTS:-}" ] || fail "ALLOWED_HOSTS must be set."
require_csv_contains "ALLOWED_HOSTS" "${ALLOWED_HOSTS:-}" "${ATLAS_DOMAIN:-}"
[ -n "${HERMES_ALLOWED_HOSTS:-}" ] || fail "HERMES_ALLOWED_HOSTS must be set in production when hermes-worker is enabled."
case "${HERMES_ALLOWED_HOSTS:-}" in
  *[[:space:]]*) fail "HERMES_ALLOWED_HOSTS must be a comma-separated list without spaces." ;;
esac

case "${CORS_ORIGINS:-}" in
  *"*"*) fail "CORS_ORIGINS must not contain '*'." ;;
esac
case "${CORS_ORIGINS:-}" in
  *example.com*) fail "CORS_ORIGINS still contains the example.com placeholder." ;;
esac
# Validate each origin individually so a leading https:// origin cannot
# smuggle a plain http:// one later in the list.
if [ -n "${CORS_ORIGINS:-}" ]; then
  old_ifs="$IFS"
  IFS=','
  for _origin in ${CORS_ORIGINS}; do
    _origin="$(printf '%s' "$_origin" | tr -d '[:space:]')"
    case "$_origin" in
      http://localhost*|http://127.0.0.1*|https://*) ;;
      *) fail "Each CORS_ORIGINS entry must use https:// except localhost. Got: $_origin" ;;
    esac
  done
  IFS="$old_ifs"
fi

case "${PROMETHEUS_ALLOWED_CIDRS:-}" in
  *"0.0.0.0/0"*|*"::/0"*) fail "PROMETHEUS_ALLOWED_CIDRS must not allow public scraping." ;;
esac

printf 'Free deployment environment preflight passed for %s\n' "$ENV_FILE"
