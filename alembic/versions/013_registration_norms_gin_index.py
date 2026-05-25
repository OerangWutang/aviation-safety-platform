"""Add GIN index on event_identity_index.registration_norms for alias lookup.

``find_by_registration`` (added in v4) queries the ``registration_norms``
JSONB array with the containment operator (``@>``).  Without an index this
is a sequential scan over the whole table.  A GIN index makes it an efficient
index scan.

Index creation strategy
-----------------------
This migration uses plain ``CREATE INDEX IF NOT EXISTS`` (non-concurrent) rather
than ``CREATE INDEX CONCURRENTLY``.  Reasons:

1. Alembic wraps each migration in an explicit transaction by default.
   ``CREATE INDEX CONCURRENTLY`` is not allowed inside a transaction and would
   raise ``ERROR: CREATE INDEX CONCURRENTLY cannot run inside a transaction``.

2. The ``event_identity_index`` table is small at this schema stage (one row
   per accident event), so a brief share lock during index creation is
   acceptable in a maintenance window.

For large production deployments where a share lock is unacceptable, run the
concurrent index creation separately outside Alembic *after* this migration:

    CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_identity_registration_norms_gin
    ON event_identity_index USING gin (registration_norms);

Then downgrade + re-upgrade will find the index already present and skip it
(``IF NOT EXISTS``).
"""

from __future__ import annotations

from alembic import op

revision = "013"
down_revision = "012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Non-concurrent index creation - runs inside the Alembic transaction.
    # See the module docstring for guidance on running CONCURRENTLY in
    # large production deployments outside this migration.
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_identity_registration_norms_gin "
        "ON event_identity_index USING gin (registration_norms)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_identity_registration_norms_gin")
