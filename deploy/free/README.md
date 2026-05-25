# Atlas free/self-hosted deployment

This deployment is for a single free VPS or homelab server. It exposes only
Caddy on ports 80/443. Postgres, Redis, PgBouncer, the API, and the outbox
worker remain private on the Docker network.

## First run

```bash
cp .env.example .env
# edit .env: domain, email, URL-safe DB values, strong secrets, and HERMES_ALLOWED_HOSTS
python -c 'import secrets; print(secrets.token_hex(32))'  # use for API_KEY_HASH_SECRET
docker build -t atlas-backend:local ../..
./check-env.sh .env
docker compose --env-file .env up -d
```

The compose stack also runs the same preflight as a one-shot `preflight`
service before Postgres, Redis, migrations, the API, or the worker start. This
keeps accidental direct `docker compose up` runs from bypassing the safety
checks.

Use URL-safe values for `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB`,
`ATLAS_TENANT_DB_USER`, `ATLAS_TENANT_DB_PASSWORD`, `ATLAS_SYSTEM_DB_USER`, and
`ATLAS_SYSTEM_DB_PASSWORD`: letters, numbers, `.`, `_`, `~`, and `-` only. The
free compose file assembles Postgres URLs from these values, so characters like
`@`, `/`, `:`, `?`, and `#` must not be used unless you introduce URL-encoded
variables. All database passwords must be at least 24 characters, and
`API_KEY_HASH_SECRET` should be a 64-character hex string from
`secrets.token_hex(32)`.
`API_KEY_HASH_SECRET_PREVIOUS` is optional and should be set only during a
bounded key-secret rotation window.

The stack now creates three database roles. `POSTGRES_USER` is the
migration/admin owner and is not used by the API or workers.
`ATLAS_TENANT_DB_USER` is a `NOSUPERUSER NOBYPASSRLS` runtime role used for
tenant-scoped HTTP work. `ATLAS_SYSTEM_DB_USER` is a separate `NOSUPERUSER
BYPASSRLS` role used by workers/admin paths that legitimately need to cross
tenant RLS boundaries. The `db-roles` and `migrate` one-shot services create
those roles and grant schema privileges before the API starts.

`ALLOWED_HOSTS` must include your public `ATLAS_DOMAIN`. The example also
includes `127.0.0.1` and `localhost` so local health checks and SSH-tunnel
smoke tests do not fight the Host-header middleware.

`HERMES_ALLOWED_HOSTS` is also required because this compose stack starts the
Hermes crawler worker. Set it to the exact comma-separated domains this
deployment may crawl, for example `aviation-safety.net,ntsb.gov`. The preflight
script rejects an empty value so the worker cannot pass preflight and then
crash-loop at startup.

## Create the first admin API key

After the stack is healthy, create your first admin key inside the API
container:

```bash
docker compose --env-file .env run --rm api atlas bootstrap --role admin
```

Copy the printed API key into your password manager. Atlas stores only its hash
and cannot show it again.

## Backups

```bash
./backup-postgres.sh
./check-latest-backup.sh  # fails if the newest local backup is missing/stale
```

Store backups off the server periodically. A free VM is not a backup strategy.

### Backup scheduling example

See `examples/backup-cron.example` for a simple nightly cron entry,
`examples/OFFSERVER_BACKUPS.md` for rclone/restic examples, and
`examples/RESTORE_DRILL.md` for a restore drill checklist. Restores require
`ATLAS_RESTORE_CONFIRM=1`; restoring into a non-empty database also requires
`ATLAS_RESTORE_ALLOW_DIRTY_DB=1`. Prefer disposable restore drills before trusting
the deployment with real data.

## Security notes

- Do not publish Postgres or Redis ports. This compose file intentionally does
  not bind them to the host.
- `/metrics` is blocked publicly by Caddy and also gated in the app.
- `DB_USE_NULL_POOL=true` lets PgBouncer own connection pooling for system
  traffic. Tenant traffic uses the least-privilege tenant role directly; the
  app may warn that the tenant URL is not PgBouncer-backed, which is expected
  for this free topology.
- `EDGE_RATE_LIMIT_OWNER` must explicitly name the edge control enforcing
  public rate limits (`caddy`, `cloudflare`, `nginx`, etc.). The preflight
  script fails if this is unset so disabling the in-app limiter cannot happen
  silently.
- Run `alembic upgrade head` through the `migrate` one-shot service, which
  connects directly to Postgres as the migration/admin owner and then grants
  privileges to the tenant/system runtime roles.


## Log rotation and disk pressure

The compose file limits Docker JSON logs to 5 files of 10 MiB per service. This is intentionally conservative for free VPS disks. Still monitor disk usage, prune old local dumps with `./prune-backups.sh`, and move compressed Postgres backups off the server.

## Admin maintenance safety

The API refuses unlimited projection rebuilds through HTTP in production by default. Use bounded `max_events` values for routine maintenance. Only set `ADMIN_ALLOW_UNBOUNDED_PROJECTION_REBUILDS=true` during a deliberate maintenance window.

Before and after large backups, check disk pressure:

```bash
./check-disk.sh .
```
