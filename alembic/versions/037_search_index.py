"""Public event search index (Phase 2).

Revision ID: 037
Revises: 036

A materialized full-text search index over PUBLISHED public event
pages.  Rebuilt synchronously from Phase 9's publication hooks
(``PublishPublicEventPage``, ``ArchivePublicEventPage``,
``RetractPublicEventPage``), so the invariant "search only indexes
PUBLISHED rows" is enforced by the use cases that own the
publication state machine.

Design notes
------------

- One row per ``public_event_pages.id``.  Carries the publication's
  searchable text plus de-normalised projection fields so the search
  response doesn't N+1 over projections at query time.
- The ``tsvector`` column is stored, not generated.  A generated
  column would have to live on ``public_event_pages`` and pull from
  joined tables, which Postgres doesn't support; storing it here
  with weights baked in is simpler and faster.
- Weighted ranking: title (A) > short_summary (B) > projection fields
  (C) > narrative (D).  Set at write time in the repository's
  ``upsert`` so query-time ``ts_rank_cd`` is deterministic.
- GIN index for the tsvector; B-tree partial indexes for the keyable
  filters that read cleanly from the projection.
- The table holds *only* PUBLISHED pages.  Archive/retract delete the
  row; publish inserts or updates it.  No status column needed.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "037"
down_revision = "036"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "search_index_entries",
        sa.Column(
            "page_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("public_event_pages.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        # Materialised payload so search responses are single-table
        # reads.  Stay in sync via the publication lifecycle hooks.
        sa.Column("slug", sa.String(length=160), nullable=False),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("short_summary", sa.Text(), nullable=True),
        # De-normalised projection facets.  Nullable because not every
        # projection has every field.
        sa.Column("operator", sa.String(length=300), nullable=True),
        sa.Column("aircraft_type", sa.String(length=300), nullable=True),
        sa.Column("country", sa.String(length=300), nullable=True),
        sa.Column("event_date", sa.Date(), nullable=True),
        sa.Column("fatalities_total", sa.Integer(), nullable=True),
        # Coarse confidence band; the public list uses the same shape.
        # Pre-computed at write time so queries don't need to evaluate
        # completeness_score thresholds in SQL.
        sa.Column("confidence_band", sa.String(length=10), nullable=False),
        sa.Column(
            "last_published_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "indexed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        # The weighted tsvector.  Built in the repository with
        # setweight() so query-time ts_rank_cd uses the bake-in
        # weights.
        sa.Column(
            "search_vector",
            postgresql.TSVECTOR(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "confidence_band IN ('high', 'medium', 'low', 'unknown')",
            name="ck_search_index_entries_confidence_band",
        ),
    )

    # GIN over the tsvector — the canonical FTS index shape.
    op.create_index(
        "ix_search_index_entries_search_vector",
        "search_index_entries",
        ["search_vector"],
        postgresql_using="gin",
    )

    # B-tree indexes on the small set of equality-keyable filter
    # facets.  These are sized so a filter+text query can intersect
    # the GIN match with a cheap filter scan.  We deliberately do not
    # index every facet — adding indexes for low-cardinality columns
    # like country is rarely a planner win and bloats writes.
    op.create_index(
        "ix_search_index_entries_operator",
        "search_index_entries",
        ["operator"],
    )
    op.create_index(
        "ix_search_index_entries_aircraft_type",
        "search_index_entries",
        ["aircraft_type"],
    )
    op.create_index(
        "ix_search_index_entries_event_date",
        "search_index_entries",
        ["event_date"],
    )
    # Composite index supporting the empty-query "newest published"
    # fallback (search with no text returns recent rows).
    op.create_index(
        "ix_search_index_entries_pub_id",
        "search_index_entries",
        [sa.text("last_published_at DESC"), sa.text("page_id DESC")],
    )


def downgrade() -> None:
    op.drop_index("ix_search_index_entries_pub_id", table_name="search_index_entries")
    op.drop_index("ix_search_index_entries_event_date", table_name="search_index_entries")
    op.drop_index(
        "ix_search_index_entries_aircraft_type",
        table_name="search_index_entries",
    )
    op.drop_index("ix_search_index_entries_operator", table_name="search_index_entries")
    op.drop_index(
        "ix_search_index_entries_search_vector",
        table_name="search_index_entries",
    )
    op.drop_table("search_index_entries")
