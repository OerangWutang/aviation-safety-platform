"""ProjectionUpdater - queue an outbox event to trigger projection rebuild."""

from __future__ import annotations

from uuid import UUID, uuid4

from atlas.application.unit_of_work import UnitOfWork
from atlas.domain.entities import OutboxEvent


class ProjectionUpdater:
    """Queue a ``CLAIMS_UPDATED`` outbox event for the given accident event.

    The outbox worker picks this up asynchronously and rebuilds
    ``projected_accident_records`` for the event.  Queuing in the same
    transaction as claim writes ensures the outbox entry is never lost.
    """

    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    async def queue(
        self,
        event_id: UUID,
        source_id: UUID,
        ingestion_run_id: UUID,
    ) -> None:
        await self._uow.outbox.add(
            OutboxEvent(
                id=uuid4(),
                event_type="CLAIMS_UPDATED",
                aggregate_id=event_id,
                payload={
                    "event_id": str(event_id),
                    "source_id": str(source_id),
                    "ingestion_run_id": str(ingestion_run_id),
                },
            )
        )
