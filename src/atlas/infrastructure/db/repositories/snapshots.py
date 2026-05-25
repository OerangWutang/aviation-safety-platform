"""SQLAlchemy repositories for the snapshots aggregate.

Carved out of the old ``repositories.py`` monolith in r9; behaviour
unchanged.  Public ``Sql*`` classes are re-exported from
``atlas.infrastructure.db.repositories`` so existing imports keep
working.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from atlas.domain.entities import (
    RawSnapshot,
)
from atlas.domain.interfaces.repositories import (
    RawSnapshotRepository,
)
from atlas.infrastructure.db.orm_models import (
    ClaimModel,
    RawSnapshotModel,
)
from atlas.infrastructure.db.repositories._helpers import (
    ADVISORY_LOCK_SOURCE_RECORD_CORRECTION,
    _domain_data,
    _to_domain_opt,
)


class SqlRawSnapshotRepository(RawSnapshotRepository):
    def __init__(self, session: AsyncSession):
        self._session = session

    async def add(self, snapshot: RawSnapshot) -> None:
        self._session.add(RawSnapshotModel(**_domain_data(snapshot)))

    async def get(self, snapshot_id: UUID) -> RawSnapshot | None:
        # Plain PK lookup.  No row-level locking — the audit read path
        # is read-only and concurrent ingestion is allowed to win.
        obj = await self._session.get(RawSnapshotModel, snapshot_id)
        return _to_domain_opt(obj, RawSnapshot)

    async def try_add_unique(self, snapshot: RawSnapshot) -> bool:
        data = _domain_data(snapshot)
        # Build the full statement in a single chain so mypy infers
        # ``ReturningInsert[tuple[UUID]]`` without a re-assignment widening
        # back to plain ``Insert``.
        stmt = (
            insert(RawSnapshotModel)
            .values(**data)
            .on_conflict_do_nothing(constraint="uq_raw_snapshot_ingestion_key")
            .returning(RawSnapshotModel.id)
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none() is not None

    async def find_by_source_run(
        self,
        source_id: UUID,
        ingestion_run_id: UUID,
    ) -> RawSnapshot | None:
        result = await self._session.execute(
            select(RawSnapshotModel)
            .where(
                RawSnapshotModel.source_id == source_id,
                RawSnapshotModel.ingestion_run_id == ingestion_run_id,
            )
            .order_by(RawSnapshotModel.created_at.desc())
            .limit(1)
        )
        obj = result.scalar_one_or_none()
        return _to_domain_opt(obj, RawSnapshot)

    async def update_ingestion_result(self, snapshot_id: UUID, result_json: dict[str, Any]) -> None:
        result = await self._session.execute(
            update(RawSnapshotModel)
            .where(RawSnapshotModel.id == snapshot_id)
            .values(ingestion_result_json=result_json)
        )
        if getattr(result, "rowcount", 0) != 1:
            raise RuntimeError(f"Failed to persist ingestion result for snapshot {snapshot_id}")

    async def find_latest_by_source_record_id(
        self,
        source_id: UUID,
        source_record_id: str,
    ) -> RawSnapshot | None:
        """Return the most recent snapshot for this (source, record_id) pair."""
        result = await self._session.execute(
            select(RawSnapshotModel)
            .where(
                RawSnapshotModel.source_id == source_id,
                RawSnapshotModel.source_record_id == source_record_id,
            )
            .order_by(RawSnapshotModel.created_at.desc())
            .limit(1)
        )
        obj = result.scalar_one_or_none()
        return _to_domain_opt(obj, RawSnapshot)

    async def find_latest_event_id_by_source_record_id(
        self,
        source_id: UUID,
        source_record_id: str,
    ) -> UUID | None:
        """Return the latest event owner for this source record, skipping orphans."""
        result = await self._session.execute(
            select(ClaimModel.event_id)
            .join(RawSnapshotModel, ClaimModel.raw_snapshot_id == RawSnapshotModel.id)
            .where(
                RawSnapshotModel.source_id == source_id,
                RawSnapshotModel.source_record_id == source_record_id,
            )
            .order_by(RawSnapshotModel.created_at.desc(), ClaimModel.created_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def lock_for_source_record_correction(
        self,
        source_id: UUID,
        source_record_id: str,
    ) -> None:
        """Acquire a transaction-scoped advisory lock for this (source, record) pair.

        Serialises concurrent corrections for the same source_record_id so that
        only one writer reads the prior snapshot, supersedes old claims, and
        inserts new ones at a time.  The second concurrent transaction will block
        here until the first commits, then pick up the already-updated state.

        The lock uses the two-int advisory-lock form with namespace
        ``ADVISORY_LOCK_SOURCE_RECORD_CORRECTION`` and ``hashtext(key)`` so it
        cannot collide with reprojection or identity-resolution locks.
        """
        key = f"{source_id}:{source_record_id}"
        await self._session.execute(
            text("SELECT pg_advisory_xact_lock(CAST(:namespace AS integer), hashtext(:k))"),
            {"namespace": ADVISORY_LOCK_SOURCE_RECORD_CORRECTION, "k": key},
        )
