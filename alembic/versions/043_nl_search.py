"""Natural-language search: query log + saved queries (Phase 7).

Revision ID: 043
Revises: 042

Two tables:

- ``nl_query_log`` — anonymised log of every NL query executed.
  Captures the raw text, the structured parse, the result count,
  and a coarse time bucket.  **No user_id column** — we collect
  aggregate query patterns to inform future parser improvements
  (and an eventual embeddings-based replacement), but never
  behavioural traces of individual analysts.  Analysts running NL
  queries sometimes describe sensitive operational concerns; the
  table is designed to be safe to share with researchers if asked.

- ``saved_nl_queries`` — per-user pinned queries.  Carries both
  the original NL text and the structured filters the user
  accepted at save time, so re-running a saved query is stable
  even if the parser's behaviour drifts in a future revision.

Design notes
------------

- ``parsed_filters`` is JSONB.  The parser's output shape evolves;
  storing as JSONB lets a future parser version add new keys
  without a schema change.  Old log rows remain readable.
- ``query_hash`` on the log table is a SHA256 of the lowercased
  query text.  Lets analytics queries group "how often is this
  same query asked?" without re-tokenising on read.
- The ``result_count`` column on the log is denormalised (we know
  it at log-insert time).  Saves an aggregation join when
  computing zero-result-query reports.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "043"
down_revision = "042"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── nl_query_log ────────────────────────────────────────────────
    op.create_table(
        "nl_query_log",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("uuid_generate_v4()"),
        ),
        sa.Column("raw_query", sa.Text(), nullable=False),
        sa.Column(
            "query_hash",
            sa.String(length=64),
            nullable=False,
            comment="SHA256 hex of the lowercased raw_query.",
        ),
        sa.Column("parsed_filters", postgresql.JSONB(), nullable=False),
        sa.Column("result_count", sa.Integer(), nullable=False),
        sa.Column(
            "parser_confidence",
            sa.Float(),
            nullable=False,
            comment="0..1 — what fraction of the query tokens the "
            "parser was able to map onto a structured filter.",
        ),
        # Time bucket is the floor of the call timestamp to the hour.
        # Coarser than created_at; querying analytics by hour buckets
        # is cheap and lets a researcher join against log volume
        # without per-row scanning.
        sa.Column(
            "hour_bucket",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "parser_confidence >= 0.0 AND parser_confidence <= 1.0",
            name="ck_nl_query_log_confidence_range",
        ),
        sa.CheckConstraint("result_count >= 0", name="ck_nl_query_log_result_count_nonneg"),
    )
    op.create_index("ix_nl_query_log_hour_bucket", "nl_query_log", ["hour_bucket"])
    op.create_index("ix_nl_query_log_query_hash", "nl_query_log", ["query_hash"])

    # ── saved_nl_queries ─────────────────────────────────────────────
    op.create_table(
        "saved_nl_queries",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("uuid_generate_v4()"),
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            comment="The user who owns this saved query.",
        ),
        # Free-form human label.  The original NL text is the
        # default suggestion; users can rename to something tidier.
        sa.Column("label", sa.String(length=200), nullable=False),
        # The raw NL text the user typed.  Kept for display and
        # because a future parser revision may extract different
        # filters from the same text — we preserve the original.
        sa.Column("raw_query", sa.Text(), nullable=False),
        # The frozen structured filters as of save time.  Reruns
        # use these, not a fresh parse, so behaviour is stable.
        sa.Column("frozen_filters", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index("ix_saved_nl_queries_user", "saved_nl_queries", ["user_id"])
    # A user can save many queries; we don't unique on (user, label)
    # because users sometimes save near-duplicates while iterating.
    # If duplication becomes a problem we can add a soft-dedupe
    # later.


def downgrade() -> None:
    op.drop_index("ix_saved_nl_queries_user", table_name="saved_nl_queries")
    op.drop_table("saved_nl_queries")
    op.drop_index("ix_nl_query_log_query_hash", table_name="nl_query_log")
    op.drop_index("ix_nl_query_log_hour_bucket", table_name="nl_query_log")
    op.drop_table("nl_query_log")
