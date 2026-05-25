"""SQLAlchemy repositories for the projections aggregate.

Carved out of the old ``repositories.py`` monolith in r9; behaviour
unchanged.  Public ``Sql*`` classes are re-exported from
``atlas.infrastructure.db.repositories`` so existing imports keep
working.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID

from sqlalchemy import func, literal, select, text, tuple_
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import DataError, StatementError
from sqlalchemy.ext.asyncio import AsyncSession

from atlas.domain.entities import (
    AccidentProjectionHistory,
    ProjectedAccidentRecord,
)
from atlas.domain.interfaces.repositories import (
    ProjectionHistoryRepository,
    ProjectionRepository,
)
from atlas.infrastructure.db.orm_models import (
    AccidentProjectionHistoryModel,
    ProjectedAccidentRecordModel,
)
from atlas.infrastructure.db.repositories._helpers import (
    _domain_data,
    _to_domain,
    _to_domain_opt,
    logger,
)


class SqlProjectionRepository(ProjectionRepository):
    def __init__(self, session: AsyncSession):
        self._session = session

    async def get(self, event_id: UUID) -> ProjectedAccidentRecord | None:
        obj = await self._session.get(ProjectedAccidentRecordModel, event_id)
        return _to_domain_opt(obj, ProjectedAccidentRecord)

    async def upsert(self, projection: ProjectedAccidentRecord) -> None:
        data = _domain_data(projection)
        stmt = insert(ProjectedAccidentRecordModel).values(**data)
        update_data = {k: v for k, v in data.items() if k != "event_id"}
        stmt = stmt.on_conflict_do_update(
            index_elements=["event_id"],
            set_=update_data,
        )
        await self._session.execute(stmt)

    async def delete(self, event_id: UUID) -> None:
        obj = await self._session.get(ProjectedAccidentRecordModel, event_id)
        if obj is not None:
            await self._session.delete(obj)

    async def find_candidates_for_event_matching(
        self,
        event_date: str,
        limit: int = 50,
    ) -> list[ProjectedAccidentRecord]:
        """Return projections whose event_date is within ±1 day of the given date.

        We query using a Postgres date cast on the JSONB ``fields`` column.
        The partial index on ``fields->>'event_date'`` (if any) will be used
        when the planner decides; for MVP this is a small enough table that
        a sequential scan is acceptable. We return at most ``limit`` rows.

        Excludes events that have been merged into another event so that
        matching never re-attaches new claims to an absorbed duplicate.
        """
        sql = text(
            """
            SELECT p.*
            FROM projected_accident_records p
            JOIN accident_events e ON e.id = p.event_id
            WHERE e.merged_into_event_id IS NULL
              AND CASE
                    WHEN (p.fields->>'event_date') ~ '^\\d{4}-\\d{2}-\\d{2}$'
                    THEN (p.fields->>'event_date')::date
                    ELSE NULL
                  END BETWEEN (:event_date::date - INTERVAL '1 day')
                          AND (:event_date::date + INTERVAL '1 day')
            ORDER BY p.updated_at DESC
            LIMIT :limit
            """
        )
        try:
            result = await self._session.execute(sql, {"event_date": event_date, "limit": limit})
            rows = result.fetchall()
            return [
                _to_domain(ProjectedAccidentRecordModel(**row._mapping), ProjectedAccidentRecord)
                for row in rows
            ]
        except (DataError, StatementError, ValueError) as exc:
            # If the caller supplied a malformed event_date, return no projection
            # candidates.  Unexpected DB/connection failures must still bubble up
            # so ingestion does not silently create duplicate events while the
            # matching query is broken.
            logger.warning(
                "Skipping projection candidate lookup for invalid event_date",
                extra={"event_date": event_date, "error": str(exc)},
            )
            return []

    async def count_total(self) -> int:
        result = await self._session.execute(
            select(func.count()).select_from(ProjectedAccidentRecordModel)
        )
        return int(result.scalar_one())

    async def iter_all_claims(self) -> AsyncIterator[tuple[UUID, dict[str, Any]]]:
        """Stream (event_id, fields) for every projected record.

        Uses ``yield_per`` so the driver fetches rows in batches rather than
        materialising the full table into the ORM identity map.  Ordered by
        event_id for deterministic corpus construction.
        """
        stmt = (
            select(
                ProjectedAccidentRecordModel.event_id,
                ProjectedAccidentRecordModel.fields,
            )
            .order_by(ProjectedAccidentRecordModel.event_id)
            .execution_options(yield_per=500)
        )
        result = await self._session.stream(stmt)
        async for row in result:
            yield row.event_id, row.fields


class SqlProjectionHistoryRepository(ProjectionHistoryRepository):
    def __init__(self, session: AsyncSession):
        self._session = session

    async def add(self, history: AccidentProjectionHistory) -> None:
        self._session.add(AccidentProjectionHistoryModel(**_domain_data(history)))

    async def find_by_event(
        self,
        event_id: UUID,
        *,
        limit: int | None = None,
        after_id: UUID | None = None,
    ) -> list[AccidentProjectionHistory]:
        stmt = (
            select(AccidentProjectionHistoryModel)
            .where(AccidentProjectionHistoryModel.accident_event_id == event_id)
            .order_by(
                AccidentProjectionHistoryModel.projection_version,
                AccidentProjectionHistoryModel.id,
            )
        )
        if after_id is not None:
            cursor_version = await self._session.scalar(
                select(AccidentProjectionHistoryModel.projection_version).where(
                    AccidentProjectionHistoryModel.id == after_id
                )
            )
            if cursor_version is not None:
                stmt = stmt.where(
                    tuple_(
                        AccidentProjectionHistoryModel.projection_version,
                        AccidentProjectionHistoryModel.id,
                    )
                    > tuple_(literal(cursor_version), literal(after_id))
                )
        if limit is not None:
            stmt = stmt.limit(limit)
        result = await self._session.execute(stmt)
        return [_to_domain(obj, AccidentProjectionHistory) for obj in result.scalars()]

    async def find_by_outbox_event(self, outbox_event_id: UUID) -> AccidentProjectionHistory | None:
        result = await self._session.execute(
            select(AccidentProjectionHistoryModel).where(
                AccidentProjectionHistoryModel.caused_by_outbox_event_id == outbox_event_id
            )
        )
        obj = result.scalar_one_or_none()
        return _to_domain_opt(obj, AccidentProjectionHistory)
