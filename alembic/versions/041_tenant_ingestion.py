"""FOQA/ASAP tenant ingestion (Phase 6).

Revision ID: 041
Revises: 040

Extends the Phase 5 tenancy model with:

- ``tenant_claims.claim_kind`` / ``confidence`` columns so a single
  table can carry FOQA exceedance claims, generic structured
  claims, and any future claim kind without a new table per kind.
- ``tenant_safety_reports`` for ASAP-style narrative reports.
  Separate table because the row shape (narrative-heavy,
  identity-sensitive, attestation flags) is too different from the
  structured-claim shape to combine cleanly.
- ``tenant_event_associations`` for the explicit
  "this FOQA/ASAP evidence relates to this public event" claim.
  Separated from claims because the relation is editorial
  (correlation, not direct evidence) and analysts may want to
  associate a report with multiple events or none.

Design notes
------------

- ASAP narratives never leave the tenant.  No public-side surface
  ever reads ``tenant_safety_reports`` — that's a hard rule
  enforced in the routing layer (no public router touches this
  table).  This migration cannot prevent that on its own; the
  invariant is co-enforced with the use-case + router gates.
- ``confidence`` is a 0..1 float on tenant claims — separate from
  the public projection's completeness band so tenant editorial
  doesn't get pulled into the public confidence math.
- The ``claim_kind`` enum is small and forward-extensible.  We
  reserve ``FOQA``, ``ASAP``, ``OTHER`` for Phase 6 and document
  that new values require both a migration AND an updated CHECK
  constraint to keep the schema honest.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "041"
down_revision = "040"
branch_labels = None
depends_on = None


_CLAIM_KINDS = ("'FOQA'", "'ASAP'", "'OTHER'")
_REPORT_KINDS = ("'FOQA'", "'ASAP'", "'OTHER'")
_ASSOCIATION_KINDS = ("'RELATED'", "'CONTRIBUTED_TO'", "'PRECEDED'")


def upgrade() -> None:
    # ── tenant_claims: claim_kind + confidence ──────────────────────
    #
    # Both nullable in the migration so existing Phase 5 rows
    # (currently none in any deployed environment) survive.  New
    # writes through Phase 6 always carry both.
    op.add_column(
        "tenant_claims",
        sa.Column(
            "claim_kind",
            sa.String(length=20),
            nullable=False,
            server_default=sa.text("'OTHER'"),
        ),
    )
    op.add_column(
        "tenant_claims",
        sa.Column("confidence", sa.Float(), nullable=True),
    )
    op.create_check_constraint(
        "ck_tenant_claims_claim_kind",
        "tenant_claims",
        f"claim_kind IN ({', '.join(_CLAIM_KINDS)})",
    )
    op.create_check_constraint(
        "ck_tenant_claims_confidence_range",
        "tenant_claims",
        "confidence IS NULL OR (confidence >= 0.0 AND confidence <= 1.0)",
    )
    # Index supporting "all FOQA claims for a tenant on an event".
    op.create_index(
        "ix_tenant_claims_tenant_event_kind",
        "tenant_claims",
        ["tenant_id", "event_id", "claim_kind"],
    )

    # ── tenant_safety_reports ───────────────────────────────────────
    #
    # One row per ASAP-style report.  Identity-sensitive narrative
    # data.  Cannot be joined into the public claims table because
    # it lives in a separate, tenant-scoped table by construction.
    op.create_table(
        "tenant_safety_reports",
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
        sa.Column("report_kind", sa.String(length=20), nullable=False),
        sa.Column("narrative_markdown", sa.Text(), nullable=False),
        # The tenant attests, at submission time, that the narrative
        # has been deidentified.  Atlas runs a best-effort pattern
        # strip on top; this column is the operator's record.
        sa.Column(
            "deidentified_attested",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        # Optional pointer to the operator's internal report id,
        # for cross-referencing in the operator's own SMS tooling.
        # Free-form string; we don't try to validate it.
        sa.Column("external_report_ref", sa.String(length=200), nullable=True),
        sa.Column(
            "submitter_user_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            f"report_kind IN ({', '.join(_REPORT_KINDS)})",
            name="ck_tenant_safety_reports_kind",
        ),
    )
    op.create_index(
        "ix_tenant_safety_reports_tenant_created",
        "tenant_safety_reports",
        ["tenant_id", sa.text("created_at DESC")],
    )

    # ── tenant_event_associations ───────────────────────────────────
    #
    # Editorial association between a tenant's private evidence (a
    # claim or a safety report) and a public event.  One row per
    # association so an analyst can attach the same report to
    # multiple events or none.
    op.create_table(
        "tenant_event_associations",
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
        sa.Column(
            "event_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("accident_events.id"),
            nullable=False,
        ),
        # Exactly one of (claim_id, safety_report_id) is non-null —
        # enforced by the CHECK below.  Nullable FK keeps the schema
        # simple without a discriminator pattern.
        sa.Column(
            "claim_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenant_claims.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "safety_report_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenant_safety_reports.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("association_kind", sa.String(length=30), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column(
            "created_by_user_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            f"association_kind IN ({', '.join(_ASSOCIATION_KINDS)})",
            name="ck_tenant_event_associations_kind",
        ),
        sa.CheckConstraint(
            "(claim_id IS NOT NULL)::int + (safety_report_id IS NOT NULL)::int = 1",
            name="ck_tenant_event_associations_exactly_one_source",
        ),
    )
    op.create_index(
        "ix_tenant_event_associations_tenant_event",
        "tenant_event_associations",
        ["tenant_id", "event_id"],
    )
    op.create_index(
        "ix_tenant_event_associations_claim",
        "tenant_event_associations",
        ["claim_id"],
    )
    op.create_index(
        "ix_tenant_event_associations_safety_report",
        "tenant_event_associations",
        ["safety_report_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_tenant_event_associations_safety_report",
        table_name="tenant_event_associations",
    )
    op.drop_index(
        "ix_tenant_event_associations_claim",
        table_name="tenant_event_associations",
    )
    op.drop_index(
        "ix_tenant_event_associations_tenant_event",
        table_name="tenant_event_associations",
    )
    op.drop_table("tenant_event_associations")

    op.drop_index(
        "ix_tenant_safety_reports_tenant_created",
        table_name="tenant_safety_reports",
    )
    op.drop_table("tenant_safety_reports")

    op.drop_index("ix_tenant_claims_tenant_event_kind", table_name="tenant_claims")
    op.drop_constraint(
        "ck_tenant_claims_confidence_range",
        "tenant_claims",
        type_="check",
    )
    op.drop_constraint("ck_tenant_claims_claim_kind", "tenant_claims", type_="check")
    op.drop_column("tenant_claims", "confidence")
    op.drop_column("tenant_claims", "claim_kind")
