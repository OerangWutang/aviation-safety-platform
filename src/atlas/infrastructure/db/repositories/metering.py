"""SQL repositories for the metering bounded context (Phase 8).

The events repo is append-only.  The rollup repo's ``upsert``
relies on the ``(tenant_id, metric_kind, day)`` unique constraint
via ``ON CONFLICT DO UPDATE`` — Postgres-specific but it's the
right primitive for idempotent rollup recomputation.
"""

from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

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
from atlas.infrastructure.db.orm_models import (
    TenantModel,
    UsageDailyRollupModel,
    UsageEventModel,
)
from atlas.infrastructure.db.repositories._helpers import _domain_data


class SqlUsageEventRepository(UsageEventRepository):
    def __init__(self, session: AsyncSession):
        self._session = session

    async def add(self, event: UsageEvent) -> None:
        data = _domain_data(event)
        # Convert StrEnum to string for the wire column.
        data["metric_kind"] = (
            event.metric_kind.value if hasattr(event.metric_kind, "value") else event.metric_kind
        )
        self._session.add(UsageEventModel(**data))
        await self._session.flush()

    async def add_many(self, events: list[UsageEvent]) -> None:
        if not events:
            return
        models = []
        for event in events:
            data = _domain_data(event)
            data["metric_kind"] = (
                event.metric_kind.value
                if hasattr(event.metric_kind, "value")
                else event.metric_kind
            )
            models.append(UsageEventModel(**data))
        # add_all queues every row; a single flush emits one
        # multi-row INSERT instead of N round trips.
        self._session.add_all(models)
        await self._session.flush()

    async def count_in_range(
        self,
        *,
        tenant_id: UUID | None,
        metric_kind: MetricKind,
        start: datetime,
        end: datetime,
    ) -> int:
        stmt = select(func.count(UsageEventModel.id)).where(
            UsageEventModel.metric_kind == metric_kind.value,
            UsageEventModel.recorded_at >= start,
            UsageEventModel.recorded_at < end,
        )
        if tenant_id is None:
            stmt = stmt.where(UsageEventModel.tenant_id.is_(None))
        else:
            stmt = stmt.where(UsageEventModel.tenant_id == tenant_id)
        result = await self._session.execute(stmt)
        # ``scalar_one`` returns the aggregate count.
        return int(result.scalar_one() or 0)

    async def distinct_tenants_in_range(self, *, start: datetime, end: datetime) -> list[UUID]:
        result = await self._session.execute(
            select(UsageEventModel.tenant_id)
            .where(
                UsageEventModel.tenant_id.is_not(None),
                UsageEventModel.recorded_at >= start,
                UsageEventModel.recorded_at < end,
            )
            .distinct()
        )
        return [row for (row,) in result.all() if row is not None]


class SqlUsageDailyRollupRepository(UsageDailyRollupRepository):
    def __init__(self, session: AsyncSession):
        self._session = session

    async def upsert(self, rollup: UsageDailyRollup) -> None:
        # ON CONFLICT DO UPDATE on the natural key.  Replaces
        # ``count`` and ``computed_at`` atomically; idempotent on
        # repeat runs for the same day.
        stmt = insert(UsageDailyRollupModel).values(
            id=rollup.id,
            tenant_id=rollup.tenant_id,
            metric_kind=(
                rollup.metric_kind.value
                if hasattr(rollup.metric_kind, "value")
                else rollup.metric_kind
            ),
            day=rollup.day,
            count=rollup.count,
            computed_at=rollup.computed_at,
        )
        stmt = stmt.on_conflict_do_update(
            constraint="uq_usage_daily_rollups_natural",
            set_={
                "count": stmt.excluded.count,
                "computed_at": stmt.excluded.computed_at,
            },
        )
        await self._session.execute(stmt)

    async def list_for_tenant(
        self,
        *,
        tenant_id: UUID,
        day_from: date,
        day_to: date,
    ) -> list[UsageDailyRollup]:
        result = await self._session.execute(
            select(UsageDailyRollupModel)
            .where(
                UsageDailyRollupModel.tenant_id == tenant_id,
                UsageDailyRollupModel.day >= day_from,
                UsageDailyRollupModel.day <= day_to,
            )
            .order_by(
                UsageDailyRollupModel.day,
                UsageDailyRollupModel.metric_kind,
            )
        )
        return [
            UsageDailyRollup(
                id=row.id,
                tenant_id=row.tenant_id,
                metric_kind=MetricKind(row.metric_kind),
                day=row.day,
                count=row.count,
                computed_at=row.computed_at,
            )
            for row in result.scalars()
        ]

    async def summary_across_tenants(
        self,
        *,
        day_from: date,
        day_to: date,
    ) -> list[UsageSummaryRow]:
        # SUM(count) GROUP BY (tenant_id, metric_kind), left-joined
        # against tenants for the slug.  Sentinel UUID maps back to
        # NULL on the way out.
        sentinel = UUID(NO_TENANT_SENTINEL)
        stmt = (
            select(
                UsageDailyRollupModel.tenant_id,
                TenantModel.slug,
                UsageDailyRollupModel.metric_kind,
                func.sum(UsageDailyRollupModel.count).label("total"),
            )
            .select_from(
                UsageDailyRollupModel.__table__.join(
                    TenantModel.__table__,
                    UsageDailyRollupModel.tenant_id == TenantModel.id,
                    isouter=True,
                )
            )
            .where(
                UsageDailyRollupModel.day >= day_from,
                UsageDailyRollupModel.day <= day_to,
            )
            .group_by(
                UsageDailyRollupModel.tenant_id,
                TenantModel.slug,
                UsageDailyRollupModel.metric_kind,
            )
            .order_by(
                TenantModel.slug.nulls_first(),
                UsageDailyRollupModel.metric_kind,
            )
        )
        result = await self._session.execute(stmt)
        rows: list[UsageSummaryRow] = []
        for tenant_id, slug, metric_kind, total in result.all():
            mapped_tenant_id = None if tenant_id == sentinel else tenant_id
            rows.append(
                UsageSummaryRow(
                    tenant_id=mapped_tenant_id,
                    tenant_slug=slug,
                    metric_kind=MetricKind(metric_kind),
                    total_count=int(total or 0),
                )
            )
        return rows
