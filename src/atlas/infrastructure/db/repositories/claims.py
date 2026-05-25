"""SQLAlchemy repositories for the claims aggregate.

Carved out of the old ``repositories.py`` monolith in r9; behaviour
unchanged.  Public ``Sql*`` classes are re-exported from
``atlas.infrastructure.db.repositories`` so existing imports keep
working.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from atlas.domain.entities import (
    Claim,
    ClaimHistory,
)
from atlas.domain.enums import (
    ClaimType,
)
from atlas.domain.interfaces.repositories import (
    ClaimHistoryRepository,
    ClaimRepository,
)
from atlas.infrastructure.db.orm_models import (
    ClaimHistoryModel,
    ClaimModel,
    RawSnapshotModel,
)
from atlas.infrastructure.db.repositories._helpers import (
    _apply_created_at_uuid_cursor,
    _chunked,
    _domain_data,
    _to_domain,
    _to_domain_opt,
)


class SqlClaimRepository(ClaimRepository):
    def __init__(self, session: AsyncSession):
        self._session = session

    async def get(self, id: UUID) -> Claim | None:
        obj = await self._session.get(ClaimModel, id)
        return _to_domain_opt(obj, Claim)

    async def lock_for_update(self, id: UUID) -> Claim | None:
        result = await self._session.execute(
            select(ClaimModel).where(ClaimModel.id == id).with_for_update()
        )
        return _to_domain_opt(result.scalar_one_or_none(), Claim)

    async def get_many(self, claim_ids: list[UUID]) -> list[Claim]:
        if not claim_ids:
            return []
        unique_claim_ids = list(dict.fromkeys(claim_ids))
        claims_by_id: dict[UUID, Claim] = {}
        for chunk in _chunked(unique_claim_ids):
            result = await self._session.execute(select(ClaimModel).where(ClaimModel.id.in_(chunk)))
            for obj in result.scalars():
                claims_by_id[obj.id] = _to_domain(obj, Claim)
        return [claims_by_id[claim_id] for claim_id in unique_claim_ids if claim_id in claims_by_id]

    async def add(self, claim: Claim) -> None:
        self._session.add(ClaimModel(**_domain_data(claim)))

    async def update(self, claim: Claim) -> None:
        await self._session.merge(ClaimModel(**_domain_data(claim)))

    async def find_active_by_event(self, event_id: UUID) -> list[Claim]:
        result = await self._session.execute(
            select(ClaimModel).where(
                ClaimModel.event_id == event_id,
                ClaimModel.claim_type.in_(ClaimType.active_values()),
            )
        )
        return [_to_domain(obj, Claim) for obj in result.scalars()]

    async def find_all_by_event(
        self,
        event_id: UUID,
        *,
        limit: int | None = None,
        after_id: UUID | None = None,
    ) -> list[Claim]:
        stmt = (
            select(ClaimModel)
            .where(ClaimModel.event_id == event_id)
            .order_by(ClaimModel.created_at, ClaimModel.id)
        )
        stmt = await _apply_created_at_uuid_cursor(self._session, stmt, ClaimModel, after_id)
        if limit is not None:
            stmt = stmt.limit(limit)
        result = await self._session.execute(stmt)
        return [_to_domain(obj, Claim) for obj in result.scalars()]

    async def find_event_id_by_raw_snapshot_id(self, raw_snapshot_id: UUID) -> UUID | None:
        # Backward-compat fallback for pre-result-json snapshots.  Prefer active
        # claims over superseded claims and use deterministic ordering so merges
        # that copy claims to a canonical target do not randomly replay the
        # absorbed event.  New snapshots should replay from ingestion_result_json.
        result = await self._session.execute(
            select(ClaimModel.event_id)
            .where(ClaimModel.raw_snapshot_id == raw_snapshot_id)
            .order_by(
                text("CASE WHEN claim_type = 'SUPERSEDED' THEN 1 ELSE 0 END"),
                ClaimModel.created_at.desc(),
                ClaimModel.id.asc(),
            )
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def find_active_by_event_field(self, event_id: UUID, field_name: str) -> list[Claim]:
        # Use SELECT ... FOR UPDATE to prevent concurrent ingestions from both
        # reading the same prior active claims, both inserting new ones, and
        # leaving multiple same-source active claims where only one should remain.
        # This method is only called on the source_record_id=None supersession
        # path; the source_record_id path is protected by an advisory lock at
        # the ingestion use-case level.
        result = await self._session.execute(
            select(ClaimModel)
            .where(
                ClaimModel.event_id == event_id,
                ClaimModel.field_name == field_name,
                ClaimModel.claim_type.in_(ClaimType.active_values()),
            )
            .with_for_update()
        )
        return [_to_domain(obj, Claim) for obj in result.scalars()]

    async def find_active_by_source_record(
        self, source_id: UUID, source_record_id: str
    ) -> list[Claim]:
        result = await self._session.execute(
            select(ClaimModel)
            .join(RawSnapshotModel, RawSnapshotModel.id == ClaimModel.raw_snapshot_id)
            .where(
                ClaimModel.source_id == source_id,
                ClaimModel.claim_type.in_(ClaimType.active_values()),
                RawSnapshotModel.source_id == source_id,
                RawSnapshotModel.source_record_id == source_record_id,
            )
        )
        return [_to_domain(obj, Claim) for obj in result.scalars()]

    async def bulk_supersede(self, claim_ids: list[UUID], by_claim_id: UUID) -> list[Claim]:
        if not claim_ids:
            return []

        unique_claim_ids = list(dict.fromkeys(claim_ids))
        claims: list[Claim] = []
        for chunk in _chunked(unique_claim_ids):
            result = await self._session.execute(
                select(ClaimModel)
                .where(
                    ClaimModel.id.in_(chunk),
                    ClaimModel.claim_type.in_(ClaimType.active_values()),
                )
                # Lock the rows being superseded so that a concurrent ingestion
                # that reads the same active claims and also calls bulk_supersede
                # blocks until the first transaction commits.  Without this lock
                # two concurrent ingestions for the same source/event/field can
                # both read the same prior active claims and both insert new ones,
                # leaving multiple same-source active claims where only one should
                # remain.  The caller's find_active_by_event_field also locks, so
                # both sides of the read-modify-write are serialised.
                .with_for_update()
            )
            # Snapshot only currently active/winnable claims. Historical
            # SUPERSEDED claims are audit evidence and their lineage must not be
            # overwritten even if a caller accidentally passes them in.
            claims.extend(_to_domain(obj, Claim) for obj in result.scalars())

        active_ids = [claim.id for claim in claims]
        for chunk in _chunked(active_ids):
            await self._session.execute(
                update(ClaimModel)
                .where(
                    ClaimModel.id.in_(chunk),
                    ClaimModel.claim_type.in_(ClaimType.active_values()),
                )
                .values(
                    claim_type=ClaimType.SUPERSEDED.value,
                    superseded_by_claim_id=by_claim_id,
                )
                .execution_options(synchronize_session="fetch"),
            )
        return claims

    async def find_superseded_by(self, by_claim_id: UUID) -> list[Claim]:
        """Return claims whose ``superseded_by_claim_id`` equals ``by_claim_id``."""
        result = await self._session.execute(
            select(ClaimModel).where(ClaimModel.superseded_by_claim_id == by_claim_id)
        )
        return [_to_domain(obj, Claim) for obj in result.scalars()]

    async def bulk_unsupersede(self, claim_ids: list[UUID]) -> list[Claim]:
        """Reactivate SUPERSEDED claims and restore their prior claim_type.

        Manual conflict reopen needs to make the losing claims eligible again,
        but it must not erase their semantics.  A claim that was CONFIRMED
        before being superseded should become CONFIRMED again, not RAW.  The
        most recent ClaimHistory row that moved the claim to SUPERSEDED records
        the previous type in ``from_claim_type``; when that audit record is not
        available, RAW is the conservative fallback for legacy rows.

        Returns the claims after reactivation so callers can log the restored
        ``to_claim_type`` accurately.
        """
        if not claim_ids:
            return []

        unique_claim_ids = list(dict.fromkeys(claim_ids))
        claim_models: list[ClaimModel] = []
        for chunk in _chunked(unique_claim_ids):
            result = await self._session.execute(select(ClaimModel).where(ClaimModel.id.in_(chunk)))
            claim_models.extend(result.scalars())
        if not claim_models:
            return []

        restore_types: dict[UUID, str] = {}
        for chunk in _chunked([obj.id for obj in claim_models]):
            history_result = await self._session.execute(
                select(ClaimHistoryModel)
                .where(
                    ClaimHistoryModel.claim_id.in_(chunk),
                    ClaimHistoryModel.to_claim_type == ClaimType.SUPERSEDED.value,
                )
                .order_by(ClaimHistoryModel.created_at.desc())
            )
            for history in history_result.scalars():
                if history.claim_id not in restore_types:
                    restored = history.from_claim_type or ClaimType.RAW.value
                    if restored == ClaimType.SUPERSEDED.value:
                        restored = ClaimType.RAW.value
                    restore_types[history.claim_id] = restored

        reactivated: list[Claim] = []
        for obj in claim_models:
            restored_type = restore_types.get(obj.id, ClaimType.RAW.value)
            await self._session.execute(
                update(ClaimModel)
                .where(ClaimModel.id == obj.id)
                .values(
                    claim_type=restored_type,
                    superseded_by_claim_id=None,
                )
                .execution_options(synchronize_session="fetch"),
            )
            claim = _to_domain(obj, Claim)
            claim.claim_type = ClaimType(restored_type)
            claim.superseded_by_claim_id = None
            reactivated.append(claim)
        return reactivated

    async def count_total(self) -> int:
        result = await self._session.execute(select(func.count()).select_from(ClaimModel))
        return int(result.scalar_one())


class SqlClaimHistoryRepository(ClaimHistoryRepository):
    def __init__(self, session: AsyncSession):
        self._session = session

    async def add(self, history: ClaimHistory) -> None:
        self._session.add(ClaimHistoryModel(**_domain_data(history)))

    async def find_by_event(
        self,
        event_id: UUID,
        *,
        limit: int | None = None,
        after_id: UUID | None = None,
    ) -> list[ClaimHistory]:
        stmt = (
            select(ClaimHistoryModel)
            .where(ClaimHistoryModel.event_id == event_id)
            .order_by(ClaimHistoryModel.created_at, ClaimHistoryModel.id)
        )
        stmt = await _apply_created_at_uuid_cursor(self._session, stmt, ClaimHistoryModel, after_id)
        if limit is not None:
            stmt = stmt.limit(limit)
        result = await self._session.execute(stmt)
        return [_to_domain(obj, ClaimHistory) for obj in result.scalars()]
