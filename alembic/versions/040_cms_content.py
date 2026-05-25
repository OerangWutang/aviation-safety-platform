"""CMS-like content: glossary, methodology, changelog (Phase 10).

Revision ID: 040
Revises: 039

Three independent content kinds, each with the editorial workflow
from Phase 9 (DRAFT/IN_REVIEW/APPROVED/PUBLISHED/ARCHIVED/RETRACTED).
The state machine is identical; only the row shape differs.

Tables added
------------

Glossary
    - ``glossary_terms`` — one row per defined term.  ``term`` is
      the canonical lookup key (kebab-case slug); ``display_term``
      is the human form for UI.  Unique on ``term``.
    - ``glossary_term_revisions`` — immutable audit row per
      transition, same shape as Phase 9's page revisions.

Methodology
    - ``methodology_pages`` — long-form pages explaining how Atlas
      works.  ``slug`` is unique; ``section`` groups pages into
      ordered sets (e.g. "data-sources", "confidence", "audit").
    - ``methodology_page_revisions``.

Changelog
    - ``changelog_entries`` — dated entries describing notable
      platform changes.  ``effective_date`` is the human-meaningful
      date (when the change happened in the real world);
      ``last_published_at`` is when the entry first reached
      PUBLISHED.  Slugs are unique.
    - ``changelog_entry_revisions``.

Why three tables instead of one polymorphic ``content_pages``
------------------------------------------------------------

Each kind has fields that don't apply to the others (glossary's
``term`` key, methodology's ``section`` grouping, changelog's
``effective_date``).  A single polymorphic table with nullable
columns would force every read path to filter by ``kind`` and would
invite drift where a column added for one kind doesn't apply to
another.  Three tables, three small repos, one shared state
machine.

The state machine itself lives in ``atlas.domain.publication.workflow``
and is reused unchanged.  Phase 10 doesn't extend it.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "040"
down_revision = "039"
branch_labels = None
depends_on = None


_STATUS_CHECK = "status IN ('DRAFT', 'IN_REVIEW', 'APPROVED', 'PUBLISHED', 'ARCHIVED', 'RETRACTED')"


def _add_workflow_columns(table_name: str) -> None:
    """Common workflow column set added to all three content tables.

    Each row carries ``status`` (with the same CHECK as Phase 9),
    ``version`` (for optimistic concurrency), and the
    first/last-published-at pair so the public surface can render
    "first published / last updated" without joining the revision
    audit table.
    """
    op.add_column(
        table_name,
        sa.Column("status", sa.String(length=20), nullable=False),
    )
    op.add_column(
        table_name,
        sa.Column(
            "version",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("1"),
        ),
    )
    op.add_column(
        table_name,
        sa.Column(
            "first_published_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.add_column(
        table_name,
        sa.Column(
            "last_published_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.add_column(
        table_name,
        sa.Column("retraction_note", sa.Text(), nullable=True),
    )


def upgrade() -> None:
    # ── glossary_terms ─────────────────────────────────────────────
    op.create_table(
        "glossary_terms",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("uuid_generate_v4()"),
        ),
        sa.Column("term", sa.String(length=120), nullable=False),
        sa.Column("display_term", sa.String(length=200), nullable=False),
        sa.Column("body_markdown", sa.Text(), nullable=False),
        sa.Column(
            "status", sa.String(length=20), nullable=False, server_default=sa.text("'DRAFT'")
        ),
        sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("first_published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_published_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.CheckConstraint(_STATUS_CHECK, name="ck_glossary_terms_status"),
    )
    op.create_index(
        "uq_glossary_terms_term",
        "glossary_terms",
        ["term"],
        unique=True,
    )
    # Partial index: the "currently visible publicly" subset.  Mirror
    # the Phase 1 ix_public_event_pages_published_pub_id shape.
    op.execute(
        "CREATE INDEX ix_glossary_terms_published_term "
        "ON glossary_terms (term) "
        "WHERE status = 'PUBLISHED'"
    )

    op.create_table(
        "glossary_term_revisions",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("uuid_generate_v4()"),
        ),
        sa.Column(
            "term_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("glossary_terms.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("from_status", sa.String(length=20), nullable=True),
        sa.Column("to_status", sa.String(length=20), nullable=False),
        sa.Column("version_at_revision", sa.Integer(), nullable=False),
        sa.Column("editor_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("transition_reason", sa.Text(), nullable=True),
        sa.Column("correction_note", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index(
        "ix_glossary_term_revisions_term",
        "glossary_term_revisions",
        ["term_id", "created_at"],
    )

    # ── methodology_pages ───────────────────────────────────────────
    op.create_table(
        "methodology_pages",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("uuid_generate_v4()"),
        ),
        sa.Column("slug", sa.String(length=160), nullable=False),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("section", sa.String(length=100), nullable=False),
        sa.Column(
            "section_order",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("body_markdown", sa.Text(), nullable=False),
        sa.Column(
            "status", sa.String(length=20), nullable=False, server_default=sa.text("'DRAFT'")
        ),
        sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("first_published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_published_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.CheckConstraint(_STATUS_CHECK, name="ck_methodology_pages_status"),
    )
    op.create_index(
        "uq_methodology_pages_slug",
        "methodology_pages",
        ["slug"],
        unique=True,
    )
    # Section-ordered listing: a typical "/methodology" navigation
    # page renders by section, then by section_order, then by title.
    op.create_index(
        "ix_methodology_pages_section_order",
        "methodology_pages",
        ["section", "section_order", "title"],
    )

    op.create_table(
        "methodology_page_revisions",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("uuid_generate_v4()"),
        ),
        sa.Column(
            "page_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("methodology_pages.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("from_status", sa.String(length=20), nullable=True),
        sa.Column("to_status", sa.String(length=20), nullable=False),
        sa.Column("version_at_revision", sa.Integer(), nullable=False),
        sa.Column("editor_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("transition_reason", sa.Text(), nullable=True),
        sa.Column("correction_note", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index(
        "ix_methodology_page_revisions_page",
        "methodology_page_revisions",
        ["page_id", "created_at"],
    )

    # ── changelog_entries ───────────────────────────────────────────
    op.create_table(
        "changelog_entries",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("uuid_generate_v4()"),
        ),
        sa.Column("slug", sa.String(length=160), nullable=False),
        sa.Column("title", sa.String(length=300), nullable=False),
        # Human-meaningful date: when the change happened in the real
        # world.  Distinct from ``last_published_at`` (when the entry
        # was published to readers) — a retroactive changelog entry
        # can describe a change that took effect weeks earlier.
        sa.Column("effective_date", sa.Date(), nullable=False),
        sa.Column("body_markdown", sa.Text(), nullable=False),
        sa.Column(
            "status", sa.String(length=20), nullable=False, server_default=sa.text("'DRAFT'")
        ),
        sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("first_published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_published_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.CheckConstraint(_STATUS_CHECK, name="ck_changelog_entries_status"),
    )
    op.create_index(
        "uq_changelog_entries_slug",
        "changelog_entries",
        ["slug"],
        unique=True,
    )
    # Public listing: PUBLISHED entries ordered by effective_date DESC.
    op.execute(
        "CREATE INDEX ix_changelog_entries_published_effective "
        "ON changelog_entries (effective_date DESC, id DESC) "
        "WHERE status = 'PUBLISHED'"
    )

    op.create_table(
        "changelog_entry_revisions",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("uuid_generate_v4()"),
        ),
        sa.Column(
            "entry_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("changelog_entries.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("from_status", sa.String(length=20), nullable=True),
        sa.Column("to_status", sa.String(length=20), nullable=False),
        sa.Column("version_at_revision", sa.Integer(), nullable=False),
        sa.Column("editor_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("transition_reason", sa.Text(), nullable=True),
        sa.Column("correction_note", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index(
        "ix_changelog_entry_revisions_entry",
        "changelog_entry_revisions",
        ["entry_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_changelog_entry_revisions_entry",
        table_name="changelog_entry_revisions",
    )
    op.drop_table("changelog_entry_revisions")
    op.execute("DROP INDEX IF EXISTS ix_changelog_entries_published_effective")
    op.drop_index("uq_changelog_entries_slug", table_name="changelog_entries")
    op.drop_table("changelog_entries")

    op.drop_index(
        "ix_methodology_page_revisions_page",
        table_name="methodology_page_revisions",
    )
    op.drop_table("methodology_page_revisions")
    op.drop_index(
        "ix_methodology_pages_section_order",
        table_name="methodology_pages",
    )
    op.drop_index("uq_methodology_pages_slug", table_name="methodology_pages")
    op.drop_table("methodology_pages")

    op.drop_index(
        "ix_glossary_term_revisions_term",
        table_name="glossary_term_revisions",
    )
    op.drop_table("glossary_term_revisions")
    op.execute("DROP INDEX IF EXISTS ix_glossary_terms_published_term")
    op.drop_index("uq_glossary_terms_term", table_name="glossary_terms")
    op.drop_table("glossary_terms")
