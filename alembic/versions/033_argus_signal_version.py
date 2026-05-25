"""Argus v0.1 — optimistic-concurrency ``version`` column on argus_signals.

Revision ID: 033
Revises: 032

Adds the ``version`` integer column that ``ReviewArgusSignal`` uses to detect
concurrent reviewer races (mirrors the existing ``claim_conflicts.version``
pattern).  Existing rows are backfilled to 1 by the server-side default;
subsequent reviewer actions bump the version via
``ArgusSignalRepository.update_with_version_check``.

The column is NOT NULL because every reviewer flow requires it.  We add a
CHECK constraint ``version >= 1`` so a programming bug that decrements the
counter is caught at the DB rather than corrupting the audit trail.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "033"
down_revision = "032"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "argus_signals",
        sa.Column(
            "version",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("1"),
        ),
    )
    op.create_check_constraint(
        "ck_argus_signals_version_positive",
        "argus_signals",
        "version >= 1",
    )


def downgrade() -> None:
    op.drop_constraint("ck_argus_signals_version_positive", "argus_signals", type_="check")
    op.drop_column("argus_signals", "version")
