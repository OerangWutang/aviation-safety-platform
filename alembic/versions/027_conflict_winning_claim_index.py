"""Add resolved-conflict winning-claim lookup index.

Revision ID: 027
Revises: 026

Claim supersession checks ask whether a claim previously won a resolved
conflict so later evidence updates can reopen the right dispute.  This partial
index keeps those lookups bounded as the conflict table grows while avoiding
index bloat for OPEN rows and unresolved SYSTEM_AUTO_CLOSED tombstones.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "027"
down_revision = "026"
branch_labels = None
depends_on = None


_INDEX_WHERE = "status = 'RESOLVED' AND winning_claim_id IS NOT NULL"


def upgrade() -> None:
    op.create_index(
        "ix_claim_conflicts_resolved_winning_claim",
        "claim_conflicts",
        ["winning_claim_id"],
        postgresql_where=sa.text(_INDEX_WHERE),
    )


def downgrade() -> None:
    op.drop_index("ix_claim_conflicts_resolved_winning_claim", table_name="claim_conflicts")
