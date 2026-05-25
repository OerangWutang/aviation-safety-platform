"""SQLAlchemy repositories for the conflicts aggregate.

Carved out of the old ``repositories.py`` monolith in r9; behaviour
unchanged.  Public ``Sql*`` classes are re-exported from
``atlas.infrastructure.db.repositories`` so existing imports keep
working.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from atlas.domain.entities import (
    ClaimConflict,
    ConflictActivityLogEntry,
)
from atlas.domain.enums import (
    ConflictStatus,
)
from atlas.domain.interfaces.repositories import (
    ConflictActivityLogRepository,
    ConflictRepository,
)
from atlas.infrastructure.db.orm_models import (
    ClaimConflictClaimModel,
    ClaimConflictModel,
    ConflictActivityLogModel,
)
from atlas.infrastructure.db.repositories._helpers import (
    _apply_created_at_uuid_cursor,
    _chunked,
    _domain_data,
    _to_domain,
)


class SqlConflictRepository(ConflictRepository):
    def __init__(self, session: AsyncSession):
        self._session = session

    async def get(self, id: UUID) -> ClaimConflict | None:
        result = await self._session.execute(
            select(ClaimConflictModel).where(ClaimConflictModel.id == id)
        )
        obj = result.scalar_one_or_none()
        if not obj:
            return None
        conflict = _to_domain(obj, ClaimConflict)
        links_result = await self._session.execute(
            select(ClaimConflictClaimModel.claim_id).where(
                ClaimConflictClaimModel.conflict_id == id
            )
        )
        conflict.claim_ids = [row[0] for row in links_result.all()]
        return conflict

    async def add(self, conflict: ClaimConflict) -> None:
        self._session.add(ClaimConflictModel(**_domain_data(conflict, exclude={"claim_ids"})))

    async def try_add_open(self, conflict: ClaimConflict) -> bool:
        """Insert a new OPEN conflict row.

        Uses ``ON CONFLICT DO NOTHING`` against the partial unique index
        ``uq_open_conflict_event_field`` (migration 008) so that concurrent
        ingestion workers racing to create the same conflict produce at most one
        row. Returns True if the row was inserted, False if a concurrent writer
        already created an OPEN conflict for the same (event_id, field_name).

        The conflict target is expressed as ``index_elements`` + ``index_where``
        rather than ``constraint=``: PostgreSQL's ``ON CONFLICT`` only accepts
        named unique constraints via ``constraint``, but partial unique indexes
        are not unique constraints. Targeting them by name silently fails to
        match the inference-based path that PostgreSQL actually uses, which is
        why the previous form was unsafe.
        """
        data = _domain_data(conflict, exclude={"claim_ids"})
        stmt = (
            insert(ClaimConflictModel)
            .values(**data)
            .on_conflict_do_nothing(
                index_elements=[
                    ClaimConflictModel.event_id,
                    ClaimConflictModel.field_name,
                ],
                index_where=ClaimConflictModel.status == "OPEN",
            )
            .returning(ClaimConflictModel.id)
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none() is not None

    async def save(self, conflict: ClaimConflict) -> None:
        await self._session.merge(
            ClaimConflictModel(**_domain_data(conflict, exclude={"claim_ids"}))
        )

    async def find_by_event(
        self,
        event_id: UUID,
        limit: int | None = None,
        offset: int = 0,
        after_id: UUID | None = None,
    ) -> list[ClaimConflict]:
        stmt = (
            select(ClaimConflictModel)
            .where(ClaimConflictModel.event_id == event_id)
            .order_by(ClaimConflictModel.created_at.desc(), ClaimConflictModel.id.desc())
        )
        if after_id is not None:
            stmt = await _apply_created_at_uuid_cursor(
                self._session, stmt, ClaimConflictModel, after_id, descending=True
            )
        else:
            stmt = stmt.offset(offset)
        if limit is not None:
            stmt = stmt.limit(limit)
        result = await self._session.execute(stmt)
        conflicts = [_to_domain(obj, ClaimConflict) for obj in result.scalars()]
        if not conflicts:
            return []

        conflict_ids = [conflict.id for conflict in conflicts]
        claim_ids_by_conflict: dict[UUID, list[UUID]] = defaultdict(list)
        for chunk in _chunked(conflict_ids):
            links_result = await self._session.execute(
                select(ClaimConflictClaimModel.conflict_id, ClaimConflictClaimModel.claim_id).where(
                    ClaimConflictClaimModel.conflict_id.in_(chunk)
                )
            )
            for row in links_result.all():
                claim_ids_by_conflict[row[0]].append(row[1])

        for conflict in conflicts:
            conflict.claim_ids = claim_ids_by_conflict.get(conflict.id, [])
        return conflicts

    async def close_event_conflicts_as_merged(
        self,
        event_id: UUID,
        *,
        note: str,
    ) -> list[ClaimConflict]:
        stmt = (
            update(ClaimConflictModel)
            .where(
                ClaimConflictModel.event_id == event_id,
                ClaimConflictModel.status == "OPEN",
            )
            .values(
                status="RESOLVED",
                winning_claim_id=None,
                resolved_at=func.now(),
                last_modified_reason="SYSTEM_AUTO_CLOSED",
                last_modified_note=note[:255],
                updated_at=func.now(),
                version=ClaimConflictModel.version + 1,
            )
            .returning(ClaimConflictModel)
        )
        result = await self._session.execute(stmt)
        updated = [_to_domain(obj, ClaimConflict) for obj in result.scalars().all()]
        for conflict in updated:
            conflict.claim_ids = await self.get_claim_ids_for_conflict(conflict.id)
        return updated

    async def find_by_event_field(self, event_id: UUID, field_name: str) -> ClaimConflict | None:
        result = await self._session.execute(
            select(ClaimConflictModel)
            .where(
                ClaimConflictModel.event_id == event_id, ClaimConflictModel.field_name == field_name
            )
            .order_by(ClaimConflictModel.created_at.desc())
        )
        obj = result.scalars().first()
        if not obj:
            return None
        conflict = _to_domain(obj, ClaimConflict)
        conflict.claim_ids = await self.get_claim_ids_for_conflict(conflict.id)
        return conflict

    async def find_open_by_event_field(
        self, event_id: UUID, field_name: str
    ) -> ClaimConflict | None:
        result = await self._session.execute(
            select(ClaimConflictModel).where(
                ClaimConflictModel.event_id == event_id,
                ClaimConflictModel.field_name == field_name,
                ClaimConflictModel.status == "OPEN",
            )
        )
        obj = result.scalar_one_or_none()
        if not obj:
            return None
        conflict = _to_domain(obj, ClaimConflict)
        conflict.claim_ids = await self.get_claim_ids_for_conflict(conflict.id)
        return conflict

    async def get_claim_ids_for_conflict(self, conflict_id: UUID) -> list[UUID]:
        result = await self._session.execute(
            select(ClaimConflictClaimModel.claim_id).where(
                ClaimConflictClaimModel.conflict_id == conflict_id
            )
        )
        return [row[0] for row in result.all()]

    async def add_claim_to_conflict(self, conflict_id: UUID, claim_id: UUID) -> None:
        stmt = insert(ClaimConflictClaimModel).values(conflict_id=conflict_id, claim_id=claim_id)
        stmt = stmt.on_conflict_do_nothing(index_elements=["conflict_id", "claim_id"])
        await self._session.execute(stmt)

    async def update_with_version_check(
        self, conflict_id: UUID, expected_version: int, updates: dict[str, Any]
    ) -> ClaimConflict | None:
        stmt = (
            update(ClaimConflictModel)
            .where(
                ClaimConflictModel.id == conflict_id, ClaimConflictModel.version == expected_version
            )
            .values(**updates, version=ClaimConflictModel.version + 1)
            .returning(ClaimConflictModel)
        )
        result = await self._session.execute(stmt)
        obj = result.scalar_one_or_none()
        if not obj:
            return None
        conflict = _to_domain(obj, ClaimConflict)
        conflict.claim_ids = await self.get_claim_ids_for_conflict(conflict.id)
        return conflict

    async def find_resolved_by_winning_claim(self, claim_id: UUID) -> ClaimConflict | None:
        """Return the RESOLVED conflict whose winning_claim_id equals claim_id, or None."""
        result = await self._session.execute(
            select(ClaimConflictModel).where(
                ClaimConflictModel.winning_claim_id == claim_id,
                ClaimConflictModel.status == ConflictStatus.RESOLVED.value,
            )
        )
        obj = result.scalar_one_or_none()
        if not obj:
            return None
        conflict = _to_domain(obj, ClaimConflict)
        conflict.claim_ids = await self.get_claim_ids_for_conflict(conflict.id)
        return conflict

    async def find_resolved_by_winning_claims(
        self, claim_ids: list[UUID]
    ) -> dict[UUID, ClaimConflict]:
        """Return a map of winning_claim_id -> RESOLVED conflict for a batch of claim ids.

        A single query replaces the per-claim ``find_resolved_by_winning_claim``
        loop in ``ClaimWriter.write`` when multiple claims are superseded at once.
        """
        if not claim_ids:
            return {}
        unique_claim_ids = list(dict.fromkeys(claim_ids))
        out: dict[UUID, ClaimConflict] = {}
        for chunk in _chunked(unique_claim_ids):
            result = await self._session.execute(
                select(ClaimConflictModel).where(
                    ClaimConflictModel.winning_claim_id.in_(chunk),
                    ClaimConflictModel.status == ConflictStatus.RESOLVED.value,
                )
            )
            for obj in result.scalars():
                if obj.winning_claim_id is None:
                    continue
                conflict = _to_domain(obj, ClaimConflict)
                conflict.claim_ids = await self.get_claim_ids_for_conflict(conflict.id)
                out[obj.winning_claim_id] = conflict
        return out

    async def count_by_status(self, status: ConflictStatus) -> int:
        result = await self._session.execute(
            select(func.count())
            .select_from(ClaimConflictModel)
            .where(ClaimConflictModel.status == status.value)
        )
        return int(result.scalar_one())

    async def count_open_conflicts_per_event(
        self,
        min_count: int = 3,
        limit: int = 50,
    ) -> list[tuple[UUID, int]]:
        if min_count < 2:
            raise ValueError(
                "min_count must be >= 2 (a single OPEN conflict is not a 'high conflict' record)"
            )
        # ``count_col`` is named so the HAVING/ORDER BY clauses below can refer
        # to it directly rather than re-aliasing.  We deliberately do not use
        # the partial index ``uq_open_conflict_event_field`` here because that
        # is unique, not a count source; the supporting index for this query
        # is ``ix_claim_conflicts_event_id`` from migration 001 (the FK index
        # Alembic auto-emits) combined with the ``WHERE status = 'OPEN'``
        # filter.  At realistic conflict volumes this is a fast aggregate.
        count_col = func.count().label("open_count")
        stmt = (
            select(ClaimConflictModel.event_id, count_col)
            .where(ClaimConflictModel.status == ConflictStatus.OPEN.value)
            .group_by(ClaimConflictModel.event_id)
            .having(count_col >= min_count)
            .order_by(count_col.desc(), ClaimConflictModel.event_id.asc())
            .limit(limit)
        )
        result = await self._session.execute(stmt)
        return [(row[0], int(row[1])) for row in result.all()]


class SqlConflictActivityLogRepository(ConflictActivityLogRepository):
    def __init__(self, session: AsyncSession):
        self._session = session

    async def add(self, entry: ConflictActivityLogEntry) -> None:
        self._session.add(ConflictActivityLogModel(**_domain_data(entry)))

    async def next_sequence(self, conflict_id: UUID) -> int:
        # Lock the parent conflict row for the duration of this transaction so
        # concurrent writers cannot race to claim the same sequence number.
        # ``NOWAIT`` is intentionally absent: we want to wait rather than fail,
        # because the window is tiny and callers are already inside a transaction.
        await self._session.execute(
            select(ClaimConflictModel.id)
            .where(ClaimConflictModel.id == conflict_id)
            .with_for_update()
        )
        result = await self._session.execute(
            select(func.coalesce(func.max(ConflictActivityLogModel.sequence), 0) + 1).where(
                ConflictActivityLogModel.conflict_id == conflict_id
            )
        )
        return int(result.scalar_one())

    async def find_by_conflict(
        self,
        conflict_id: UUID,
        *,
        limit: int | None = None,
        after_id: UUID | None = None,
    ) -> list[ConflictActivityLogEntry]:
        stmt = (
            select(ConflictActivityLogModel)
            .where(ConflictActivityLogModel.conflict_id == conflict_id)
            .order_by(ConflictActivityLogModel.sequence, ConflictActivityLogModel.id)
        )
        if after_id is not None:
            cursor_sequence = await self._session.scalar(
                select(ConflictActivityLogModel.sequence).where(
                    ConflictActivityLogModel.conflict_id == conflict_id,
                    ConflictActivityLogModel.id == after_id,
                )
            )
            if cursor_sequence is not None:
                stmt = stmt.where(ConflictActivityLogModel.sequence > cursor_sequence)
        if limit is not None:
            stmt = stmt.limit(limit)
        result = await self._session.execute(stmt)
        return [_to_domain(obj, ConflictActivityLogEntry) for obj in result.scalars()]

    async def find_by_event(
        self,
        event_id: UUID,
        *,
        limit: int | None = None,
        after_id: UUID | None = None,
    ) -> list[ConflictActivityLogEntry]:
        stmt = (
            select(ConflictActivityLogModel)
            .where(ConflictActivityLogModel.event_id == event_id)
            .order_by(ConflictActivityLogModel.created_at, ConflictActivityLogModel.id)
        )
        stmt = await _apply_created_at_uuid_cursor(
            self._session, stmt, ConflictActivityLogModel, after_id
        )
        if limit is not None:
            stmt = stmt.limit(limit)
        result = await self._session.execute(stmt)
        return [_to_domain(obj, ConflictActivityLogEntry) for obj in result.scalars()]

    async def latest_for_conflict(self, conflict_id: UUID) -> ConflictActivityLogEntry | None:
        result = await self._session.execute(
            select(ConflictActivityLogModel)
            .where(ConflictActivityLogModel.conflict_id == conflict_id)
            .order_by(ConflictActivityLogModel.sequence.desc())
            .limit(1)
        )
        obj = result.scalar_one_or_none()
        return _to_domain(obj, ConflictActivityLogEntry) if obj else None
