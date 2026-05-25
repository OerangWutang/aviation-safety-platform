"""SQLAlchemy repositories for the outbox aggregate.

Carved out of the old ``repositories.py`` monolith in r9; behaviour
unchanged.  Public ``Sql*`` classes are re-exported from
``atlas.infrastructure.db.repositories`` so existing imports keep
working.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from atlas.domain.entities import (
    OutboxEvent,
)
from atlas.domain.enums import (
    OutboxStatus,
)
from atlas.domain.interfaces.repositories import (
    OutboxRepository,
)
from atlas.infrastructure.db.orm_models import (
    OutboxEventModel,
    OutboxWorkerHeartbeatModel,
)
from atlas.infrastructure.db.repositories._helpers import (
    _domain_data,
    _to_domain,
)


class SqlOutboxRepository(OutboxRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, event: OutboxEvent) -> None:
        self._session.add(OutboxEventModel(**_domain_data(event)))

    async def fetch_and_lock_pending(
        self, limit: int, worker_id: str, max_attempts: int = 5
    ) -> list[OutboxEvent]:
        """Atomically select and lock eligible events for processing.

        Poll PENDING and due FAILED retries through separate index-friendly
        CTEs instead of one OR-heavy predicate. Each candidate CTE uses
        ``FOR UPDATE SKIP LOCKED`` so concurrent workers do not contend, and
        the final selection preserves oldest-created ordering across both
        streams while respecting the caller's total limit.
        """
        now = datetime.now(UTC)
        sql = text(
            """
            WITH pending_candidates AS (
                SELECT id, created_at
                FROM outbox_events
                WHERE status = :pending_status
                ORDER BY created_at, id
                LIMIT :limit
                FOR UPDATE SKIP LOCKED
            ),
            failed_candidates AS (
                SELECT id, created_at
                FROM outbox_events
                WHERE status = :failed_status
                  AND attempt_count < :max_attempts
                  AND (next_attempt_at IS NULL OR next_attempt_at <= :now)
                ORDER BY next_attempt_at NULLS FIRST, created_at, id
                LIMIT :limit
                FOR UPDATE SKIP LOCKED
            ),
            selected AS (
                SELECT id
                FROM (
                    SELECT id, created_at FROM pending_candidates
                    UNION ALL
                    SELECT id, created_at FROM failed_candidates
                ) eligible
                ORDER BY created_at, id
                LIMIT :limit
            )
            UPDATE outbox_events
            SET status = :processing_status,
                locked_at = :now,
                locked_by = :worker_id,
                attempt_count = attempt_count + 1
            FROM selected
            WHERE outbox_events.id = selected.id
            RETURNING outbox_events.*
            """
        )
        result = await self._session.execute(
            sql,
            {
                "pending_status": OutboxStatus.PENDING.value,
                "failed_status": OutboxStatus.FAILED.value,
                "processing_status": OutboxStatus.PROCESSING.value,
                "now": now,
                "limit": limit,
                "worker_id": worker_id,
                "max_attempts": max_attempts,
            },
        )
        return [
            _to_domain(OutboxEventModel(**row._mapping), OutboxEvent) for row in result.fetchall()
        ]

    async def update_status(
        self,
        event_id: UUID,
        status: OutboxStatus,
        attempt_count: int,
        last_error: str | None = None,
        next_attempt_at: datetime | None = None,
        expected_worker_id: str | None = None,
        expected_attempt_count: int | None = None,
    ) -> bool:
        """Persist a status change, optionally fenced by lock ownership.

        When ``expected_worker_id`` and ``expected_attempt_count`` are provided,
        the row is updated only if its current ``status='PROCESSING'``,
        ``locked_by=expected_worker_id``, and ``attempt_count=expected_attempt_count``.
        This protects against the race where:

            - Worker A locks the event (attempt N).
            - Worker A hangs past the stale threshold.
            - Stale recovery requeues the event.
            - Worker B locks and processes it (attempt N+1).
            - Worker A wakes up and tries to write its outdated result.

        Without fencing, Worker A would overwrite Worker B's outcome. With the
        WHERE clause below, Worker A's UPDATE matches zero rows and returns
        False, signalling that the worker lost ownership and must not act on
        the result. Callers SHOULD pass the fencing parameters in production.
        """
        now = datetime.now(UTC)
        values: dict[str, Any] = {
            "status": status.value,
            "attempt_count": attempt_count,
            "last_error": last_error,
            "next_attempt_at": next_attempt_at,
            "locked_at": None,
            "locked_by": None,
        }
        if status == OutboxStatus.PROCESSED:
            values["processed_at"] = now

        stmt = update(OutboxEventModel).where(OutboxEventModel.id == event_id)
        if expected_worker_id is not None and expected_attempt_count is not None:
            stmt = stmt.where(
                OutboxEventModel.status == OutboxStatus.PROCESSING.value,
                OutboxEventModel.locked_by == expected_worker_id,
                OutboxEventModel.attempt_count == expected_attempt_count,
            )
        stmt = stmt.values(**values).returning(OutboxEventModel.id)
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none() is not None

    async def list_recent(self, limit: int = 50) -> list[OutboxEvent]:
        result = await self._session.execute(
            select(OutboxEventModel).order_by(OutboxEventModel.created_at.desc()).limit(limit)
        )
        return [_to_domain(obj, OutboxEvent) for obj in result.scalars()]

    async def requeue_stale_locked_with_dead_letters(
        self, stale_after_minutes: int = 10, max_attempts: int = 5
    ) -> tuple[int, list[OutboxEvent]]:
        """Sweep stale PROCESSING locks and return rows newly dead-lettered.

        The worker uses the returned dead-letter rows to update any user-visible
        job/result records that would otherwise remain PENDING after a crashed
        final attempt. ``requeue_stale_locked`` remains as the legacy count-only
        wrapper for tests and callers that do not need those rows.
        """
        cutoff = datetime.now(UTC) - timedelta(minutes=stale_after_minutes)

        requeue_stmt = (
            update(OutboxEventModel)
            .where(
                OutboxEventModel.status == OutboxStatus.PROCESSING.value,
                OutboxEventModel.locked_at < cutoff,
                OutboxEventModel.attempt_count < max_attempts,
            )
            .values(
                status=OutboxStatus.PENDING.value,
                locked_at=None,
                locked_by=None,
                next_attempt_at=None,
            )
        )
        requeue_result = await self._session.execute(requeue_stmt)
        requeued = int(getattr(requeue_result, "rowcount", 0) or 0)

        suffix = f" [stale lock dead-lettered at {datetime.now(UTC).isoformat()}]"
        deadletter_stmt = (
            update(OutboxEventModel)
            .where(
                OutboxEventModel.status == OutboxStatus.PROCESSING.value,
                OutboxEventModel.locked_at < cutoff,
                OutboxEventModel.attempt_count >= max_attempts,
            )
            .values(
                status=OutboxStatus.DEAD_LETTER.value,
                locked_at=None,
                locked_by=None,
                next_attempt_at=None,
                last_error=func.coalesce(OutboxEventModel.last_error, "") + suffix,
            )
            .returning(OutboxEventModel)
        )
        deadletter_result = await self._session.execute(deadletter_stmt)
        deadlettered = [_to_domain(obj, OutboxEvent) for obj in deadletter_result.scalars().all()]
        return requeued + len(deadlettered), deadlettered

    async def requeue_stale_locked(
        self, stale_after_minutes: int = 10, max_attempts: int = 5
    ) -> int:
        """Sweep stale PROCESSING locks: retry budgeted rows and dead-letter exhausted rows."""
        count, _ = await self.requeue_stale_locked_with_dead_letters(
            stale_after_minutes=stale_after_minutes,
            max_attempts=max_attempts,
        )
        return count

    async def count_by_status(self, status: OutboxStatus) -> int:
        result = await self._session.execute(
            select(func.count())
            .select_from(OutboxEventModel)
            .where(OutboxEventModel.status == status.value)
        )
        return int(result.scalar_one())

    async def oldest_unprocessed_age_seconds(self) -> float | None:
        result = await self._session.execute(
            select(OutboxEventModel.created_at)
            .where(
                OutboxEventModel.status.in_(
                    [
                        OutboxStatus.PENDING.value,
                        OutboxStatus.PROCESSING.value,
                        OutboxStatus.FAILED.value,
                    ]
                )
            )
            .order_by(OutboxEventModel.created_at, OutboxEventModel.id)
            .limit(1)
        )
        oldest = result.scalar_one_or_none()
        if oldest is None:
            return None
        return max(0.0, (datetime.now(UTC) - oldest).total_seconds())

    async def record_worker_heartbeat(
        self, worker_id: str, *, successful_batch: bool = False
    ) -> None:
        now = datetime.now(UTC)
        stmt = insert(OutboxWorkerHeartbeatModel).values(
            worker_id=worker_id,
            last_loop_at=now,
            last_successful_batch_at=now if successful_batch else None,
            updated_at=now,
        )
        update_values = {
            "last_loop_at": now,
            "updated_at": now,
        }
        if successful_batch:
            update_values["last_successful_batch_at"] = now
        await self._session.execute(
            stmt.on_conflict_do_update(
                index_elements=[OutboxWorkerHeartbeatModel.worker_id],
                set_=update_values,
            )
        )

    async def worker_heartbeat_age_seconds(self) -> float | None:
        result = await self._session.execute(
            select(func.max(OutboxWorkerHeartbeatModel.last_loop_at))
        )
        newest = result.scalar_one_or_none()
        if newest is None:
            return None
        return max(0.0, (datetime.now(UTC) - newest).total_seconds())

    async def worker_successful_batch_age_seconds(self) -> float | None:
        result = await self._session.execute(
            select(func.max(OutboxWorkerHeartbeatModel.last_successful_batch_at))
        )
        newest = result.scalar_one_or_none()
        if newest is None:
            return None
        return max(0.0, (datetime.now(UTC) - newest).total_seconds())
