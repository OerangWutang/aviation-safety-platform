"""Outbox worker: polls and processes outbox events with retry and backoff.

State machine:
    PENDING -> PROCESSING -> PROCESSED
                       ↘ FAILED (retryable, next_attempt_at set) -> PENDING (on retry)
                       ↘ DEAD_LETTER (after max_attempts exhausted)

Exponential backoff:
    next_attempt_at = now + 2^attempt_count seconds (capped at 30 minutes)

Stale lock recovery:
    PROCESSING events locked longer than ``outbox_stale_lock_minutes`` are
    reset to PENDING when budget remains, or moved to DEAD_LETTER if the
    crashed worker had already exhausted ``max_attempts``.

Lease fencing:
    Every ``update_status`` call after the initial lock identifies the lock
    holder by ``(worker_id, attempt_count)``. If a stale-recovery sweep has
    re-issued the lock to another worker between fetch and write, the late
    write becomes a no-op rather than overwriting the new outcome.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from datetime import UTC, datetime, timedelta
from uuid import UUID

from atlas.application.use_cases.echo_crossref import (
    RunEchoCrossReference,
    RunEchoCrossReferenceInput,
)
from atlas.application.use_cases.reproject_event import ReProjectEvent
from atlas.config import get_settings
from atlas.domain.entities import OutboxEvent
from atlas.domain.enums import OutboxStatus
from atlas.domain.tenancy.entities import CrossrefResultStatus
from atlas.infrastructure.db.unit_of_work import (
    create_public_uow,
    create_tenant_uow,
    create_uow,
)

logger = logging.getLogger(__name__)

# Cap for exponential backoff: 2^N seconds, max 30 minutes.
_MAX_BACKOFF_SECONDS = 1800


def _next_attempt_at(attempt_count: int) -> datetime:
    """Compute the earliest retry time using capped exponential backoff."""
    delay = min(2**attempt_count, _MAX_BACKOFF_SECONDS)
    return datetime.now(UTC) + timedelta(seconds=delay)


class OutboxWorker:
    def __init__(self, worker_id: str = "worker-1") -> None:
        self._worker_id = worker_id

    async def process_batch(self, limit: int = 10) -> int:
        settings = get_settings()
        # Atomically lock eligible events (PENDING + retryable FAILED) and
        # commit the lock in its own transaction so concurrent workers cannot
        # double-claim.
        async with create_uow() as uow:
            events = await uow.outbox.fetch_and_lock_pending(
                limit, self._worker_id, max_attempts=settings.outbox_max_attempts
            )
            await uow.commit()

        processed = 0
        for event in events:
            try:
                async with create_uow() as event_uow:
                    succeeded = await self._process_event(event_uow, event)
                    if succeeded:
                        await event_uow.commit()
                    else:
                        # Lost the lease (another worker took over): roll back
                        # any partial writes from this attempt so we don't
                        # leave orphans behind, and let that worker own it.
                        await event_uow.rollback()
                if succeeded:
                    processed += 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover - defensive worker path
                logger.exception("Error processing outbox event %s", event.id)
                await self._mark_failed(event, str(exc))
        return processed

    async def _mark_failed(self, event: OutboxEvent, error: str) -> None:  # pragma: no cover
        """Persist FAILED status with exponential backoff or dead-letter the event.

        Fenced by ``(worker_id, attempt_count)`` so that a worker which lost
        its lease (e.g. due to a stale-recovery sweep) does not overwrite the
        successor's outcome with its own failure. Echo cross-reference results
        stay ``PENDING`` while retries remain available and become ``FAILED``
        only after the outbox event is dead-lettered.
        """
        settings = get_settings()
        if event.attempt_count >= settings.outbox_max_attempts:
            new_status = OutboxStatus.DEAD_LETTER
            next_at = None
            logger.error(
                "Outbox event %s dead-lettered after %d attempts: %s",
                event.id,
                event.attempt_count,
                error,
            )
        else:
            new_status = OutboxStatus.FAILED
            next_at = _next_attempt_at(event.attempt_count)
            logger.warning(
                "Outbox event %s failed (attempt %d/%d), retry after %s",
                event.id,
                event.attempt_count,
                settings.outbox_max_attempts,
                next_at.isoformat(),
            )
        async with create_uow() as fail_uow:
            applied = await fail_uow.outbox.update_status(
                event.id,
                new_status,
                event.attempt_count,
                last_error=error,
                next_attempt_at=next_at,
                expected_worker_id=self._worker_id,
                expected_attempt_count=event.attempt_count,
            )
            if applied:
                await fail_uow.commit()
            else:
                # Lost the lease - another worker now owns this event. Don't
                # commit our outdated failure record; just roll back.
                logger.warning(
                    "Outbox event %s: lease lost during failure write - "
                    "another worker owns it; not overwriting",
                    event.id,
                )
                await fail_uow.rollback()
                return

        if new_status == OutboxStatus.DEAD_LETTER and event.event_type == "ECHO_CROSSREF_REQUESTED":
            await self._mark_echo_result_failed(event, error)

    async def _mark_echo_result_failed(self, event: OutboxEvent, error: str) -> None:
        """Mark the user-visible Echo result FAILED after final outbox failure."""
        try:
            tenant_id = UUID(str(event.payload["tenant_id"]))
            result_id = UUID(str(event.payload["crossref_result_id"]))
        except Exception:
            logger.exception("Malformed Echo outbox payload for event %s", event.id)
            return

        async with create_tenant_uow(tenant_id) as tenant_uow:
            await tenant_uow.tenant_crossref_results.mark_failed(
                tenant_id=tenant_id,
                result_id=result_id,
                error_detail=f"Echo worker exhausted retry budget: {error}",
                completed_at=datetime.now(UTC),
            )
            await tenant_uow.commit()

    async def recover_stale_locks(self) -> int:
        """Re-queue expired locks and fail visible Echo jobs that are dead-lettered.

        Normal processing failures call ``_mark_failed`` and eventually mark the
        Echo result FAILED after retry exhaustion. A process crash on the final
        attempt takes a different path: stale-lock recovery dead-letters the
        outbox row directly. This method therefore captures the rows newly moved
        to DEAD_LETTER and updates their tenant-visible result state too.
        """
        settings = get_settings()
        async with create_uow() as uow:
            count, deadlettered = await uow.outbox.requeue_stale_locked_with_dead_letters(
                stale_after_minutes=settings.outbox_stale_lock_minutes,
                max_attempts=settings.outbox_max_attempts,
            )
            await uow.commit()
        for event in deadlettered:
            if event.event_type == "ECHO_CROSSREF_REQUESTED":
                await self._mark_echo_result_failed(
                    event,
                    "Outbox event was dead-lettered during stale-lock recovery "
                    "after exhausting retry attempts.",
                )
        if count:
            logger.warning("Recovered %d stale outbox events", count)
        return count

    async def run_loop(self, sleep_seconds: float = 5.0) -> None:
        get_settings().validate_worker_runtime_settings()
        logger.info("OutboxWorker %s started (sleep=%ss)", self._worker_id, sleep_seconds)
        while True:
            try:
                processed = await self.process_batch()
                await self.recover_stale_locks()
                await self._record_heartbeat(successful_batch=processed > 0)
                if processed:
                    logger.info("OutboxWorker processed %d events", processed)
            except asyncio.CancelledError:
                logger.info("OutboxWorker %s shutting down (cancelled)", self._worker_id)
                raise
            except Exception:  # pragma: no cover - defensive worker loop
                logger.exception("OutboxWorker loop error - continuing")
            await asyncio.sleep(sleep_seconds)

    async def _record_heartbeat(self, *, successful_batch: bool) -> None:
        """Persist a lightweight worker progress heartbeat for metrics/alerts."""
        try:
            async with create_uow() as uow:
                await uow.outbox.record_worker_heartbeat(
                    self._worker_id, successful_batch=successful_batch
                )
                await uow.commit()
        except Exception:  # pragma: no cover - defensive observability path
            logger.exception("Failed to record outbox worker heartbeat")

    async def run_continuous(self, sleep_seconds: float = 5.0) -> None:
        """Backward-compatible alias for older CLI callers."""
        await self.run_loop(sleep_seconds=sleep_seconds)

    async def _process_event(self, uow, event) -> bool:
        """Process a single locked outbox event.

        Returns True if the worker still owned the lease at the moment it
        wrote the terminal status (and therefore the caller should commit),
        False if the lease was lost mid-flight (caller should roll back).
        """
        if event.event_type == "ECHO_CROSSREF_REQUESTED":
            return await self._process_echo_crossref_event(uow, event)

        if event.event_type != "CLAIMS_UPDATED":
            logger.warning("Unknown outbox event type %r - dead-lettering", event.event_type)
            return await uow.outbox.update_status(
                event.id,
                OutboxStatus.DEAD_LETTER,
                event.attempt_count,
                last_error=f"Unknown event type: {event.event_type}",
                expected_worker_id=self._worker_id,
                expected_attempt_count=event.attempt_count,
            )

        payload = event.payload
        event_id = UUID(payload["event_id"])

        # Idempotency check: if a projection history row for this outbox event
        # already exists, mark the event processed and skip. Merged/absorbed
        # events are the exception: an older history row must not leave a stale
        # public projection in place, so let ReProjectEvent enforce the
        # tombstone invariant before acknowledging the event.
        existing = await uow.projection_history.find_by_outbox_event(event.id)
        if existing:
            event_row = await uow.events.get(event_id)
            if event_row is None or not event_row.is_merged:
                return await uow.outbox.update_status(
                    event.id,
                    OutboxStatus.PROCESSED,
                    event.attempt_count,
                    expected_worker_id=self._worker_id,
                    expected_attempt_count=event.attempt_count,
                )

        ingestion_run_id = payload.get("ingestion_run_id")
        await ReProjectEvent(uow).execute(
            event_id=event_id,
            caused_by_ingestion_run_id=UUID(ingestion_run_id) if ingestion_run_id else None,
            caused_by_outbox_event_id=event.id,
            commit=False,
        )
        return await uow.outbox.update_status(
            event.id,
            OutboxStatus.PROCESSED,
            event.attempt_count,
            expected_worker_id=self._worker_id,
            expected_attempt_count=event.attempt_count,
        )

    async def _process_echo_crossref_event(self, uow, event: OutboxEvent) -> bool:
        """Run a durable Echo cross-reference job from the outbox.

        The tenant result intentionally remains ``PENDING`` if this attempt
        raises: the outbox failure path will schedule a retry.  Only when the
        event is dead-lettered do we mark the visible result ``FAILED``.
        """
        payload = event.payload
        tenant_id = UUID(str(payload["tenant_id"]))
        result_id = UUID(str(payload["crossref_result_id"]))

        async with create_tenant_uow(tenant_id) as tenant_uow:
            result = await tenant_uow.tenant_crossref_results.get(
                tenant_id=tenant_id,
                result_id=result_id,
            )
            if result is None:
                raise RuntimeError(
                    f"Echo crossref result {result_id} not found for tenant {tenant_id}"
                )

            # Idempotency: the worker can crash after the tenant UoW commits the
            # COMPLETE/FAILED result but before the system outbox row is marked
            # PROCESSED.  A retry must acknowledge the already-terminal tenant
            # result instead of re-running the non-idempotent matching use case
            # and dead-lettering a successfully completed user-visible job.
            if result.status in {CrossrefResultStatus.COMPLETE, CrossrefResultStatus.FAILED}:
                logger.info(
                    "Outbox event %s already has terminal Echo result %s; acknowledging",
                    event.id,
                    result.status,
                )
            elif result.status == CrossrefResultStatus.PENDING:
                async with create_public_uow() as public_uow:
                    await RunEchoCrossReference(
                        tenant_uow=tenant_uow,
                        public_uow=public_uow,
                        mark_failed_on_error=False,
                    ).execute(
                        RunEchoCrossReferenceInput(
                            tenant_id=tenant_id,
                            crossref_result_id=result_id,
                        )
                    )
            else:  # pragma: no cover - defensive against future enum additions
                raise RuntimeError(
                    f"Unexpected Echo crossref result status {result.status!r} for {result_id}"
                )

        return await uow.outbox.update_status(
            event.id,
            OutboxStatus.PROCESSED,
            event.attempt_count,
            expected_worker_id=self._worker_id,
            expected_attempt_count=event.attempt_count,
        )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the Atlas outbox worker loop")
    parser.add_argument("--worker-id", default="module-worker")
    parser.add_argument("--sleep-seconds", type=float, default=5.0)
    return parser.parse_args()


def main() -> None:  # pragma: no cover - process entrypoint
    args = _parse_args()
    asyncio.run(OutboxWorker(worker_id=args.worker_id).run_loop(sleep_seconds=args.sleep_seconds))


if __name__ == "__main__":  # pragma: no cover - process entrypoint
    main()
