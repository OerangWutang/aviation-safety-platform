"""SQLAlchemy repositories for the events aggregate.

Carved out of the old ``repositories.py`` monolith in r9; behaviour
unchanged.  Public ``Sql*`` classes are re-exported from
``atlas.infrastructure.db.repositories`` so existing imports keep
working.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from atlas.domain.entities import (
    AccidentEvent,
)
from atlas.domain.interfaces.repositories import (
    AccidentEventRepository,
)
from atlas.infrastructure.db.orm_models import (
    AccidentEventModel,
)
from atlas.infrastructure.db.repositories._helpers import (
    ADVISORY_LOCK_REPROJECTION,
    _domain_data,
    _to_domain_opt,
)


class SqlAccidentEventRepository(AccidentEventRepository):
    def __init__(self, session: AsyncSession):
        self._session = session

    async def get(self, id: UUID) -> AccidentEvent | None:
        obj = await self._session.get(AccidentEventModel, id)
        return _to_domain_opt(obj, AccidentEvent)

    async def lock_for_update(self, id: UUID) -> AccidentEvent | None:
        """Lock and return one accident event row with ``SELECT ... FOR UPDATE``.

        ``populate_existing=True`` matters because callers often performed a
        lightweight ``get`` before taking the write lock.  Without forcing a
        refresh, SQLAlchemy could hand back the stale identity-map instance and
        the caller would miss a concurrent merge that committed while it was
        waiting for the row lock.
        """
        result = await self._session.execute(
            select(AccidentEventModel)
            .where(AccidentEventModel.id == id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        obj = result.scalar_one_or_none()
        return _to_domain_opt(obj, AccidentEvent)

    async def add(self, event: AccidentEvent) -> None:
        self._session.add(AccidentEventModel(**_domain_data(event)))

    async def save(self, event: AccidentEvent) -> None:
        await self._session.merge(AccidentEventModel(**_domain_data(event)))

    async def update(self, event: AccidentEvent) -> None:
        """Alias for ``save``; kept for interface compatibility."""
        await self.save(event)

    async def list_all_ids(self) -> list[UUID]:
        result = await self._session.execute(select(AccidentEventModel.id))
        return [row[0] for row in result.all()]

    async def list_ids_page(self, limit: int, offset: int = 0) -> list[UUID]:
        result = await self._session.execute(
            select(AccidentEventModel.id)
            .order_by(AccidentEventModel.id)
            .offset(offset)
            .limit(limit)
        )
        return [row[0] for row in result.all()]

    async def list_ids_after_keyset(self, after_id: UUID | None, limit: int) -> list[UUID]:
        stmt = select(AccidentEventModel.id).order_by(AccidentEventModel.id).limit(limit)
        if after_id is not None:
            stmt = stmt.where(AccidentEventModel.id > after_id)
        result = await self._session.execute(stmt)
        return [row[0] for row in result.all()]

    async def lock_for_reprojection(self, event_id: UUID) -> None:
        """Acquire a transaction-scoped advisory lock keyed on the event id.

        ``pg_advisory_xact_lock(key)`` is held until the surrounding transaction
        ends, regardless of which rows were touched. Two reprojection workers
        called for the same ``event_id`` will serialize here; workers on
        different events do not contend.

        This uses the two-int advisory-lock form with a dedicated reprojection
        namespace and ``hashtext(event_id)``. Hash collisions can only serialize
        unrelated reprojections; they can no longer collide with source-record
        correction or identity-resolution locks.
        """
        await self._session.execute(
            text("SELECT pg_advisory_xact_lock(CAST(:namespace AS integer), hashtext(:k))"),
            {"namespace": ADVISORY_LOCK_REPROJECTION, "k": str(event_id)},
        )

    async def try_atomic_merge(self, source_event_id: UUID, target_event_id: UUID) -> bool:
        """Atomically claim the merge by setting merged_into_event_id.

        Uses a conditional UPDATE with RETURNING so that only one concurrent
        caller can win the race for the same source event.  The caller that
        finds ``merged_into_event_id IS NOT NULL`` (zero rows returned) gets
        ``False`` and must raise ``EventAlreadyMergedError``.

        This is the only correct way to close the TOCTOU gap between the
        ``is_merged`` guard-read and the ``merged_into_event_id`` write.
        """
        result = await self._session.execute(
            text(
                "UPDATE accident_events "
                "SET merged_into_event_id = :target "
                "WHERE id = :source AND merged_into_event_id IS NULL "
                "RETURNING id"
            ),
            {"source": source_event_id, "target": target_event_id},
        )
        return result.scalar_one_or_none() is not None

    async def find_existing_ids(self, ids: list[UUID]) -> set[UUID]:
        """Return the subset of ``ids`` that exist in the event table.

        Single ``WHERE id = ANY(:ids)`` round trip so cost is independent of
        batch size.  Overrides the base-class N-gets default.
        """
        if not ids:
            return set()
        from sqlalchemy import select

        from atlas.infrastructure.db.orm_models import AccidentEventModel

        result = await self._session.execute(
            select(AccidentEventModel.id).where(AccidentEventModel.id.in_(ids))
        )
        return set(result.scalars().all())
