"""Metering domain entities (Phase 8).

``UsageEvent`` is the immutable append-only row.  ``UsageDailyRollup``
is the per-day aggregate.  ``UsageSummaryRow`` is a non-persisted
DTO used in the admin summary endpoint.

The ``MetricKind`` enum lists the metered actions.  Adding new
metrics requires updating both the enum here AND the CHECK
constraint in a follow-up migration — the schema is the
authoritative source.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from uuid import UUID, uuid4

from pydantic import Field

from atlas.domain.entities import DomainModel
from atlas.domain.utils import utc_now


class MetricKind(StrEnum):
    """The closed set of metered actions.

    Each value is the stable string identifier used in
    ``usage_events.metric_kind`` and ``usage_daily_rollups.metric_kind``.
    New metrics in a future phase require:

    1. A new enum value here.
    2. A schema migration that updates the CHECK constraint on
       both tables.
    3. A call site in the action's use case (typically the last
       step before commit).
    """

    TENANT_CLAIM_INGESTED = "TENANT_CLAIM_INGESTED"
    TENANT_REPORT_FILED = "TENANT_REPORT_FILED"
    TENANT_INGESTION_RUN_COMPLETED = "TENANT_INGESTION_RUN_COMPLETED"
    NL_QUERY_EXECUTED = "NL_QUERY_EXECUTED"
    HFACS_ATTRIBUTION_CREATED = "HFACS_ATTRIBUTION_CREATED"
    ECHO_CROSSREF_RUN = "ECHO_CROSSREF_RUN"


# Tuple for fast membership checks; matches the migration's CHECK
# constraint.  Mirrors the StrEnum but exists as a separate
# constant so callers without StrEnum awareness (config files,
# CLI tools) have a list to iterate.
METRIC_KINDS: tuple[str, ...] = tuple(k.value for k in MetricKind)


class UsageEvent(DomainModel):
    """One immutable metered action.

    ``tenant_id`` is optional because some metrics
    (``NL_QUERY_EXECUTED``) are not tenant-scoped.  ``user_id`` is
    optional because some metrics are system-driven.  ``resource_id``
    is the optional pointer to the resource the action operated on
    (event id, claim id, etc.) — denormalised, no FK, kept for
    audit even if the resource is later deleted.
    """

    id: UUID = Field(default_factory=uuid4)
    metric_kind: MetricKind
    tenant_id: UUID | None = None
    user_id: UUID | None = None
    resource_id: UUID | None = None
    recorded_at: datetime = Field(default_factory=utc_now)


class UsageDailyRollup(DomainModel):
    """One per-tenant, per-day, per-metric aggregate row.

    ``tenant_id`` is non-nullable here — the rollup table encodes
    "no tenant" via the sentinel UUID
    ``00000000-0000-0000-0000-000000000000`` so the unique
    constraint ``(tenant_id, metric_kind, day)`` works without a
    partial index.  Consumers reading the rollup are expected to
    map the sentinel back to "system-wide" for display.
    """

    id: UUID = Field(default_factory=uuid4)
    tenant_id: UUID
    metric_kind: MetricKind
    day: date
    count: int = Field(default=0, ge=0)
    computed_at: datetime = Field(default_factory=utc_now)


class UsageSummaryRow(DomainModel):
    """A non-persisted DTO returned by the admin summary endpoint.

    Carries a tenant id, a metric kind, and the summed count over
    the requested date range.  ``tenant_id`` is the real tenant id
    (sentinel mapped back to None) and ``tenant_slug`` is the
    optional human-readable label for UI rendering.
    """

    tenant_id: UUID | None
    tenant_slug: str | None
    metric_kind: MetricKind
    total_count: int = Field(ge=0)
