"""Add indexes on FK columns that lack them.

Revision ID: 049
Revises: 048
Create Date: 2026-05-24

Audit (run against ORM metadata, May 2026) found 26 FK columns with no
covering B-tree index.  A PostgreSQL FK constraint itself creates no index on
the referencing side; without one, any DELETE/UPDATE on the parent and any
reverse lookup on the child requires a sequential scan.

This migration adds indexes for the columns where the absence is most likely
to hurt in production:

**accident_events.merged_into_event_id** — the self-referential merge pointer
  walked by the canonicalisation path on every public page lookup, and by the
  admin "which events were merged into this?" query.  Partial index (NOT NULL)
  since the overwhelming majority of rows have merged_into_event_id = NULL.

**ingestion_runs.source_id** — used by every "list runs for source X" lookup
  in the ingestion review UI and the re-ingestion path.  The table has no
  index at all beyond its PK.

**raw_snapshots.ingestion_run_id** — the unique constraint covers
  (source_id, ingestion_run_id) which helps the idempotency check but not
  "fetch all snapshots for a given run" without also providing source_id.
  Partial index (NOT NULL) since legacy rows pre-dating the run FK may be NULL.

**tenant_ingestion_runs.tenant_source_id** — "list runs for tenant source X"
  query used by the tenant review UI.

**tenant_claims.tenant_ingestion_run_id** and **tenant_claims.tenant_source_id**
  — lookup paths for "what did this run/source produce?" in tenant audit.
  The existing indexes cover (tenant_id, event_id) but not these FK columns.

**tenant_crossref_results.claim_id** — lookup used by the crossref review path
  to find all cross-reference results derived from a given tenant claim.

The remaining 19 unindexed FK columns (Chronos provenance links, Orion
review pairs, Hermes change log, projection history audit fields) are
deliberately deferred: they belong to admin/audit tables that are queried
rarely and by primary key or composite filters, not by the FK column alone.
Add them if profiling shows otherwise.
"""

from __future__ import annotations

from alembic import op

revision = "049"
down_revision = "048"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── accident_events ──────────────────────────────────────────────────────
    # Partial index: merged rows are a small fraction; NULLs need no entry.
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_accident_events_merged_into
            ON accident_events (merged_into_event_id)
            WHERE merged_into_event_id IS NOT NULL
        """
    )

    # ── ingestion_runs ───────────────────────────────────────────────────────
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_ingestion_runs_source_id
            ON ingestion_runs (source_id)
        """
    )

    # ── raw_snapshots ────────────────────────────────────────────────────────
    # Partial: pre-FK legacy rows have NULL; they're not reachable by run anyway.
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_raw_snapshots_ingestion_run_id
            ON raw_snapshots (ingestion_run_id)
            WHERE ingestion_run_id IS NOT NULL
        """
    )

    # ── tenant_ingestion_runs ────────────────────────────────────────────────
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_tenant_ingestion_runs_source_id
            ON tenant_ingestion_runs (tenant_source_id)
        """
    )

    # ── tenant_claims ────────────────────────────────────────────────────────
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_tenant_claims_ingestion_run_id
            ON tenant_claims (tenant_ingestion_run_id)
            WHERE tenant_ingestion_run_id IS NOT NULL
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_tenant_claims_source_id
            ON tenant_claims (tenant_source_id)
            WHERE tenant_source_id IS NOT NULL
        """
    )

    # ── tenant_crossref_results ──────────────────────────────────────────────
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_tenant_crossref_results_claim_id
            ON tenant_crossref_results (claim_id)
            WHERE claim_id IS NOT NULL
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_accident_events_merged_into")
    op.execute("DROP INDEX IF EXISTS ix_ingestion_runs_source_id")
    op.execute("DROP INDEX IF EXISTS ix_raw_snapshots_ingestion_run_id")
    op.execute("DROP INDEX IF EXISTS ix_tenant_ingestion_runs_source_id")
    op.execute("DROP INDEX IF EXISTS ix_tenant_claims_ingestion_run_id")
    op.execute("DROP INDEX IF EXISTS ix_tenant_claims_source_id")
    op.execute("DROP INDEX IF EXISTS ix_tenant_crossref_results_claim_id")
