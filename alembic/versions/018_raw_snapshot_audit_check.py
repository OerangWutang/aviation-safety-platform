"""raw snapshot audit column consistency check

Enforce that the audit-column pair introduced in migration 016 is populated
together.  New ingestions always set ``raw_payload_hash`` and
``submission_fingerprint_json`` jointly (see
``IngestSourceData.execute_with_result``); the only valid alternate state is
"both NULL" - legacy rows from before 016 (where the backfill left both
columns NULL) plus the post-016 backfill itself (which set ``submission_hash``
to the legacy ``payload_hash`` but left ``raw_payload_hash`` and
``submission_fingerprint_json`` NULL).

Without this constraint the legacy fallback path in
``IngestionIdempotencyService.snapshot_hash_matches`` could silently match a
partially-populated row that does not represent the submission being checked,
because the fallback discriminates "legacy row" purely by these two columns
being NULL.  A future patch that forgets to set one of them on a new
ingestion would re-introduce the same ambiguity.

This constraint is intentionally tolerant: it does not require
``submission_hash`` to be set together with the audit pair, because the 016
backfill populated ``submission_hash`` without populating the pair.

Revision ID: 018
Revises: 017
"""

from __future__ import annotations

from alembic import op

revision = "018"
down_revision = "017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Defensive audit: if any existing row violates the invariant, fail the
    # migration with a clear message rather than silently dropping evidence of
    # a bug elsewhere.  Operators can then triage before re-running.
    op.execute(
        """
        DO $$
        DECLARE
            bad_count bigint;
        BEGIN
            SELECT count(*) INTO bad_count
            FROM raw_snapshots
            WHERE (raw_payload_hash IS NULL) <> (submission_fingerprint_json IS NULL);
            IF bad_count > 0 THEN
                RAISE EXCEPTION
                    'raw_snapshots has % rows with mismatched audit columns; '
                    'investigate before applying check constraint',
                    bad_count;
            END IF;
        END $$;
        """
    )
    op.create_check_constraint(
        "ck_raw_snapshots_audit_pair_consistent",
        "raw_snapshots",
        "(raw_payload_hash IS NULL) = (submission_fingerprint_json IS NULL)",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_raw_snapshots_audit_pair_consistent",
        "raw_snapshots",
        type_="check",
    )
