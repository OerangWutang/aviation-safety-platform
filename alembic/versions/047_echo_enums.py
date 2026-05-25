"""Extend Argus and metering enums for Echo cross-reference signals.

Revision ID: 047
Revises: 046
Create Date: 2026-05-23

Adds three new values to existing CHECK-constrained columns:

* ``argus_signals.signal_type``    += 'ECHO_STRONG_PRECEDENT_MATCH'
* ``argus_signal_evidence.evidence_type`` += 'ECHO_CROSSREF_RESULT'
* ``usage_events.metric_kind``     += 'ECHO_CROSSREF_RUN'
* ``usage_daily_rollups.metric_kind`` += 'ECHO_CROSSREF_RUN'

Postgres CHECK constraints are not additive — you cannot ALTER a
CHECK to add a value.  The standard pattern here (and the one used by
migrations 031 and 044) is DROP + CREATE on the constraint.  This is
safe with zero downtime because:

1. The constraint is named (not anonymous) so DROP is surgical.
2. No existing rows violate the new constraint — we are only *adding*
   a value to the allowed set, never removing one.
3. Each DROP + CREATE is inside the same transaction that Alembic
   wraps the migration in, so either both happen or neither does.
"""

from __future__ import annotations

from alembic import op

revision = "047"
down_revision = "046"
branch_labels = None
depends_on = None

# Full value sets after this migration.  Keeping them here (rather than
# reading from the domain enums at migration time) makes the migration
# self-contained and independently auditable.
_SIGNAL_TYPES_NEW = (
    "NEW_SOURCE_CHANGE",
    "TIMELINE_SEQUENCE_CONFLICT",
    "HIGH_CONFLICT_ACCIDENT_RECORD",
    "REPEATED_AIRCRAFT_INVOLVEMENT",
    "REPEATED_OPERATOR_INVOLVEMENT",
    "SOURCE_FETCH_FAILURE_SPIKE",
    "ECHO_STRONG_PRECEDENT_MATCH",
)
_EVIDENCE_TYPES_NEW = (
    "ATLAS_CLAIM",
    "ATLAS_CONFLICT",
    "ATLAS_ACCIDENT_EVENT",
    "ORION_ENTITY",
    "ORION_RELATIONSHIP",
    "CHRONOS_TIMELINE_EVENT",
    "CHRONOS_SEQUENCE_REVIEW",
    "HERMES_SOURCE_CHANGE",
    "HERMES_FETCH_JOB",
    "HERMES_FETCHED_DOCUMENT",
    "ECHO_CROSSREF_RESULT",
)
_METRIC_KINDS_NEW = (
    "TENANT_CLAIM_INGESTED",
    "TENANT_REPORT_FILED",
    "TENANT_INGESTION_RUN_COMPLETED",
    "NL_QUERY_EXECUTED",
    "HFACS_ATTRIBUTION_CREATED",
    "ECHO_CROSSREF_RUN",
)

# Pre-migration value sets (for downgrade).
_SIGNAL_TYPES_OLD = _SIGNAL_TYPES_NEW[:-1]
_EVIDENCE_TYPES_OLD = _EVIDENCE_TYPES_NEW[:-1]
_METRIC_KINDS_OLD = _METRIC_KINDS_NEW[:-1]


def _in_list(values: tuple[str, ...]) -> str:
    return ", ".join(repr(v) for v in values)


def upgrade() -> None:
    # argus_signals.signal_type
    op.drop_constraint("ck_argus_signals_signal_type", "argus_signals")
    op.create_check_constraint(
        "ck_argus_signals_signal_type",
        "argus_signals",
        f"signal_type IN ({_in_list(_SIGNAL_TYPES_NEW)})",
    )

    # argus_signal_evidence.evidence_type
    op.drop_constraint("ck_argus_signal_evidence_type", "argus_signal_evidence")
    op.create_check_constraint(
        "ck_argus_signal_evidence_type",
        "argus_signal_evidence",
        f"evidence_type IN ({_in_list(_EVIDENCE_TYPES_NEW)})",
    )

    # usage_events.metric_kind and usage_daily_rollups.metric_kind
    for table, name in (
        ("usage_events", "ck_usage_events_metric_kind"),
        ("usage_daily_rollups", "ck_usage_daily_rollups_metric_kind"),
    ):
        op.drop_constraint(name, table)
        op.create_check_constraint(
            name,
            table,
            f"metric_kind IN ({_in_list(_METRIC_KINDS_NEW)})",
        )


def downgrade() -> None:
    # Downgrade is only safe if no rows use the new values.
    for table, name in (
        ("usage_events", "ck_usage_events_metric_kind"),
        ("usage_daily_rollups", "ck_usage_daily_rollups_metric_kind"),
    ):
        op.drop_constraint(name, table)
        op.create_check_constraint(
            name,
            table,
            f"metric_kind IN ({_in_list(_METRIC_KINDS_OLD)})",
        )

    op.drop_constraint("ck_argus_signal_evidence_type", "argus_signal_evidence")
    op.create_check_constraint(
        "ck_argus_signal_evidence_type",
        "argus_signal_evidence",
        f"evidence_type IN ({_in_list(_EVIDENCE_TYPES_OLD)})",
    )

    op.drop_constraint("ck_argus_signals_signal_type", "argus_signals")
    op.create_check_constraint(
        "ck_argus_signals_signal_type",
        "argus_signals",
        f"signal_type IN ({_in_list(_SIGNAL_TYPES_OLD)})",
    )
