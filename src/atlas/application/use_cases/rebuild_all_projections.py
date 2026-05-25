from __future__ import annotations

import logging
from dataclasses import dataclass, field
from uuid import UUID

from atlas.application.unit_of_work import UnitOfWork
from atlas.application.use_cases.reproject_event import ReProjectEvent
from atlas.domain.exceptions import DomainValidationError

logger = logging.getLogger(__name__)

DEFAULT_BATCH_SIZE = 100


@dataclass(frozen=True)
class RebuildAllResult:
    processed: int = 0
    skipped: int = 0
    failed_event_ids: list[UUID] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def failed_count(self) -> int:
        return len(self.failed_event_ids)


class RebuildAllProjections:
    """Rebuild projections for all or a capped subset of accident events.

    Warning: this use case commits after each event. If the process is
    interrupted mid-run, some events may have newer projection versions while
    others still hold older projections. Operators should use the verify
    endpoint for spot checks, or a future rebuild-runs table for epoch-level
    consistency tracking.

    Event ids are scanned with keyset pagination rather than offsets. This
    prevents rows from being skipped or processed twice because earlier pages
    changed while the rebuild was running. New rows inserted with UUIDs that
    sort before the current cursor may be picked up by a later rebuild pass.
    """

    def __init__(self, uow: UnitOfWork):
        self._uow = uow

    async def execute(
        self,
        *,
        batch_size: int = DEFAULT_BATCH_SIZE,
        max_events: int | None = None,
    ) -> RebuildAllResult:
        if batch_size < 1:
            raise DomainValidationError("batch_size must be at least 1")

        processed = 0
        skipped = 0
        failed_event_ids: list[UUID] = []
        errors: list[str] = []
        last_seen_id: UUID | None = None

        while True:
            attempted = processed + skipped
            remaining = None if max_events is None else max_events - attempted
            if remaining is not None and remaining <= 0:
                break
            limit = min(batch_size, remaining) if remaining is not None else batch_size
            event_ids = await self._uow.events.list_ids_after_keyset(last_seen_id, limit)
            if not event_ids:
                break

            for event_id in event_ids:
                try:
                    # commit=True means each event's projection is persisted
                    # immediately in its own transaction. A failure on event N
                    # therefore cannot roll back the already-committed work for
                    # events 1..N-1. The rollback below cleans up the failed
                    # event's partial state (if any) before moving on.
                    await ReProjectEvent(self._uow).execute(event_id, commit=True)
                    processed += 1
                except Exception as exc:
                    skipped += 1
                    failed_event_ids.append(event_id)
                    errors.append(f"{event_id}: {exc}")
                    logger.exception(
                        "Failed to reproject event %s - skipping",
                        event_id,
                        extra={"event_id": str(event_id)},
                    )
                    await self._uow.rollback()

            # No batch commit needed - each event already committed individually.
            last_seen_id = event_ids[-1]
            logger.info(
                "Rebuild progress: processed=%d skipped=%d cursor=%s",
                processed,
                skipped,
                last_seen_id,
            )

        if skipped:
            logger.warning(
                "Rebuild completed with %d skipped event(s). Check result.failed_event_ids for details.",
                skipped,
            )
        return RebuildAllResult(
            processed=processed,
            skipped=skipped,
            failed_event_ids=failed_event_ids,
            errors=errors,
        )
