"""Cap event identity historical registration aliases.

Revision ID: 020
Revises: 019

The identity index keeps historical registrations as low-confidence aliases.
Capping the JSONB array prevents bad mappings or malicious payloads from
amplifying future duplicate-review candidates indefinitely.
"""

from __future__ import annotations

from alembic import op

revision = "020"
down_revision = "019"
branch_labels = None
depends_on = None

MAX_REGISTRATION_ALIASES = 5


def upgrade() -> None:
    op.execute(
        f"""
        UPDATE event_identity_index AS e
        SET registration_norms = COALESCE(
            (
                SELECT jsonb_agg(value ORDER BY last_pos)
                FROM (
                    SELECT elem AS value, MAX(ord) AS last_pos
                    FROM jsonb_array_elements(e.registration_norms)
                        WITH ORDINALITY AS t(elem, ord)
                    GROUP BY elem
                    ORDER BY MAX(ord) DESC
                    LIMIT {MAX_REGISTRATION_ALIASES}
                ) AS capped_aliases
            ),
            '[]'::jsonb
        )
        WHERE jsonb_typeof(e.registration_norms) = 'array'
          AND jsonb_array_length(e.registration_norms) > {MAX_REGISTRATION_ALIASES}
        """
    )


def downgrade() -> None:
    # The pruned historical aliases cannot be reconstructed safely.
    pass
