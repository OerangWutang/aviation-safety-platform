"""api_keys role check constraint

Ensure only valid Role values can be stored in api_keys.role.  Any row with a
typo role (e.g. 'admn', 'curator') will now be rejected at the DB level rather
than silently stored and then failing at authentication time.

Safety policy for existing bad rows
------------------------------------
This migration cannot succeed if api_keys already contains rows with role
values outside the valid set.  Rather than silently failing or dropping the
constraint, we:

  1. Detect bad rows before adding the constraint.
  2. Deactivate any key whose role is the legacy ``curator`` value (which was
     used in earlier versions before Role was tightened to analyst/reviewer/admin).
     Curator keys are mapped to ``reviewer`` as the closest equivalent.
  3. Any other unknown role (typos, stale test rows) causes the migration to
     raise an explicit error with remediation instructions rather than letting
     ALTER TABLE fail with a cryptic constraint violation.

Run this migration during a maintenance window if you suspect bad rows exist.
Check first with:

    SELECT role, count(*)
    FROM api_keys
    WHERE role NOT IN ('analyst', 'reviewer', 'admin')
    GROUP BY role;

Revision ID: 014
Revises: 013
"""

import sqlalchemy as sa
from alembic import op

revision = "014"
down_revision = "013"
branch_labels = None
depends_on = None

# Canonical role values that must match atlas.domain.enums.Role exactly.
_VALID_ROLES = ("analyst", "reviewer", "admin")
_CONSTRAINT_NAME = "ck_api_keys_role_valid"


def upgrade() -> None:
    conn = op.get_bind()

    # ── Step 1: Remap legacy 'curator' rows to 'reviewer' ──────────────────
    # 'curator' was used as a role value in early schema versions before the
    # Role enum was tightened.  Map it to 'reviewer' (same permission level in
    # the current model) rather than blocking the migration.
    curator_result = conn.execute(sa.text("SELECT count(*) FROM api_keys WHERE role = 'curator'"))
    curator_count = curator_result.scalar_one()
    if curator_count > 0:
        conn.execute(sa.text("UPDATE api_keys SET role = 'reviewer' WHERE role = 'curator'"))

    # ── Step 2: Reject any remaining unrecognised roles ────────────────────
    # Any role that is neither a known legacy value nor a current valid value
    # is an unexpected state.  Raise before ALTER TABLE so the error message
    # is actionable rather than opaque.
    bad_rows_result = conn.execute(
        sa.text(
            "SELECT role, count(*) AS cnt "
            "FROM api_keys "
            "WHERE role NOT IN ('analyst', 'reviewer', 'admin') "
            "GROUP BY role"
        )
    )
    bad_rows = bad_rows_result.fetchall()
    if bad_rows:
        details = ", ".join(f"'{row[0]}' ({row[1]} rows)" for row in bad_rows)
        raise RuntimeError(
            f"Migration 014 cannot add role CHECK constraint: api_keys contains rows "
            f"with unrecognised role values: {details}. "
            f"Manually update or deactivate these rows before re-running the migration. "
            f"Valid roles are: {_VALID_ROLES!r}."
        )

    # ── Step 3: Add the CHECK constraint ───────────────────────────────────
    op.execute(
        sa.text(
            f"ALTER TABLE api_keys ADD CONSTRAINT {_CONSTRAINT_NAME} "
            f"CHECK (role IN {_VALID_ROLES!r})"
        )
    )


def downgrade() -> None:
    op.execute(sa.text(f"ALTER TABLE api_keys DROP CONSTRAINT IF EXISTS {_CONSTRAINT_NAME}"))
