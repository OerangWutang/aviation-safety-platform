# Tenant Isolation: Database-Enforced (RLS)

## Why this exists

Before this change, tenant isolation in the SMS was enforced **only in
application code** — the "three-layer rule" in repositories and use-cases. That
is a *procedural* guarantee. A single forgotten `WHERE tenant_id = …`, in
existing code or in a future feature, leaks one operator's protected safety data
to another tenant. The AI cross-reference engine is precisely such a future
feature: it reads across the public/private boundary by design, so it is the
worst possible place to rely on "we remembered the filter everywhere."

This makes the isolation **structural** — enforced by PostgreSQL itself, as
defense-in-depth *behind* the existing application checks (not a replacement).

## What migration 045 does

For each tenant **payload** table (`tenant_sources`, `tenant_ingestion_runs`,
`tenant_claims`, `tenant_event_overlays`, `tenant_safety_reports`,
`tenant_event_associations`), and additionally `tenant_crossref_results` (added
and RLS-protected inline in migration 046 using the same pattern):

```sql
ALTER TABLE <t> ENABLE ROW LEVEL SECURITY;
ALTER TABLE <t> FORCE  ROW LEVEL SECURITY;       -- owner is constrained too
CREATE POLICY tenant_isolation ON <t>
    USING      (tenant_id::text = current_setting('app.current_tenant_id', true))
    WITH CHECK (tenant_id::text = current_setting('app.current_tenant_id', true));
```

* **`FORCE`** means the guarantee survives even if the app connects as the table
  owner — owners are normally exempt from RLS.
* **`USING`** filters reads; **`WITH CHECK`** rejects cross-tenant writes.
* **Fail-closed:** `current_setting(key, true)` returns NULL when unset, so the
  predicate matches nothing. A request that forgets to establish tenant context
  sees **zero** tenant rows, never all of them.

## Bootstrap tables are intentionally excluded

`tenants` and `tenant_memberships` are the identity/auth tables read to
*establish* the tenant context in the first place. Putting them behind the same
GUC would deadlock auth (you'd need the context to read the table that tells you
the context). They stay guarded by application-layer auth + the membership
check. Only the tables holding tenant *payload* are RLS-covered.

## How the application sets context

`infrastructure/db/unit_of_work.py` adds:

* `set_tenant_context(session, tenant_id)` — issues
  `SELECT set_config('app.current_tenant_id', <id>, true)`. The `true` makes it
  **transaction-local**, which is mandatory under the PgBouncer
  transaction-pooling mode this app supports (`NullPool`): a session-level `SET`
  would leak context onto the next client multiplexed onto the same connection.
  The id is a bound parameter, never interpolated into SQL.
* `create_tenant_uow(tenant_id)` — a unit of work that establishes the context
  before yielding, so every statement runs isolated. Tenant request handlers
  should use this instead of `create_uow()`. A tenant UoW is one logical
  transaction (the request-scoped norm), because the context is transaction-local.

## Operational requirement (do not skip)

The guarantee holds **only** if the application connects as a role that is
**not a superuser and does not have `BYPASSRLS`**. Superusers and `BYPASSRLS`
roles ignore RLS entirely.

* **Application / request role:** `NOSUPERUSER NOBYPASSRLS`.
* **System / admin role (separate):** `BYPASSRLS`, used by the cross-tenant jobs
  that legitimately span tenants — the cross-reference indexer, platform admin,
  projection rebuilds, the NTSB importer (public-only anyway). Using a distinct
  role keeps every cross-tenant access **explicit and auditable** rather than an
  implicit in-app flag that a bug could flip.

The bundled `deploy/free` stack enforces this split directly: `db-roles` creates
`ATLAS_TENANT_DB_USER` as `NOSUPERUSER NOBYPASSRLS` and
`ATLAS_SYSTEM_DB_USER` as `NOSUPERUSER BYPASSRLS`, then `migrate` grants schema
privileges after Alembic finishes. The API/worker runtime URLs use those runtime
roles; the migration/admin owner in `POSTGRES_USER` is not injected into the
application containers.

## Proven, not asserted

Verified against a real PostgreSQL 16, connecting as a `NOSUPERUSER
NOBYPASSRLS` role through the production async stack (SQLAlchemy + asyncpg):

| Guarantee | Result |
|-----------|--------|
| Tenant A sees only A's rows; B only B's | PASS |
| Unset context → 0 rows (fail-closed) | PASS |
| Cross-tenant `INSERT` rejected by `WITH CHECK` | PASS |
| Table owner also constrained (`FORCE`) | PASS (psql proof) |
| `BYPASSRLS` role sees all rows (system path) | PASS (psql proof) |

The first three run in CI as `tests/integration/test_tenant_rls.py` (marked
`integration`). Local runs are skipped without `TEST_DATABASE_URL`; when
`ATLAS_RLS_TEST_MUST_RUN=1` is set, the suite fails if the configured role is a
superuser or has `BYPASSRLS`, because a false green would be worse than no test.
The SQL-shape of the context helper is unit-tested without a database in
`tests/infrastructure/test_tenant_context.py`.

## Apply

```bash
alembic upgrade head      # runs 045 after 044
```

Downgrade drops the policies and disables RLS cleanly (`alembic downgrade 044`).

## Relationship to the topology you chose

You chose "fully separate, sync public→private." RLS is the *cross-tenant*
guarantee **inside** the SMS database; physical separation is the
*public↔private* guarantee **between** databases. They are complementary: even
once the public Atlas is a separate store, the SMS still holds many tenants, and
this is what keeps them apart. The cross-reference engine can now be built
against `create_tenant_uow` (per-tenant, RLS-enforced) for the private side and
the read-only public corpus for the precedent side — with the boundary it
crosses backed by the database, not by remembering to filter.
