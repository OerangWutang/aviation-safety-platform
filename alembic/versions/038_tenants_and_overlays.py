"""Enterprise tenants and private overlays (Phase 5).

Revision ID: 038
Revises: 037

Introduces the multi-tenant data model.  Tenant-private rows live in
*parallel* tables, not as a column on existing public tables.  This
makes accidental contamination of public projections impossible by
construction: a query against ``claims`` can never return a tenant
row, because there are no tenant rows in that table.

Tables added
------------

- ``tenants`` — directory of tenant organisations.
- ``tenant_memberships`` — which user has access to which tenant,
  and at what tenant role.
- ``tenant_sources`` — tenant-private source records.  References
  ``tenants``.  Cannot be joined into the public ``sources`` table.
- ``tenant_ingestion_runs`` — tenant-side ingestion provenance.
- ``tenant_claims`` — tenant-private claims about public events.
  References ``tenant_sources``, ``tenant_ingestion_runs``, and the
  public ``accident_events`` table (so claims hang off the canonical
  event identity).
- ``tenant_event_overlays`` — per-event tenant-private editorial
  notes and structured fields that overlay the public projection.

Existing tables changed
-----------------------

- ``api_keys`` gets two nullable columns:
  - ``tenant_id`` (FK to ``tenants``) — non-null means this key
    acts inside a tenant scope in addition to its system role.
  - ``tenant_role`` — the tenant-side role (OWNER, MEMBER,
    READ_ONLY).  Constrained by CHECK.

Design notes
------------

- Tenant isolation is enforced **at the repository layer**, not just
  in routers.  Every tenant repo method takes ``tenant_id`` as a
  required parameter and includes it in the WHERE clause.
- Tenant claims reference ``accident_events.id``, the same canonical
  identity as public claims.  This is the whole point of overlays:
  the tenant's private view is *anchored* to public ground truth,
  not a separate truth.
- No tenant data reaches public read paths because public
  repositories never query the ``tenant_*`` tables.  No filter
  predicate is needed; the table separation is the filter.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "038"
down_revision = "037"
branch_labels = None
depends_on = None


_TENANT_ROLE_VALUES = ("'OWNER'", "'MEMBER'", "'READ_ONLY'")
_TENANT_INGESTION_STATUSES = ("'running'", "'succeeded'", "'failed'")


def upgrade() -> None:
    # ── tenants ────────────────────────────────────────────────────
    op.create_table(
        "tenants",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("uuid_generate_v4()"),
        ),
        sa.Column("slug", sa.String(length=120), nullable=False),
        sa.Column("display_name", sa.String(length=300), nullable=False),
        sa.Column(
            "is_active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index(
        "uq_tenants_slug",
        "tenants",
        ["slug"],
        unique=True,
    )

    # ── tenant_memberships ─────────────────────────────────────────
    op.create_table(
        "tenant_memberships",
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
            "user_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column(
            "tenant_role",
            sa.String(length=20),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            f"tenant_role IN ({', '.join(_TENANT_ROLE_VALUES)})",
            name="ck_tenant_memberships_role",
        ),
    )
    op.create_index(
        "uq_tenant_memberships_user",
        "tenant_memberships",
        ["tenant_id", "user_id"],
        unique=True,
    )
    op.create_index(
        "ix_tenant_memberships_user_id",
        "tenant_memberships",
        ["user_id"],
    )

    # ── api_keys: tenant binding ───────────────────────────────────
    #
    # A tenant API key has a non-null ``tenant_id`` and ``tenant_role``.
    # System keys keep both NULL.  The CHECK is "both null or both
    # non-null": rules out half-configured rows.
    op.add_column(
        "api_keys",
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column(
        "api_keys",
        sa.Column("tenant_role", sa.String(length=20), nullable=True),
    )
    op.create_check_constraint(
        "ck_api_keys_tenant_pair_consistent",
        "api_keys",
        "(tenant_id IS NULL) = (tenant_role IS NULL)",
    )
    op.create_check_constraint(
        "ck_api_keys_tenant_role_valid",
        "api_keys",
        f"tenant_role IS NULL OR tenant_role IN ({', '.join(_TENANT_ROLE_VALUES)})",
    )
    op.create_index(
        "ix_api_keys_tenant_id",
        "api_keys",
        ["tenant_id"],
    )

    # ── tenant_sources ─────────────────────────────────────────────
    op.create_table(
        "tenant_sources",
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
        sa.Column("name", sa.String(length=300), nullable=False),
        sa.Column("kind", sa.String(length=50), nullable=False),
        sa.Column(
            "reliability_tier",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("1"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    # Source names are unique *within a tenant* — different tenants
    # may legitimately have a source called "Operations" without
    # colliding.  The composite uniqueness mirrors that.
    op.create_index(
        "uq_tenant_sources_tenant_name",
        "tenant_sources",
        ["tenant_id", "name"],
        unique=True,
    )

    # ── tenant_ingestion_runs ──────────────────────────────────────
    op.create_table(
        "tenant_ingestion_runs",
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
            "tenant_source_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenant_sources.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.String(length=20),
            nullable=False,
            server_default=sa.text("'running'"),
        ),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "finished_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.CheckConstraint(
            f"status IN ({', '.join(_TENANT_INGESTION_STATUSES)})",
            name="ck_tenant_ingestion_runs_status",
        ),
    )
    op.create_index(
        "ix_tenant_ingestion_runs_tenant",
        "tenant_ingestion_runs",
        ["tenant_id"],
    )

    # ── tenant_claims ──────────────────────────────────────────────
    #
    # Tenant-private claims about public events.  References:
    # - the public canonical ``accident_events`` table (event_id)
    # - the tenant's own source and ingestion run
    #
    # Note: this is intentionally NOT the public ``claims`` table.
    # Tenant claims do not participate in the public projection.
    op.create_table(
        "tenant_claims",
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
        sa.Column(
            "tenant_source_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenant_sources.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "tenant_ingestion_run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenant_ingestion_runs.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("field_name", sa.String(length=200), nullable=False),
        sa.Column(
            "field_value",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    # All three lookup shapes the use cases need:
    op.create_index(
        "ix_tenant_claims_tenant_event",
        "tenant_claims",
        ["tenant_id", "event_id"],
    )
    op.create_index(
        "ix_tenant_claims_tenant_field",
        "tenant_claims",
        ["tenant_id", "field_name"],
    )

    # ── tenant_event_overlays ──────────────────────────────────────
    #
    # One row per (tenant, event) — a tenant's free-form notes plus a
    # structured field bag that overlays the public projection in the
    # tenant's own views.  Single-row-per-(tenant,event) keeps the
    # overlay a coherent unit of edit rather than a stream.
    op.create_table(
        "tenant_event_overlays",
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
        sa.Column("notes_markdown", sa.Text(), nullable=True),
        sa.Column(
            "overlay_fields",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index(
        "uq_tenant_event_overlays_tenant_event",
        "tenant_event_overlays",
        ["tenant_id", "event_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(
        "uq_tenant_event_overlays_tenant_event",
        table_name="tenant_event_overlays",
    )
    op.drop_table("tenant_event_overlays")

    op.drop_index("ix_tenant_claims_tenant_field", table_name="tenant_claims")
    op.drop_index("ix_tenant_claims_tenant_event", table_name="tenant_claims")
    op.drop_table("tenant_claims")

    op.drop_index("ix_tenant_ingestion_runs_tenant", table_name="tenant_ingestion_runs")
    op.drop_table("tenant_ingestion_runs")

    op.drop_index("uq_tenant_sources_tenant_name", table_name="tenant_sources")
    op.drop_table("tenant_sources")

    op.drop_index("ix_api_keys_tenant_id", table_name="api_keys")
    op.drop_constraint("ck_api_keys_tenant_role_valid", "api_keys", type_="check")
    op.drop_constraint("ck_api_keys_tenant_pair_consistent", "api_keys", type_="check")
    op.drop_column("api_keys", "tenant_role")
    op.drop_column("api_keys", "tenant_id")

    op.drop_index("ix_tenant_memberships_user_id", table_name="tenant_memberships")
    op.drop_index("uq_tenant_memberships_user", table_name="tenant_memberships")
    op.drop_table("tenant_memberships")

    op.drop_index("uq_tenants_slug", table_name="tenants")
    op.drop_table("tenants")
