"""Durable Echo cross-reference outbox events.

Revision ID: 048
Revises: 047
Create Date: 2026-05-24
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "048"
down_revision = "047"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("ck_outbox_events_event_type", "outbox_events", type_="check")
    op.create_check_constraint(
        "ck_outbox_events_event_type",
        "outbox_events",
        "event_type IN ('CLAIMS_UPDATED', 'ECHO_CROSSREF_REQUESTED')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_outbox_events_event_type", "outbox_events", type_="check")
    op.create_check_constraint(
        "ck_outbox_events_event_type",
        "outbox_events",
        "event_type IN ('CLAIMS_UPDATED')",
    )
