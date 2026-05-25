"""Hermes leases and Orion strong identity keys.

Revision ID: 034
Revises: 033

Pre-upgrade safety
------------------
The new ``uq_orion_entity_identifiers_active_strong_identity`` unique index
will refuse to create itself if existing rows already violate the invariant.
Before earlier versions persisted this rule in the DB, an advisory lock kept
races out of the *expected* path but the table could still contain duplicate
active identifiers — for example if two ingestion workers raced before the
advisory lock was introduced, or if an older codepath wrote without the
lock.

To avoid a confusing CREATE UNIQUE INDEX error mid-migration, this revision
runs a preflight ``SELECT`` first and aborts with a precise list of the
offending ``(entity_type, identifier_type, normalized_value)`` triples and
their entity ids.  Operators can then run a dedup script (deactivate
duplicates by setting ``valid_to`` on all but one) before retrying.

Set ``ALEMBIC_034_SKIP_DUPLICATE_PREFLIGHT=1`` to bypass the preflight (e.g.
when restoring a backup you have already cleaned).
"""

from __future__ import annotations

import os

import sqlalchemy as sa
from alembic import op

revision = "034"
down_revision = "033"
branch_labels = None
depends_on = None


_PREFLIGHT_SQL = sa.text(
    """
    WITH duplicates AS (
        SELECT
            e.entity_type,
            i.identifier_type,
            i.normalized_value,
            array_agg(i.entity_id ORDER BY i.created_at) AS entity_ids,
            count(*) AS dup_count
        FROM orion_entity_identifiers AS i
        JOIN orion_entities AS e ON e.id = i.entity_id
        WHERE i.valid_to IS NULL
        GROUP BY e.entity_type, i.identifier_type, i.normalized_value
        HAVING count(*) > 1
    )
    SELECT entity_type, identifier_type, normalized_value, entity_ids, dup_count
    FROM duplicates
    ORDER BY dup_count DESC, entity_type, identifier_type, normalized_value
    LIMIT 50
    """
)


def _check_for_duplicate_active_identifiers() -> None:
    """Abort the migration if duplicate active identifiers already exist.

    The unique partial index this migration adds is the database-level
    invariant; existing duplicates would otherwise cause the
    ``CREATE UNIQUE INDEX`` statement to fail with a less-actionable error
    in the middle of the migration after several columns have already been
    altered.

    The output is intentionally a compact, copy-pasteable list of the
    offending triples so operators can write a one-liner to deactivate
    them.
    """
    if os.environ.get("ALEMBIC_034_SKIP_DUPLICATE_PREFLIGHT") == "1":
        return

    bind = op.get_bind()
    # ``orion_entity_identifiers`` does not have ``entity_type`` yet — that
    # column is the whole point of this migration — so the preflight joins
    # ``orion_entities`` to compute it.
    rows = bind.execute(_PREFLIGHT_SQL).fetchall()
    if not rows:
        return

    lines = [
        "Migration 034 cannot create the active-identifier unique index "
        "because duplicate active rows already exist in "
        "orion_entity_identifiers. Resolve the duplicates "
        "(set valid_to on all but one for each group), then re-run alembic "
        "upgrade. Set ALEMBIC_034_SKIP_DUPLICATE_PREFLIGHT=1 to bypass this "
        "check when you have already cleaned the data.",
        "",
        "Offending (entity_type, identifier_type, normalized_value) groups:",
    ]
    for row in rows:
        # row order matches the SELECT above.
        entity_type, identifier_type, normalized_value, entity_ids, dup_count = row
        ids_repr = ", ".join(str(eid) for eid in entity_ids)
        lines.append(
            f"  - ({entity_type!r}, {identifier_type!r}, {normalized_value!r}) "
            f"x{dup_count} across entity_ids=[{ids_repr}]"
        )
    raise RuntimeError("\n".join(lines))


def upgrade() -> None:
    # Run the Orion duplicate-identifier preflight FIRST, before touching any
    # schema.  If duplicates exist the migration aborts cleanly with zero DDL
    # applied and a copy-pasteable list of offending rows; rerunning after
    # cleanup is safe.  Running the preflight before DDL keeps failures clean
    # and easy to reason about, without depending on backend-specific
    # transactional DDL behaviour or idempotent reruns.
    _check_for_duplicate_active_identifiers()

    op.add_column("hermes_fetch_jobs", sa.Column("locked_by", sa.String(length=200), nullable=True))
    op.add_column(
        "hermes_fetch_jobs", sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "hermes_fetch_jobs",
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_hermes_fetch_jobs_locked_by", "hermes_fetch_jobs", ["locked_by"])
    op.create_index(
        "ix_hermes_fetch_jobs_lease_expires_at", "hermes_fetch_jobs", ["lease_expires_at"]
    )
    op.create_index(
        "ix_hermes_fetch_jobs_stale_running",
        "hermes_fetch_jobs",
        ["lease_expires_at", "id"],
        postgresql_where=sa.text("status = 'RUNNING' AND lease_expires_at IS NOT NULL"),
    )

    op.add_column(
        "orion_entity_identifiers", sa.Column("entity_type", sa.String(length=50), nullable=True)
    )
    op.execute(
        """
        UPDATE orion_entity_identifiers AS i
        SET entity_type = e.entity_type
        FROM orion_entities AS e
        WHERE i.entity_id = e.id
        """
    )
    op.alter_column("orion_entity_identifiers", "entity_type", nullable=False)
    op.create_index(
        "ix_orion_entity_identifiers_entity_type", "orion_entity_identifiers", ["entity_type"]
    )
    op.create_index(
        "uq_orion_entity_identifiers_active_strong_identity",
        "orion_entity_identifiers",
        ["entity_type", "identifier_type", "normalized_value"],
        unique=True,
        postgresql_where=sa.text("valid_to IS NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_orion_entity_identifiers_active_strong_identity", table_name="orion_entity_identifiers"
    )
    op.drop_index("ix_orion_entity_identifiers_entity_type", table_name="orion_entity_identifiers")
    op.drop_column("orion_entity_identifiers", "entity_type")

    op.drop_index("ix_hermes_fetch_jobs_stale_running", table_name="hermes_fetch_jobs")
    op.drop_index("ix_hermes_fetch_jobs_lease_expires_at", table_name="hermes_fetch_jobs")
    op.drop_index("ix_hermes_fetch_jobs_locked_by", table_name="hermes_fetch_jobs")
    op.drop_column("hermes_fetch_jobs", "lease_expires_at")
    op.drop_column("hermes_fetch_jobs", "locked_at")
    op.drop_column("hermes_fetch_jobs", "locked_by")
