"""Public event pages: publication metadata over canonical projections.

Revision ID: 035
Revises: 034

Phase 1 of the Aviation Safety Atlas evolution introduces a thin
publication metadata layer that sits *above* the existing evidence chain
and projection model.  A ``public_event_pages`` row points at a
canonical ``accident_events.id`` and carries only editorial overlay
fields (title, short summary, narrative, publication state) — never a
copy of any projected field.  Public read paths join this table to
``projected_accident_records`` so the projection remains the source of
truth for structured facts.

Design notes
------------

- ``slug`` is globally unique.  Bound to 160 chars to comfortably hold
  date + descriptor while staying URL-safe.
- ``event_id`` is the canonical (post-merge) event id and is unique:
  one page per canonical event.  Curators creating a page for an event
  that is later merged are responsible for either retracting the page
  or repointing it via a Phase 9 editorial workflow; this migration
  does not auto-rewrite ``event_id`` on merge.
- Status set is intentionally narrow now (DRAFT, PUBLISHED, RETRACTED).
  The full editorial state machine (IN_REVIEW, APPROVED, ARCHIVED)
  arrives in Phase 9; the ``version`` column is already present so that
  optimistic concurrency can be added without a follow-up migration.
- The partial index ``ix_public_event_pages_published_pub_id`` matches
  the exact public list query: ``WHERE status='PUBLISHED' ORDER BY
  last_published_at DESC, id DESC`` for stable keyset cursor
  pagination.
- CHECK constraints encode the invariants that a PUBLISHED row always
  has ``last_published_at`` and a RETRACTED row always has
  ``retracted_at``.  These prevent a half-written publish/retract from
  leaking into the public list.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "035"
down_revision = "034"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "public_event_pages",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("uuid_generate_v4()"),
        ),
        sa.Column(
            "event_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("accident_events.id"),
            nullable=False,
        ),
        sa.Column("slug", sa.String(length=160), nullable=False),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("short_summary", sa.Text(), nullable=True),
        sa.Column("narrative_markdown", sa.Text(), nullable=True),
        sa.Column(
            "status",
            sa.String(length=20),
            nullable=False,
            server_default=sa.text("'DRAFT'"),
        ),
        sa.Column(
            "version",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("1"),
        ),
        sa.Column("first_published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retracted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retraction_note", sa.Text(), nullable=True),
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
        sa.CheckConstraint(
            "status IN ('DRAFT', 'PUBLISHED', 'RETRACTED')",
            name="ck_public_event_pages_status",
        ),
        sa.CheckConstraint(
            "status <> 'PUBLISHED' OR last_published_at IS NOT NULL",
            name="ck_public_event_pages_published_requires_timestamp",
        ),
        sa.CheckConstraint(
            "status <> 'RETRACTED' OR retracted_at IS NOT NULL",
            name="ck_public_event_pages_retracted_requires_timestamp",
        ),
        sa.CheckConstraint(
            "version >= 1",
            name="ck_public_event_pages_version_ge_1",
        ),
    )

    op.create_index(
        "uq_public_event_pages_slug",
        "public_event_pages",
        ["slug"],
        unique=True,
    )
    op.create_index(
        "uq_public_event_pages_event_id",
        "public_event_pages",
        ["event_id"],
        unique=True,
    )
    # Partial index sized for the public list query plan.  Ordering by
    # (last_published_at DESC, id DESC) is the stable keyset key used by
    # the public list endpoint; the partial predicate keeps DRAFT and
    # RETRACTED pages out of the hot index.
    op.create_index(
        "ix_public_event_pages_published_pub_id",
        "public_event_pages",
        [sa.text("last_published_at DESC"), sa.text("id DESC")],
        postgresql_where=sa.text("status = 'PUBLISHED'"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_public_event_pages_published_pub_id",
        table_name="public_event_pages",
    )
    op.drop_index("uq_public_event_pages_event_id", table_name="public_event_pages")
    op.drop_index("uq_public_event_pages_slug", table_name="public_event_pages")
    op.drop_table("public_event_pages")
