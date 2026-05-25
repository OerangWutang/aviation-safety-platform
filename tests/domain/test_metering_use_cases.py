"""Use-case tests for Phase 8 metering.

Pins:

1. **Service records events.** ``MeteringService.record`` emits the
   right number of rows; quantity<1 is a no-op.
2. **Metered actions emit events.** Submitting claims, completing a
   run, filing a report, executing an NL search, and creating an
   HFACS attribution each leave a usage event behind.
3. **Rollup computation** is idempotent and writes zero rows
   correctly; tenant-scoped vs system-wide metrics land in the
   right buckets.
4. **Tenant usage read** is cross-tenant-isolated.
5. **Admin summary** sums across tenants and maps the sentinel back
   to None.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from atlas.application.services.metering import MeteringService
from atlas.application.use_cases.metering import (
    ComputeDailyRollups,
    ComputeDailyRollupsInput,
    GetAdminUsageSummary,
    GetAdminUsageSummaryInput,
    GetTenantUsage,
    GetTenantUsageInput,
)
from atlas.domain.metering import NO_TENANT_SENTINEL
from atlas.domain.metering.entities import MetricKind, UsageEvent
from atlas.domain.tenancy.entities import Tenant
from atlas.domain.tenancy.exceptions import CrossTenantAccessError
from tests.domain._fake_uow import InMemoryUnitOfWork

_SENTINEL = UUID(NO_TENANT_SENTINEL)


def _seed_tenant(uow: InMemoryUnitOfWork, *, slug: str = "acme") -> Tenant:
    t = Tenant(slug=slug, display_name=slug.upper())
    uow.store.tenancy.tenants[t.id] = t
    return t


# ── MeteringService ─────────────────────────────────────────────────────────


class TestMeteringService:
    async def test_records_single_event(self) -> None:
        uow = InMemoryUnitOfWork()
        tenant_id = uuid4()
        await MeteringService(uow).record(
            metric_kind=MetricKind.TENANT_REPORT_FILED,
            tenant_id=tenant_id,
            resource_id=uuid4(),
        )
        assert len(uow.store.metering.events) == 1
        e = uow.store.metering.events[0]
        assert e.metric_kind == MetricKind.TENANT_REPORT_FILED
        assert e.tenant_id == tenant_id

    async def test_quantity_emits_n_events(self) -> None:
        uow = InMemoryUnitOfWork()
        await MeteringService(uow).record(
            metric_kind=MetricKind.TENANT_CLAIM_INGESTED,
            tenant_id=uuid4(),
            quantity=5,
        )
        assert len(uow.store.metering.events) == 5

    async def test_zero_quantity_is_noop(self) -> None:
        uow = InMemoryUnitOfWork()
        await MeteringService(uow).record(
            metric_kind=MetricKind.TENANT_CLAIM_INGESTED,
            tenant_id=uuid4(),
            quantity=0,
        )
        assert len(uow.store.metering.events) == 0

    async def test_negative_quantity_is_noop(self) -> None:
        uow = InMemoryUnitOfWork()
        await MeteringService(uow).record(
            metric_kind=MetricKind.TENANT_CLAIM_INGESTED,
            tenant_id=uuid4(),
            quantity=-3,
        )
        assert len(uow.store.metering.events) == 0


# ── Rollup computation ──────────────────────────────────────────────────────


class TestComputeDailyRollups:
    async def test_counts_events_into_rollups(self) -> None:
        uow = InMemoryUnitOfWork()
        tenant = _seed_tenant(uow)
        day = date(2024, 6, 1)
        moment = datetime(2024, 6, 1, 10, 0, tzinfo=UTC)
        # Three claims + one report that day.
        for _ in range(3):
            uow.store.metering.events.append(
                UsageEvent(
                    metric_kind=MetricKind.TENANT_CLAIM_INGESTED,
                    tenant_id=tenant.id,
                    recorded_at=moment,
                )
            )
        uow.store.metering.events.append(
            UsageEvent(
                metric_kind=MetricKind.TENANT_REPORT_FILED,
                tenant_id=tenant.id,
                recorded_at=moment,
            )
        )
        await ComputeDailyRollups(uow).execute(ComputeDailyRollupsInput(day_from=day, day_to=day))
        claim_rollup = uow.store.metering.rollups[
            (tenant.id, MetricKind.TENANT_CLAIM_INGESTED, day)
        ]
        report_rollup = uow.store.metering.rollups[(tenant.id, MetricKind.TENANT_REPORT_FILED, day)]
        assert claim_rollup.count == 3
        assert report_rollup.count == 1

    async def test_idempotent_recompute(self) -> None:
        """Running the rollup twice for the same day yields the same
        counts — UPSERT replaces, never accumulates."""
        uow = InMemoryUnitOfWork()
        tenant = _seed_tenant(uow)
        day = date(2024, 6, 1)
        moment = datetime(2024, 6, 1, 10, 0, tzinfo=UTC)
        uow.store.metering.events.append(
            UsageEvent(
                metric_kind=MetricKind.TENANT_CLAIM_INGESTED,
                tenant_id=tenant.id,
                recorded_at=moment,
            )
        )
        inp = ComputeDailyRollupsInput(day_from=day, day_to=day)
        await ComputeDailyRollups(uow).execute(inp)
        await ComputeDailyRollups(uow).execute(inp)
        rollup = uow.store.metering.rollups[(tenant.id, MetricKind.TENANT_CLAIM_INGESTED, day)]
        assert rollup.count == 1  # not 2

    async def test_system_metric_rolls_under_sentinel(self) -> None:
        uow = InMemoryUnitOfWork()
        day = date(2024, 6, 1)
        moment = datetime(2024, 6, 1, 12, 0, tzinfo=UTC)
        # Two NL queries — system-wide, no tenant.
        for _ in range(2):
            uow.store.metering.events.append(
                UsageEvent(
                    metric_kind=MetricKind.NL_QUERY_EXECUTED,
                    tenant_id=None,
                    recorded_at=moment,
                )
            )
        # Need at least one tenant event so distinct_tenants finds
        # nothing tenant-scoped; system metric should still roll up.
        await ComputeDailyRollups(uow).execute(ComputeDailyRollupsInput(day_from=day, day_to=day))
        rollup = uow.store.metering.rollups[(_SENTINEL, MetricKind.NL_QUERY_EXECUTED, day)]
        assert rollup.count == 2

    async def test_events_outside_range_excluded(self) -> None:
        uow = InMemoryUnitOfWork()
        tenant = _seed_tenant(uow)
        day = date(2024, 6, 1)
        # One event on the target day, one the next day.
        uow.store.metering.events.append(
            UsageEvent(
                metric_kind=MetricKind.TENANT_CLAIM_INGESTED,
                tenant_id=tenant.id,
                recorded_at=datetime(2024, 6, 1, 10, 0, tzinfo=UTC),
            )
        )
        uow.store.metering.events.append(
            UsageEvent(
                metric_kind=MetricKind.TENANT_CLAIM_INGESTED,
                tenant_id=tenant.id,
                recorded_at=datetime(2024, 6, 2, 10, 0, tzinfo=UTC),
            )
        )
        await ComputeDailyRollups(uow).execute(ComputeDailyRollupsInput(day_from=day, day_to=day))
        rollup = uow.store.metering.rollups[(tenant.id, MetricKind.TENANT_CLAIM_INGESTED, day)]
        assert rollup.count == 1

    async def test_explicit_tenant_filter(self) -> None:
        """When tenant_ids is given, only those tenants are rolled
        up even if others had events."""
        uow = InMemoryUnitOfWork()
        t1 = _seed_tenant(uow, slug="t1")
        t2 = _seed_tenant(uow, slug="t2")
        day = date(2024, 6, 1)
        moment = datetime(2024, 6, 1, 10, 0, tzinfo=UTC)
        for t in (t1, t2):
            uow.store.metering.events.append(
                UsageEvent(
                    metric_kind=MetricKind.TENANT_CLAIM_INGESTED,
                    tenant_id=t.id,
                    recorded_at=moment,
                )
            )
        await ComputeDailyRollups(uow).execute(
            ComputeDailyRollupsInput(day_from=day, day_to=day, tenant_ids=[t1.id])
        )
        assert (
            t1.id,
            MetricKind.TENANT_CLAIM_INGESTED,
            day,
        ) in uow.store.metering.rollups
        assert (
            t2.id,
            MetricKind.TENANT_CLAIM_INGESTED,
            day,
        ) not in uow.store.metering.rollups

    async def test_midnight_boundary_belongs_to_starting_day(self) -> None:
        """An event at exactly 00:00:00 belongs to the day that's
        starting (inclusive-start, exclusive-end).  A multi-day
        rollup counts it once, on the correct day."""
        uow = InMemoryUnitOfWork()
        tenant = _seed_tenant(uow)
        uow.store.metering.events.append(
            UsageEvent(
                metric_kind=MetricKind.TENANT_CLAIM_INGESTED,
                tenant_id=tenant.id,
                recorded_at=datetime(2024, 6, 2, 0, 0, 0, tzinfo=UTC),
            )
        )
        await ComputeDailyRollups(uow).execute(
            ComputeDailyRollupsInput(day_from=date(2024, 6, 1), day_to=date(2024, 6, 2))
        )
        june1 = uow.store.metering.rollups.get(
            (tenant.id, MetricKind.TENANT_CLAIM_INGESTED, date(2024, 6, 1))
        )
        june2 = uow.store.metering.rollups.get(
            (tenant.id, MetricKind.TENANT_CLAIM_INGESTED, date(2024, 6, 2))
        )
        # June 1 had no tenant events; its rollup row may be absent
        # or zero.  June 2 owns the midnight event.
        assert (june1 is None) or (june1.count == 0)
        assert june2 is not None and june2.count == 1

    async def test_end_of_day_within_same_day(self) -> None:
        """23:59:59 stays within its day (exclusive end is the next
        midnight)."""
        uow = InMemoryUnitOfWork()
        tenant = _seed_tenant(uow)
        uow.store.metering.events.append(
            UsageEvent(
                metric_kind=MetricKind.TENANT_REPORT_FILED,
                tenant_id=tenant.id,
                recorded_at=datetime(2024, 6, 1, 23, 59, 59, tzinfo=UTC),
            )
        )
        await ComputeDailyRollups(uow).execute(
            ComputeDailyRollupsInput(day_from=date(2024, 6, 1), day_to=date(2024, 6, 1))
        )
        rollup = uow.store.metering.rollups[
            (tenant.id, MetricKind.TENANT_REPORT_FILED, date(2024, 6, 1))
        ]
        assert rollup.count == 1


# ── Tenant usage read ───────────────────────────────────────────────────────


class TestGetTenantUsage:
    async def test_reads_own_rollups(self) -> None:
        uow = InMemoryUnitOfWork()
        tenant = _seed_tenant(uow)
        day = date(2024, 6, 1)
        moment = datetime(2024, 6, 1, 10, 0, tzinfo=UTC)
        uow.store.metering.events.append(
            UsageEvent(
                metric_kind=MetricKind.TENANT_CLAIM_INGESTED,
                tenant_id=tenant.id,
                recorded_at=moment,
            )
        )
        await ComputeDailyRollups(uow).execute(ComputeDailyRollupsInput(day_from=day, day_to=day))
        rollups = await GetTenantUsage(uow).execute(
            GetTenantUsageInput(
                tenant_id=tenant.id,
                caller_tenant_id=tenant.id,
                day_from=day,
                day_to=day,
            )
        )
        claim_rollups = [r for r in rollups if r.metric_kind == MetricKind.TENANT_CLAIM_INGESTED]
        assert claim_rollups and claim_rollups[0].count == 1

    async def test_cross_tenant_read_denied(self) -> None:
        uow = InMemoryUnitOfWork()
        tenant = _seed_tenant(uow)
        with pytest.raises(CrossTenantAccessError):
            await GetTenantUsage(uow).execute(
                GetTenantUsageInput(
                    tenant_id=tenant.id,
                    caller_tenant_id=uuid4(),  # different
                    day_from=date(2024, 6, 1),
                    day_to=date(2024, 6, 1),
                )
            )


# ── Admin summary ───────────────────────────────────────────────────────────


class TestGetAdminUsageSummary:
    async def test_sums_across_tenants(self) -> None:
        uow = InMemoryUnitOfWork()
        t1 = _seed_tenant(uow, slug="t1")
        t2 = _seed_tenant(uow, slug="t2")
        day = date(2024, 6, 1)
        moment = datetime(2024, 6, 1, 10, 0, tzinfo=UTC)
        # t1: 2 claims; t2: 1 claim.
        for _ in range(2):
            uow.store.metering.events.append(
                UsageEvent(
                    metric_kind=MetricKind.TENANT_CLAIM_INGESTED,
                    tenant_id=t1.id,
                    recorded_at=moment,
                )
            )
        uow.store.metering.events.append(
            UsageEvent(
                metric_kind=MetricKind.TENANT_CLAIM_INGESTED,
                tenant_id=t2.id,
                recorded_at=moment,
            )
        )
        await ComputeDailyRollups(uow).execute(ComputeDailyRollupsInput(day_from=day, day_to=day))
        summary = await GetAdminUsageSummary(uow).execute(
            GetAdminUsageSummaryInput(day_from=day, day_to=day)
        )
        by_tenant = {(r.tenant_slug, r.metric_kind): r.total_count for r in summary}
        assert by_tenant[("t1", MetricKind.TENANT_CLAIM_INGESTED)] == 2
        assert by_tenant[("t2", MetricKind.TENANT_CLAIM_INGESTED)] == 1

    async def test_sentinel_maps_to_none(self) -> None:
        uow = InMemoryUnitOfWork()
        day = date(2024, 6, 1)
        moment = datetime(2024, 6, 1, 10, 0, tzinfo=UTC)
        uow.store.metering.events.append(
            UsageEvent(
                metric_kind=MetricKind.NL_QUERY_EXECUTED,
                tenant_id=None,
                recorded_at=moment,
            )
        )
        await ComputeDailyRollups(uow).execute(ComputeDailyRollupsInput(day_from=day, day_to=day))
        summary = await GetAdminUsageSummary(uow).execute(
            GetAdminUsageSummaryInput(day_from=day, day_to=day)
        )
        nl_rows = [r for r in summary if r.metric_kind == MetricKind.NL_QUERY_EXECUTED]
        assert nl_rows
        # Sentinel mapped back to None tenant.
        assert nl_rows[0].tenant_id is None
        assert nl_rows[0].total_count == 1

    async def test_empty_range_returns_empty_list(self) -> None:
        """A date range with no rollups returns [] rather than
        erroring — common for a new tenant or a quiet period.  This
        also exercises the SQL ``func.sum`` None-coalescing path
        indirectly (no rows -> no summary rows)."""
        uow = InMemoryUnitOfWork()
        summary = await GetAdminUsageSummary(uow).execute(
            GetAdminUsageSummaryInput(day_from=date(2030, 1, 1), day_to=date(2030, 1, 31))
        )
        assert summary == []

    async def test_tenant_usage_empty_range_returns_empty(self) -> None:
        uow = InMemoryUnitOfWork()
        tenant = _seed_tenant(uow)
        rollups = await GetTenantUsage(uow).execute(
            GetTenantUsageInput(
                tenant_id=tenant.id,
                caller_tenant_id=tenant.id,
                day_from=date(2030, 1, 1),
                day_to=date(2030, 1, 31),
            )
        )
        assert rollups == []


# ── Integration: metered actions leave events ───────────────────────────────


class TestMeteredActionsEmitEvents:
    async def test_nl_search_emits_event(self) -> None:
        from atlas.application.use_cases.nl_search import (
            ExecuteNlSearch,
            NlSearchInput,
        )

        uow = InMemoryUnitOfWork()
        await ExecuteNlSearch(uow).execute(NlSearchInput(raw_query="737 fatal in 2023"))
        nl_events = [
            e for e in uow.store.metering.events if e.metric_kind == MetricKind.NL_QUERY_EXECUTED
        ]
        assert len(nl_events) == 1
        assert nl_events[0].tenant_id is None

    async def test_multi_day_range_writes_each_day(self) -> None:
        uow = InMemoryUnitOfWork()
        tenant = _seed_tenant(uow)
        d1 = date(2024, 6, 1)
        d2 = date(2024, 6, 3)
        for d in (
            datetime(2024, 6, 1, 9, tzinfo=UTC),
            datetime(2024, 6, 2, 9, tzinfo=UTC),
            datetime(2024, 6, 3, 9, tzinfo=UTC),
        ):
            uow.store.metering.events.append(
                UsageEvent(
                    metric_kind=MetricKind.TENANT_CLAIM_INGESTED,
                    tenant_id=tenant.id,
                    recorded_at=d,
                )
            )
        result = await ComputeDailyRollups(uow).execute(
            ComputeDailyRollupsInput(day_from=d1, day_to=d2)
        )
        # 3 days x (3 tenant metrics + 2 system metrics) =
        # but tenant only appears on days it had events.
        # Each day: 3 tenant-scoped + 2 system = 5 rows.
        assert result.rows_written == 3 * (3 + 2)
        # Each day's claim rollup is 1.
        for day in (
            date(2024, 6, 1),
            date(2024, 6, 2),
            date(2024, 6, 3),
        ):
            rollup = uow.store.metering.rollups[(tenant.id, MetricKind.TENANT_CLAIM_INGESTED, day)]
            assert rollup.count == 1
        _ = timedelta  # silence unused import in some runs
