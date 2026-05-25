"""SQLAlchemy repositories for the reviews aggregate.

Carved out of the old ``repositories.py`` monolith in r9; behaviour
unchanged.  Public ``Sql*`` classes are re-exported from
``atlas.infrastructure.db.repositories`` so existing imports keep
working.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

if TYPE_CHECKING:
    from sqlalchemy.sql import ColumnElement

from atlas.domain.entities import (
    PendingDuplicateReview,
)
from atlas.domain.enums import (
    DuplicateReviewStatus,
)
from atlas.domain.interfaces.repositories import (
    PendingDuplicateReviewRepository,
)
from atlas.infrastructure.db.orm_models import (
    PendingDuplicateReviewModel,
)
from atlas.infrastructure.db.repositories._helpers import (
    _apply_created_at_uuid_cursor,
    _domain_data,
    _to_domain,
    _to_domain_opt,
)


class SqlPendingDuplicateReviewRepository(PendingDuplicateReviewRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, review: PendingDuplicateReview) -> PendingDuplicateReview | None:
        stmt = (
            insert(PendingDuplicateReviewModel)
            .values(**_domain_data(review))
            .on_conflict_do_nothing(
                index_elements=[
                    text("LEAST(event_id_a, event_id_b)"),
                    text("GREATEST(event_id_a, event_id_b)"),
                ],
                index_where=text("status = 'PENDING'"),
            )
            .returning(PendingDuplicateReviewModel)
        )
        result = await self._session.execute(stmt)
        obj = result.scalar_one_or_none()
        if obj is not None:
            return _to_domain(obj, PendingDuplicateReview)
        return await self.find_pending_pair(review.event_id_a, review.event_id_b)

    async def get(self, id: UUID) -> PendingDuplicateReview | None:
        obj = await self._session.get(PendingDuplicateReviewModel, id)
        return _to_domain_opt(obj, PendingDuplicateReview)

    async def find_pending_for_event(self, event_id: UUID) -> list[PendingDuplicateReview]:
        result = await self._session.execute(
            select(PendingDuplicateReviewModel).where(
                (
                    (PendingDuplicateReviewModel.event_id_a == event_id)
                    | (PendingDuplicateReviewModel.event_id_b == event_id)
                ),
                PendingDuplicateReviewModel.status == DuplicateReviewStatus.PENDING.value,
            )
        )
        return [_to_domain(obj, PendingDuplicateReview) for obj in result.scalars()]

    async def list_pending(
        self, *, limit: int, after_id: UUID | None = None
    ) -> list[PendingDuplicateReview]:
        """Return PENDING reviews ordered by stable newest-first keyset."""
        stmt = (
            select(PendingDuplicateReviewModel)
            .where(PendingDuplicateReviewModel.status == DuplicateReviewStatus.PENDING.value)
            .order_by(
                PendingDuplicateReviewModel.created_at.desc(),
                PendingDuplicateReviewModel.id.desc(),
            )
            .limit(limit)
        )
        stmt = await _apply_created_at_uuid_cursor(
            self._session,
            stmt,
            PendingDuplicateReviewModel,
            after_id,
            descending=True,
        )
        result = await self._session.execute(stmt)
        return [_to_domain(obj, PendingDuplicateReview) for obj in result.scalars()]

    def _pair_filter(self, event_id_a: UUID, event_id_b: UUID) -> ColumnElement[bool]:
        return (
            (PendingDuplicateReviewModel.event_id_a == event_id_a)
            & (PendingDuplicateReviewModel.event_id_b == event_id_b)
        ) | (
            (PendingDuplicateReviewModel.event_id_a == event_id_b)
            & (PendingDuplicateReviewModel.event_id_b == event_id_a)
        )

    async def find_pending_pair(
        self, event_id_a: UUID, event_id_b: UUID
    ) -> PendingDuplicateReview | None:
        """Return the active PENDING review for this unordered pair, if any."""
        result = await self._session.execute(
            select(PendingDuplicateReviewModel)
            .where(
                self._pair_filter(event_id_a, event_id_b),
                PendingDuplicateReviewModel.status == DuplicateReviewStatus.PENDING.value,
            )
            .order_by(
                PendingDuplicateReviewModel.created_at.desc(),
                PendingDuplicateReviewModel.id.desc(),
            )
            .limit(1)
        )
        obj = result.scalar_one_or_none()
        return _to_domain_opt(obj, PendingDuplicateReview)

    async def find_existing_pair(
        self, event_id_a: UUID, event_id_b: UUID
    ) -> PendingDuplicateReview | None:
        """Return a deterministic review for this pair (PENDING first, then newest)."""
        result = await self._session.execute(
            select(PendingDuplicateReviewModel)
            .where(self._pair_filter(event_id_a, event_id_b))
            .order_by(
                (PendingDuplicateReviewModel.status == DuplicateReviewStatus.PENDING.value).desc(),
                PendingDuplicateReviewModel.created_at.desc(),
                PendingDuplicateReviewModel.id.desc(),
            )
            .limit(1)
        )
        obj = result.scalar_one_or_none()
        return _to_domain_opt(obj, PendingDuplicateReview)

    async def update_status(
        self,
        id: UUID,
        status: DuplicateReviewStatus,
        resolved_by: UUID | None = None,
        resolution_note: str | None = None,
    ) -> PendingDuplicateReview | None:
        now = datetime.now(UTC)
        values: dict[str, object] = {
            "status": status.value,
            "resolved_at": now if status != DuplicateReviewStatus.PENDING else None,
            "resolved_by": resolved_by,
            "resolution_note": resolution_note,
        }
        stmt = (
            update(PendingDuplicateReviewModel)
            .where(PendingDuplicateReviewModel.id == id)
            .values(**values)
            .returning(PendingDuplicateReviewModel)
        )
        result = await self._session.execute(stmt)
        obj = result.scalar_one_or_none()
        return _to_domain_opt(obj, PendingDuplicateReview)
