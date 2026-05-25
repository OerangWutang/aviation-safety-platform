"""Add registration_norms JSONB array to event_identity_index.

Previously the identity index kept only one normalised registration per event
(registration_norm scalar).  When two sources assert different registrations
for the same accident - a genuine data conflict - the upsert COALESCE logic
kept only the latest value.  Future anonymous ingestions reporting the earlier
registration failed to score a registration match and silently created a new
duplicate event with no curator review.

This migration adds ``registration_norms``, a JSONB array that accumulates
*every* normalised registration ever asserted for the event.  The upsert
performs a distinct array union (same as source_record_ids) so no historical
alias is ever lost.

Matching behaviour (``EventMatcher.score_match``)
-------------------------------------------------
* An incoming registration that matches ``registration_norm`` (the primary,
  most-recently-confirmed registration) scores the full ``_WEIGHTS["registration"]``
  weight (0.45).
* An incoming registration that matches any entry in ``registration_norms``
  (historical aliases) but *not* the primary scores **0.5 x 0.45 = 0.225**.
  Combined with a date match (0.30) the typical alias-match total is ~0.525,
  which falls in the UNCERTAIN_LOW..HIGH_CONFIDENCE band - enough to queue a
  duplicate review but *not* enough to auto-attach.

This intentional half-weight prevents corrected-away or historically-conflicting
registrations from silently attaching future ingestions to the wrong event.

Revision ID: 012
Revises: 011
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "012"
down_revision = "011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "event_identity_index",
        sa.Column(
            "registration_norms",
            JSONB,
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    # Back-fill: seed the new array from the existing scalar column for all
    # rows that already have a registration_norm.  New ingestions will union
    # additional aliases into this array going forward.
    op.execute(
        """
        UPDATE event_identity_index
        SET    registration_norms = jsonb_build_array(registration_norm)
        WHERE  registration_norm IS NOT NULL
        """
    )


def downgrade() -> None:
    op.drop_column("event_identity_index", "registration_norms")
