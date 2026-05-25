"""Metering: usage events + daily rollups (Phase 8).

Revision ID: 044
Revises: 043

Two tables:

- ``usage_events`` — one immutable row per metered action.  Records
  the metric kind, the actor (optional tenant id, optional user
  id), an optional resource id (the event/claim/report id that the
  action operated on), and the timestamp.  Append-only.
- ``usage_daily_rollups`` — per-tenant, per-day, per-metric
  aggregates.  Computed from ``usage_events`` by the rollup use
  case.  Idempotent UPSERTs on the natural key
  ``(tenant_id, date, metric_kind)``.

Design notes
------------

- ``tenant_id`` is nullable on both tables: some metrics (NL search)
  are not tenant-scoped.  The rollup table uses a sentinel UUID for
  NULL so the natural-key unique constraint can include it without
  a partial index.
- ``metric_kind`` is a string column (not an enum type) so adding a
  new metric in a future phase is a code change only — no schema
  migration to ALTER the enum.  A CHECK constraint pins the valid
  values; updating the CHECK is a routine migration.
- ``resource_id`` is denormalised UUID, no FK.  Audit only — if the
  underlying row is later deleted, the metering row remains so
  monthly invoices stay reproducible.
- The rollup table's ``count`` column is the only mutable field;
  every other column is part of the natural key.  Updates from
  recomputation replace the count atomically.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "044"
down_revision = "043"
branch_labels = None
depends_on = None


_METRIC_KINDS = (
    "'TENANT_CLAIM_INGESTED'",
    "'TENANT_REPORT_FILED'",
    "'TENANT_INGESTION_RUN_COMPLETED'",
    "'NL_QUERY_EXECUTED'",
    "'HFACS_ATTRIBUTION_CREATED'",
)


def upgrade() -> None:
    # ── usage_events ────────────────────────────────────────────────
    op.create_table(
        "usage_events",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("uuid_generate_v4()"),
        ),
        sa.Column("metric_kind", sa.String(length=64), nullable=False),
        # Nullable tenant for non-tenant-scoped metrics like NL search.
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
        # Optional pointer to the resource the action operated on
        # (event id, claim id, report id, etc.).  Denormalised: no
        # FK so the metering row survives downstream deletes.
        sa.Column(
            "resource_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            f"metric_kind IN ({', '.join(_METRIC_KINDS)})",
            name="ck_usage_events_metric_kind",
        ),
    )
    # Query patterns:
    # - rollup computation scans by (tenant_id, recorded_at) range
    # - per-resource audit reads scan by resource_id
    op.create_index(
        "ix_usage_events_tenant_recorded_at",
        "usage_events",
        ["tenant_id", "recorded_at"],
    )
    op.create_index(
        "ix_usage_events_metric_recorded_at",
        "usage_events",
        ["metric_kind", "recorded_at"],
    )
    op.create_index(
        "ix_usage_events_resource_id",
        "usage_events",
        ["resource_id"],
    )

    # ── usage_daily_rollups ─────────────────────────────────────────
    op.create_table(
        "usage_daily_rollups",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("uuid_generate_v4()"),
        ),
        # Sentinel-UUID-when-NULL for the natural key, so we can use
        # a regular composite unique index instead of a partial one.
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column("metric_kind", sa.String(length=64), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column(
            "count",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "computed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            f"metric_kind IN ({', '.join(_METRIC_KINDS)})",
            name="ck_usage_daily_rollups_metric_kind",
        ),
        sa.CheckConstraint(
            "count >= 0",
            name="ck_usage_daily_rollups_count_nonneg",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "metric_kind",
            "day",
            name="uq_usage_daily_rollups_natural",
        ),
    )
    # Range scans by day for monthly invoices.
    op.create_index(
        "ix_usage_daily_rollups_day",
        "usage_daily_rollups",
        ["day"],
    )


def downgrade() -> None:
    op.drop_index("ix_usage_daily_rollups_day", table_name="usage_daily_rollups")
    op.drop_table("usage_daily_rollups")
    op.drop_index("ix_usage_events_resource_id", table_name="usage_events")
    op.drop_index("ix_usage_events_metric_recorded_at", table_name="usage_events")
    op.drop_index("ix_usage_events_tenant_recorded_at", table_name="usage_events")
    op.drop_table("usage_events")
