"""sources reliability_tier check constraint

Add a database-level CHECK constraint that enforces ``reliability_tier >= 1``
on the ``sources`` table.  The domain model already validates this via Pydantic
(``Field(ge=1)``), but a DB-level constraint ensures the invariant holds even
for raw SQL writes (migrations, scripts, emergency fixes).

Since reliability_tier was introduced in migration 002 with a default of 1, all
existing rows already satisfy the constraint; no backfill is required.

Revision ID: 015
Revises: 014
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "015"
down_revision = "014"
branch_labels = None
depends_on = None

_CONSTRAINT_NAME = "ck_sources_reliability_tier_ge_1"


def upgrade() -> None:
    # Safety: detect any existing rows that would violate the constraint before
    # adding it.  reliability_tier was always defaulted to 1 and validated by
    # the domain layer, so this should never fire in practice.
    conn = op.get_bind()
    bad = conn.execute(sa.text("SELECT COUNT(*) FROM sources WHERE reliability_tier < 1")).scalar()
    if bad:
        raise RuntimeError(
            f"Cannot add {_CONSTRAINT_NAME}: {bad} source row(s) have reliability_tier < 1. "
            "Fix them manually before running this migration."
        )

    op.execute(
        sa.text(
            f"ALTER TABLE sources ADD CONSTRAINT {_CONSTRAINT_NAME} CHECK (reliability_tier >= 1)"
        )
    )


def downgrade() -> None:
    op.execute(sa.text(f"ALTER TABLE sources DROP CONSTRAINT IF EXISTS {_CONSTRAINT_NAME}"))
