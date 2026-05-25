"""Editorial workflow: widen public_event_pages status and add revisions.

Revision ID: 036
Revises: 035

Phase 9 of the Aviation Safety Atlas evolution.  Two additive changes:

1. ``public_event_pages.status`` CHECK constraint widens from the
   three Phase 1 states (DRAFT, PUBLISHED, RETRACTED) to the full
   editorial state machine: DRAFT, IN_REVIEW, APPROVED, PUBLISHED,
   ARCHIVED, RETRACTED.

2. A new ``public_event_page_revisions`` table records every
   transition as an immutable audit row.  Revisions are append-only —
   the repository surface intentionally exposes no update or delete
   operations.

Design notes
------------

- ``status``-bearing CHECK constraints on ``public_event_pages`` are
  dropped and recreated rather than altered: PostgreSQL has no
  ``ALTER CHECK`` statement, and recreating is safer than relying on
  ``NOT VALID``/``VALIDATE`` for the small row volume.
- ``public_event_page_revisions.from_status`` is nullable for the
  creation revision (NULL → DRAFT), and ``to_status`` is constrained
  to the full state set.
- An editorial-side composite index on
  ``(status, updated_at DESC, id DESC)`` supports the editorial list
  query (``WHERE status = ANY(...) ORDER BY updated_at DESC``) that
  Phase 9 introduces.  The public partial index from migration 035
  is still correct and is left untouched.
- ``editor_user_id`` is required on every revision so the audit trail
  always names a responsible actor.  Curator-override-style "system
  actor" rows would still need a real ``api_keys.user_id`` to point
  at; we don't introduce a fake system UUID here.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "036"
down_revision = "035"
branch_labels = None
depends_on = None


_STATUS_VALUES = (
    "'DRAFT'",
    "'IN_REVIEW'",
    "'APPROVED'",
    "'PUBLISHED'",
    "'ARCHIVED'",
    "'RETRACTED'",
)


def upgrade() -> None:
    # Drop the Phase 1 CHECK constraint and replace it with the
    # broader one.  Replacing via DROP/ADD avoids relying on
    # backend-specific ALTER CHECK semantics.
    op.drop_constraint("ck_public_event_pages_status", "public_event_pages", type_="check")
    op.create_check_constraint(
        "ck_public_event_pages_status",
        "public_event_pages",
        f"status IN ({', '.join(_STATUS_VALUES)})",
    )

    # Editorial-side index: list-by-status, newest-edited first.
    # Different shape from the public partial index (which is
    # PUBLISHED-only and ordered by last_published_at).
    op.create_index(
        "ix_public_event_pages_status_updated",
        "public_event_pages",
        ["status", sa.text("updated_at DESC"), sa.text("id DESC")],
    )

    op.create_table(
        "public_event_page_revisions",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("uuid_generate_v4()"),
        ),
        sa.Column(
            "page_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("public_event_pages.id"),
            nullable=False,
        ),
        # The page's ``version`` after this revision.  Pinned so the
        # revision list can be reconstructed without joining back to
        # the page row.
        sa.Column("version_at_moment", sa.Integer(), nullable=False),
        # NULL on the create-revision (no prior status); not-NULL on
        # every other transition.
        sa.Column("from_status", sa.String(length=20), nullable=True),
        sa.Column("to_status", sa.String(length=20), nullable=False),
        # Editorial snapshot at the moment of the transition.  Kept
        # alongside the status change so revision viewers don't need
        # to reconstruct historical editorial content from elsewhere.
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("short_summary", sa.Text(), nullable=True),
        sa.Column("narrative_markdown", sa.Text(), nullable=True),
        sa.Column(
            "editor_user_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column("transition_reason", sa.Text(), nullable=True),
        sa.Column("correction_note", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            f"from_status IS NULL OR from_status IN ({', '.join(_STATUS_VALUES)})",
            name="ck_public_event_page_revisions_from_status",
        ),
        sa.CheckConstraint(
            f"to_status IN ({', '.join(_STATUS_VALUES)})",
            name="ck_public_event_page_revisions_to_status",
        ),
        sa.CheckConstraint(
            "version_at_moment >= 1",
            name="ck_public_event_page_revisions_version_ge_1",
        ),
    )

    # The revision list is fetched ``WHERE page_id = ? ORDER BY
    # version_at_moment ASC, id ASC`` — both columns must be in the
    # index for the planner to walk it cleanly.
    op.create_index(
        "ix_public_event_page_revisions_page_version",
        "public_event_page_revisions",
        ["page_id", "version_at_moment", "id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_public_event_page_revisions_page_version",
        table_name="public_event_page_revisions",
    )
    op.drop_table("public_event_page_revisions")

    op.drop_index("ix_public_event_pages_status_updated", table_name="public_event_pages")

    # Restore the Phase 1 (narrower) CHECK constraint.  This will
    # refuse to apply if rows in non-Phase-1 states exist — that's
    # the correct fail-closed behaviour on downgrade.
    op.drop_constraint("ck_public_event_pages_status", "public_event_pages", type_="check")
    op.create_check_constraint(
        "ck_public_event_pages_status",
        "public_event_pages",
        "status IN ('DRAFT', 'PUBLISHED', 'RETRACTED')",
    )
