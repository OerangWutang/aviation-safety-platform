"""QueryOperationalMetrics: aggregate health counters for the admin dashboard.

This use case keeps the metrics route from importing SQLAlchemy models and ORM
internals directly - the admin router should not know table names.  All counts
are produced by repository methods that stay behind the domain boundary.

The repository additions are intentionally minimal: each new method is a single
aggregate SQL query (COUNT with optional WHERE) so the call is fast even on
large datasets.
"""

from __future__ import annotations

from dataclasses import dataclass

from atlas.application.unit_of_work import UnitOfWork
from atlas.domain.enums import ConflictStatus, OutboxStatus


@dataclass(frozen=True)
class OperationalMetrics:
    outbox_pending: int
    outbox_processing: int
    outbox_failed: int
    outbox_dead_letter: int
    outbox_processed: int
    outbox_oldest_unprocessed_age_seconds: float | None
    worker_heartbeat_age_seconds: float | None
    worker_successful_batch_age_seconds: float | None
    conflicts_open: int
    conflicts_resolved: int
    total_claims: int
    total_projected_events: int

    def as_dict(self) -> dict[str, object]:
        return {
            "outbox": {
                "pending": self.outbox_pending,
                "processing": self.outbox_processing,
                "failed": self.outbox_failed,
                "dead_letter": self.outbox_dead_letter,
                "processed": self.outbox_processed,
                "oldest_unprocessed_age_seconds": self.outbox_oldest_unprocessed_age_seconds,
                "worker_heartbeat_age_seconds": self.worker_heartbeat_age_seconds,
                "worker_successful_batch_age_seconds": self.worker_successful_batch_age_seconds,
            },
            "conflicts": {
                "open": self.conflicts_open,
                "resolved": self.conflicts_resolved,
            },
            "claims": {"total": self.total_claims},
            "events": {"total_projected": self.total_projected_events},
        }


class QueryOperationalMetrics:
    """Read aggregate operational counters from the UoW repositories.

    All individual counts are fetched via repository methods - no raw SQL,
    no ORM model imports, no infrastructure leaking into the router.
    """

    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    async def execute(self, *, include_expensive_totals: bool = True) -> OperationalMetrics:
        """Return operational counters.

        ``include_expensive_totals=False`` keeps Prometheus scrapes focused on
        hot operational signals and avoids exact historical ``COUNT(*)`` calls
        over append-only tables.  Admin JSON metrics still use the default and
        return the full exact snapshot on demand.
        """
        outbox_processed = 0
        conflicts_resolved = 0
        total_claims = 0
        total_projected_events = 0
        if include_expensive_totals:
            outbox_processed = await self._uow.outbox.count_by_status(OutboxStatus.PROCESSED)
            conflicts_resolved = await self._uow.conflicts.count_by_status(ConflictStatus.RESOLVED)
            total_claims = await self._uow.claims.count_total()
            total_projected_events = await self._uow.projections.count_total()

        return OperationalMetrics(
            outbox_pending=await self._uow.outbox.count_by_status(OutboxStatus.PENDING),
            outbox_processing=await self._uow.outbox.count_by_status(OutboxStatus.PROCESSING),
            outbox_failed=await self._uow.outbox.count_by_status(OutboxStatus.FAILED),
            outbox_dead_letter=await self._uow.outbox.count_by_status(OutboxStatus.DEAD_LETTER),
            outbox_processed=outbox_processed,
            outbox_oldest_unprocessed_age_seconds=(
                await self._uow.outbox.oldest_unprocessed_age_seconds()
            ),
            worker_heartbeat_age_seconds=await self._uow.outbox.worker_heartbeat_age_seconds(),
            worker_successful_batch_age_seconds=(
                await self._uow.outbox.worker_successful_batch_age_seconds()
            ),
            conflicts_open=await self._uow.conflicts.count_by_status(ConflictStatus.OPEN),
            conflicts_resolved=conflicts_resolved,
            total_claims=total_claims,
            total_projected_events=total_projected_events,
        )
