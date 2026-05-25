"""Metering use cases (Phase 8).

Three use cases:

- :class:`ComputeDailyRollups` — compact ``usage_events`` into
  ``usage_daily_rollups`` for a (tenant, metric, date) grid.
  Idempotent.
- :class:`GetTenantUsage` — a tenant reads its own rollups over a
  date range (three-layer isolation).
- :class:`GetAdminUsageSummary` — the operator reads a per-tenant
  breakdown across all tenants.

The rollup computer is deliberately a use case, not a DB trigger
or a service-layer side effect: operators schedule it (nightly,
hourly, on-demand) however suits their billing cadence.  It reads
events, sums per (tenant, metric, day), and UPSERTs — re-running
for a day overwrites rather than double-counting.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from uuid import UUID

from atlas.application.unit_of_work import UnitOfWork
from atlas.domain.metering import NO_TENANT_SENTINEL
from atlas.domain.metering.entities import (
    MetricKind,
    UsageDailyRollup,
    UsageSummaryRow,
)
from atlas.domain.tenancy.exceptions import CrossTenantAccessError
from atlas.domain.utils import utc_now

_SENTINEL_TENANT = UUID(NO_TENANT_SENTINEL)

# Which metrics are tenant-scoped vs system-wide.  System-wide
# metrics roll up under the sentinel tenant id.  Keeping this as an
# explicit mapping (rather than inferring from the events) means the
# rollup grid is complete even on days with zero events for a
# metric — a zero-count row is still written, which makes "no usage"
# distinguishable from "rollup never ran".
_TENANT_SCOPED_METRICS = (
    MetricKind.TENANT_CLAIM_INGESTED,
    MetricKind.TENANT_REPORT_FILED,
    MetricKind.TENANT_INGESTION_RUN_COMPLETED,
)
# HFACS attributions and NL queries are editorial/public actions on
# the shared corpus, not tenant-scoped.  They roll up under the
# sentinel tenant id.  (HFACS attributions carry an editor user_id
# but no tenant — they're operator-side editorial work.)
_SYSTEM_METRICS = (
    MetricKind.NL_QUERY_EXECUTED,
    MetricKind.HFACS_ATTRIBUTION_CREATED,
)


# ── Compute daily rollups ───────────────────────────────────────────────────


@dataclass(frozen=True)
class ComputeDailyRollupsInput:
    day_from: date
    day_to: date
    # Optional tenant filter; None means "all tenants in the
    # system" (the use case enumerates tenants from the repo).
    tenant_ids: list[UUID] | None = None


@dataclass(frozen=True)
class ComputeDailyRollupsResult:
    rows_written: int


class ComputeDailyRollups:
    """Compact events into daily rollups for a date range.

    For each (tenant, metric, day) cell, counts the matching events
    and UPSERTs a rollup row.  Writes zero-count rows too, so a
    consumer can tell "no usage that day" from "rollup hasn't run".
    """

    def __init__(self, uow: UnitOfWork):
        self._uow = uow

    async def execute(self, input: ComputeDailyRollupsInput) -> ComputeDailyRollupsResult:
        rows_written = 0
        moment = utc_now()

        # Iterate each day in the inclusive range.
        for day in _iter_days(input.day_from, input.day_to):
            start, end = _day_bounds(day)

            # Resolve the tenant set for this day: explicit list, or
            # the tenants that actually had events that day.  Using
            # per-day distinct tenants keeps the rollup grid honest
            # (we don't write zero rows for tenants that didn't exist
            # yet) while staying independent of a tenant-directory
            # enumeration.
            if input.tenant_ids is not None:
                day_tenant_ids = list(input.tenant_ids)
            else:
                day_tenant_ids = await self._uow.usage_events.distinct_tenants_in_range(
                    start=start, end=end
                )

            # Tenant-scoped metrics: one rollup per (tenant, metric).
            for tenant_id in day_tenant_ids:
                for metric in _TENANT_SCOPED_METRICS:
                    count = await self._uow.usage_events.count_in_range(
                        tenant_id=tenant_id,
                        metric_kind=metric,
                        start=start,
                        end=end,
                    )
                    await self._uow.usage_daily_rollups.upsert(
                        UsageDailyRollup(
                            tenant_id=tenant_id,
                            metric_kind=metric,
                            day=day,
                            count=count,
                            computed_at=moment,
                        )
                    )
                    rows_written += 1

            # System-wide metrics roll up under the sentinel tenant.
            for metric in _SYSTEM_METRICS:
                count = await self._uow.usage_events.count_in_range(
                    tenant_id=None,
                    metric_kind=metric,
                    start=start,
                    end=end,
                )
                await self._uow.usage_daily_rollups.upsert(
                    UsageDailyRollup(
                        tenant_id=_SENTINEL_TENANT,
                        metric_kind=metric,
                        day=day,
                        count=count,
                        computed_at=moment,
                    )
                )
                rows_written += 1

        await self._uow.commit()
        return ComputeDailyRollupsResult(rows_written=rows_written)


# ── Tenant usage read ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class GetTenantUsageInput:
    tenant_id: UUID
    caller_tenant_id: UUID
    day_from: date
    day_to: date


class GetTenantUsage:
    """A tenant reads its own daily rollups.

    Three-layer isolation: the auth gate (router) verifies the
    caller is a member of the tenant; this use case verifies
    ``caller_tenant_id == tenant_id``; the repo filters on
    ``tenant_id``.
    """

    def __init__(self, uow: UnitOfWork):
        self._uow = uow

    async def execute(self, input: GetTenantUsageInput) -> list[UsageDailyRollup]:
        if input.caller_tenant_id != input.tenant_id:
            raise CrossTenantAccessError(
                caller_tenant_id=input.caller_tenant_id,
                target_tenant_id=input.tenant_id,
            )
        result = await self._uow.usage_daily_rollups.list_for_tenant(
            tenant_id=input.tenant_id,
            day_from=input.day_from,
            day_to=input.day_to,
        )
        await self._uow.rollback()
        return result


# ── Admin usage summary ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class GetAdminUsageSummaryInput:
    day_from: date
    day_to: date


class GetAdminUsageSummary:
    """Operator-facing per-tenant usage breakdown across all tenants.

    Admin-only — the router gates on the ADMIN role.  No tenant
    filtering; the operator sees everything for capacity planning
    and pricing.
    """

    def __init__(self, uow: UnitOfWork):
        self._uow = uow

    async def execute(self, input: GetAdminUsageSummaryInput) -> list[UsageSummaryRow]:
        result = await self._uow.usage_daily_rollups.summary_across_tenants(
            day_from=input.day_from,
            day_to=input.day_to,
        )
        await self._uow.rollback()
        return result


# ── Helpers ─────────────────────────────────────────────────────────────────


def _iter_days(day_from: date, day_to: date) -> Iterator[date]:
    """Yield each date in the inclusive range [day_from, day_to]."""
    current = day_from
    while current <= day_to:
        yield current
        current = current + timedelta(days=1)


def _day_bounds(day: date) -> tuple[datetime, datetime]:
    """Return (start, end) datetimes bounding a calendar day in UTC.

    Start is midnight that day; end is midnight the next day.  The
    count query uses inclusive-start, exclusive-end so events at
    exactly midnight belong to the day that's starting.
    """
    start = datetime.combine(day, time.min, tzinfo=UTC)
    end = start + timedelta(days=1)
    return start, end


__all__ = [
    "ComputeDailyRollups",
    "ComputeDailyRollupsInput",
    "ComputeDailyRollupsResult",
    "GetAdminUsageSummary",
    "GetAdminUsageSummaryInput",
    "GetTenantUsage",
    "GetTenantUsageInput",
]
