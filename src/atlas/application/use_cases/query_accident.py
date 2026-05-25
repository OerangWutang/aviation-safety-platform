import logging
from uuid import UUID

from atlas.application.unit_of_work import UnitOfWork
from atlas.domain.entities import ProjectedAccidentRecord

logger = logging.getLogger(__name__)


class QueryAccidentPublicView:
    def __init__(self, uow: UnitOfWork):
        self._uow = uow

    async def execute(self, event_id: UUID) -> ProjectedAccidentRecord | None:
        """Return the public projection for the canonical event.

        Merge supersedes all active claims on the absorbed event, but an older
        projection row for that absorbed id may still exist until cleanup.  Read
        paths should therefore resolve merge redirects before touching the
        projection table; otherwise the API can expose a stale non-canonical
        accident record after claims have already been moved.

        If the event row does not exist, keep the previous behavior and look up
        the projection directly.  That preserves compatibility for legacy or
        test data where projections may exist without an event row.
        """
        canonical_id = await self._canonical_event_id(event_id)
        if canonical_id is None:
            return None
        return await self._uow.projections.get(canonical_id)

    async def _canonical_event_id(self, event_id: UUID) -> UUID | None:
        seen: set[UUID] = set()
        current_id = event_id
        while True:
            event = await self._uow.events.get(current_id)
            if event is None:
                return current_id
            if not event.is_merged or event.merged_into_event_id is None:
                return event.id
            if event.id in seen:
                # Invalid merge cycle: fail closed by hiding the stale projection
                # instead of returning a non-canonical read model.
                logger.error(
                    "Merge cycle detected while canonicalizing event_id=%s at current=%s",
                    event_id,
                    event.id,
                )
                return None
            seen.add(event.id)
            current_id = event.merged_into_event_id
