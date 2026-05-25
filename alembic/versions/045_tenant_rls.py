"""Database-enforced tenant isolation via row-level security.

Revision ID: 045
Revises: 044
Create Date: 2026-05-23

Until now tenant isolation was enforced **only** in application code (the
"three-layer rule" in the repositories/use-cases).  That is a procedural
guarantee: one forgotten ``WHERE tenant_id = ...`` - in existing code or in a
future feature such as the AI cross-reference engine, which by design reads
across the public/private boundary - silently leaks one operator's protected
safety data to another.

This migration makes the guarantee **structural**.  Every tenant *payload*
table gets PostgreSQL row-level security with a single isolation policy keyed on
a transaction-local GUC, ``app.current_tenant_id``:

* ``ENABLE`` + ``FORCE`` ROW LEVEL SECURITY - ``FORCE`` so the table *owner*
  is constrained too, not just non-owner roles.
* policy ``USING`` + ``WITH CHECK`` on ``tenant_id::text = current_setting(...)``
  - so reads are filtered *and* cross-tenant writes are rejected.
* fail-closed: when the GUC is unset, ``current_setting(..., true)`` is NULL and
  the predicate matches no rows.  A request that forgets to establish tenant
  context sees zero tenant rows rather than all of them.

**Bootstrap tables are deliberately excluded.**  ``tenants`` and
``tenant_memberships`` are the identity/auth tables read to *establish* the
tenant context in the first place; putting them behind the same GUC would be a
chicken-and-egg deadlock.  They remain guarded by application-layer auth + the
membership check.  Only the tables holding tenant *data* are covered here.

**``tenant_crossref_results`` is excluded here** because it did not exist when
this migration was written.  It was added in migration 046, which applies RLS
inline at table-creation time using the same GUC and policy shape.  The full
set of RLS-protected tenant payload tables is therefore the union of the list
above and ``tenant_crossref_results``.

**Operational requirement (see TENANT_RLS.md):** the guarantee holds only if the
application connects as a role that is NOT a superuser and does NOT have
BYPASSRLS.  System/admin jobs that legitimately need cross-tenant access (the
cross-reference indexer, platform admin, projection rebuilds) must connect as a
separate role WITH BYPASSRLS - explicit and auditable, never implicit.
"""

from __future__ import annotations

from alembic import op

revision = "045"
down_revision = "044"
branch_labels = None
depends_on = None

# Tenant *payload* tables only.  Bootstrap/identity tables (tenants,
# tenant_memberships) are intentionally not RLS-guarded - see module docstring.
_TENANT_PAYLOAD_TABLES: tuple[str, ...] = (
    "tenant_sources",
    "tenant_ingestion_runs",
    "tenant_claims",
    "tenant_event_overlays",
    "tenant_safety_reports",
    "tenant_event_associations",
)

_POLICY_NAME = "tenant_isolation"
_GUC = "app.current_tenant_id"


def upgrade() -> None:
    for table in _TENANT_PAYLOAD_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        # FORCE so the table owner is also subject to the policy.
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"""
            CREATE POLICY {_POLICY_NAME} ON {table}
                USING (tenant_id::text = current_setting('{_GUC}', true))
                WITH CHECK (tenant_id::text = current_setting('{_GUC}', true))
            """
        )


def downgrade() -> None:
    for table in _TENANT_PAYLOAD_TABLES:
        op.execute(f"DROP POLICY IF EXISTS {_POLICY_NAME} ON {table}")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
