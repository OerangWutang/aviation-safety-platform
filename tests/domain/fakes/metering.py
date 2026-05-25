"""Fake usage-event and daily-rollup repositories."""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime
from uuid import UUID

from atlas.domain.interfaces.repositories import (
    UsageDailyRollupRepository,
    UsageEventRepository,
)
from atlas.domain.metering import NO_TENANT_SENTINEL
from atlas.domain.metering.entities import (
    MetricKind,
    UsageDailyRollup,
    UsageEvent,
    UsageSummaryRow,
)
from tests.domain.fakes._store import (
    _MeteringStore,
    _Store,
)


class FakeUsageEventRepository(UsageEventRepository):
    def __init__(self, s: _MeteringStore) -> None:
        self._s = s

    async def add(self, event: UsageEvent) -> None:
        self._s.events.append(event.model_copy(deep=True))

    async def add_many(self, events: list[UsageEvent]) -> None:
        for event in events:
            self._s.events.append(event.model_copy(deep=True))

    async def count_in_range(
        self,
        *,
        tenant_id: UUID | None,
        metric_kind: MetricKind,
        start: datetime,
        end: datetime,
    ) -> int:
        # Inclusive start, exclusive end — same as SQL repo.
        return sum(
            1
            for e in self._s.events
            if e.metric_kind == metric_kind
            and e.tenant_id == tenant_id
            and start <= e.recorded_at < end
        )

    async def distinct_tenants_in_range(self, *, start: datetime, end: datetime) -> list[UUID]:
        seen: list[UUID] = []
        for e in self._s.events:
            if e.tenant_id is not None and start <= e.recorded_at < end and e.tenant_id not in seen:
                seen.append(e.tenant_id)
        return seen


class FakeUsageDailyRollupRepository(UsageDailyRollupRepository):
    def __init__(self, s: _MeteringStore, store_ref: _Store) -> None:
        self._s = s
        self._store = store_ref

    async def upsert(self, rollup: UsageDailyRollup) -> None:
        key = (rollup.tenant_id, rollup.metric_kind, rollup.day)
        self._s.rollups[key] = rollup.model_copy(deep=True)

    async def list_for_tenant(
        self,
        *,
        tenant_id: UUID,
        day_from: date,
        day_to: date,
    ) -> list[UsageDailyRollup]:
        return sorted(
            (
                r.model_copy(deep=True)
                for r in self._s.rollups.values()
                if r.tenant_id == tenant_id and day_from <= r.day <= day_to
            ),
            key=lambda r: (r.day, r.metric_kind),
        )

    async def summary_across_tenants(
        self,
        *,
        day_from: date,
        day_to: date,
    ) -> list[UsageSummaryRow]:

        sentinel = UUID(NO_TENANT_SENTINEL)
        # Sum by (tenant_id, metric_kind).
        sums: dict[tuple[UUID, MetricKind], int] = defaultdict(int)
        for r in self._s.rollups.values():
            if day_from <= r.day <= day_to:
                sums[(r.tenant_id, r.metric_kind)] += r.count

        rows: list[UsageSummaryRow] = []
        for (tenant_id, metric_kind), total in sums.items():
            # Look up tenant slug; sentinel-rows have no tenant.
            mapped_id = None if tenant_id == sentinel else tenant_id
            slug: str | None = None
            if mapped_id is not None:
                tenant = self._store.tenancy.tenants.get(mapped_id)
                if tenant is not None:
                    slug = tenant.slug
            rows.append(
                UsageSummaryRow(
                    tenant_id=mapped_id,
                    tenant_slug=slug,
                    metric_kind=metric_kind,
                    total_count=total,
                )
            )
        # Stable ordering: slug-None first, then alphabetical, then
        # by metric.
        rows.sort(
            key=lambda r: (
                r.tenant_slug is not None,
                r.tenant_slug or "",
                r.metric_kind.value,
            )
        )
        return rows
