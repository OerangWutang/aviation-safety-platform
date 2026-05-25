"""Echo cross-reference result persistence (Phase 7+).

Revision ID: 046
Revises: 045
Create Date: 2026-05-23

Adds ``tenant_crossref_results`` — the tenant-private table that stores the
ranked ``PrecedentMatch`` list Echo produces for one hazard source.

Design notes
------------
- **Written once, never updated.**  The ``matches_json`` column is set on
  transition to ``COMPLETE`` and stays there.  Re-running Echo for the same
  hazard creates a new row (with a new ``requested_at``).  This preserves an
  audit trail of how results changed as the public corpus grew or the matcher
  weights were tuned.

- **JSONB for match payload.**  The ``PrecedentMatch`` shape (nested components,
  shared sets, display fields) is rich but not queried column-by-column by the
  application — it is read as a unit and rendered back to the analyst.  JSONB
  is the right choice here for the same reason it is used for ``payload_json``
  and ``ingestion_result_json`` elsewhere: it preserves exact structure without
  a per-field child table, and Postgres can still index into it if a future
  feature needs ``matches_json @> '...'`` filtering.

- **RLS applied at creation time.**  Unlike the six tables in migration 045
  (which had RLS retrofitted), this table gets ``ENABLE + FORCE`` and the
  ``tenant_isolation`` policy in the same migration that creates it.  That is
  the correct pattern for all new tenant payload tables going forward.

- **Exactly-one source constraint.**  The XOR CHECK on
  ``(safety_report_id, claim_id)`` mirrors the pattern on
  ``tenant_event_associations`` — the domain entity's ``model_post_init``
  enforces it in Python; the constraint enforces it in the DB; both must agree.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "046"
down_revision = "045"
branch_labels = None
depends_on = None

_GUC = "app.current_tenant_id"
_POLICY = "tenant_isolation"
_STATUSES = ("'PENDING'", "'COMPLETE'", "'FAILED'")


def upgrade() -> None:
    op.create_table(
        "tenant_crossref_results",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("uuid_generate_v4()"),
        ),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # Exactly one of safety_report_id / claim_id is non-NULL (XOR CHECK below).
        sa.Column(
            "safety_report_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenant_safety_reports.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "claim_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenant_claims.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "status",
            sa.String(length=20),
            nullable=False,
            server_default=sa.text("'PENDING'"),
        ),
        # Serialised list[PrecedentMatch] — written once, on COMPLETE.
        sa.Column(
            "matches_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        # Echo config snapshot (weights, thresholds) so results remain
        # interpretable if the matcher is tuned after the run.
        sa.Column(
            "matcher_config_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "match_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "requested_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "completed_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "error_detail",
            sa.Text(),
            nullable=True,
        ),
        # Constraints.
        sa.CheckConstraint(
            f"status IN ({', '.join(_STATUSES)})",
            name="ck_tenant_crossref_results_status",
        ),
        sa.CheckConstraint(
            "(safety_report_id IS NOT NULL)::int + (claim_id IS NOT NULL)::int = 1",
            name="ck_tenant_crossref_results_source_xor",
        ),
        sa.CheckConstraint(
            "match_count >= 0",
            name="ck_tenant_crossref_results_match_count_nonneg",
        ),
    )

    # Indexes.
    op.create_index(
        "ix_tenant_crossref_results_tenant_report",
        "tenant_crossref_results",
        ["tenant_id", "safety_report_id"],
        postgresql_where=sa.text("safety_report_id IS NOT NULL"),
    )
    op.create_index(
        "ix_tenant_crossref_results_tenant_requested",
        "tenant_crossref_results",
        ["tenant_id", sa.text("requested_at DESC")],
    )

    # RLS — same pattern as migration 045, applied at table creation this time.
    op.execute("ALTER TABLE tenant_crossref_results ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE tenant_crossref_results FORCE  ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY {_POLICY} ON tenant_crossref_results
            USING      (tenant_id::text = current_setting('{_GUC}', true))
            WITH CHECK (tenant_id::text = current_setting('{_GUC}', true))
        """
    )


def downgrade() -> None:
    op.execute(f"DROP POLICY IF EXISTS {_POLICY} ON tenant_crossref_results")
    op.execute("ALTER TABLE tenant_crossref_results NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE tenant_crossref_results DISABLE ROW LEVEL SECURITY")
    op.drop_index(
        "ix_tenant_crossref_results_tenant_requested", table_name="tenant_crossref_results"
    )
    op.drop_index("ix_tenant_crossref_results_tenant_report", table_name="tenant_crossref_results")
    op.drop_table("tenant_crossref_results")
