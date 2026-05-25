"""Argus v0.1 — stable list-ordering index for argus_signals.

Revision ID: 032
Revises: 031

``GET /argus/signals`` and ``ArgusSignalRepository.list`` order by
``last_detected_at DESC``.  That column is not unique — a single detection
pass stamps many signals with the same ``now`` — so offset pagination can
silently skip or duplicate rows.  This composite index adds the ``id`` column
as a deterministic tiebreaker.  We omit ``DESC`` modifiers because the
SqlAlchemy ``Index(...)`` helper has no portable way to express per-column
direction and Postgres can scan B-tree indexes in either direction with
equivalent performance.
"""

from __future__ import annotations

from alembic import op

revision = "032"
down_revision = "031"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_argus_signals_last_detected_id_desc",
        "argus_signals",
        ["last_detected_at", "id"],
    )


def downgrade() -> None:
    op.drop_index("ix_argus_signals_last_detected_id_desc", table_name="argus_signals")
