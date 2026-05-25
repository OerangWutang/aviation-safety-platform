"""SourceRecordContinuityService - resolve prior event for a stable source_record_id."""

from __future__ import annotations

import logging
from uuid import UUID

from atlas.application.unit_of_work import UnitOfWork
from atlas.domain.entities import AccidentEvent
from atlas.domain.exceptions import (
    EventAlreadyMergedError,
    EventNotFoundError,
    SourceRecordEventMismatchError,
)

logger = logging.getLogger(__name__)


class SourceRecordContinuityService:
    """Resolve the canonical prior event for an incoming source_record_id correction.

    A stable ``source_record_id`` is authoritative for continuity within a
    source.  When the same (source, record_id) pair has been ingested before,
    the new data is a *correction* of the previous version and must be attached
    to the same event - superseding the old claims rather than creating a new
    event.

    The service acquires a transaction-scoped advisory lock before reading the
    prior snapshot.  This serialises concurrent corrections for the same record
    and prevents the race where two writers both read the same prior state,
    both insert new claims, and both supersede the old ones.

    Returns
    -------
    AccidentEvent | None
        The canonical prior event if one exists, otherwise ``None`` (first
        ingestion of this source_record_id).

    Raises
    ------
    SourceRecordEventMismatchError
        If the caller supplied an explicit ``event_id`` that disagrees with the
        canonical owner of the source record.
    """

    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    async def resolve(
        self,
        source_id: UUID,
        source_record_id: str,
        explicit_event: AccidentEvent | None,
    ) -> AccidentEvent | None:
        """Return the canonical prior event, or ``None`` if none exists."""
        # Serialise concurrent corrections before reading the prior state.
        await self._uow.snapshots.lock_for_source_record_correction(source_id, source_record_id)
        prior_event_id = await self._uow.snapshots.find_latest_event_id_by_source_record_id(
            source_id, source_record_id
        )
        if prior_event_id is None:
            return None

        prior_event = await self._canonical_event_for(prior_event_id)
        if explicit_event is not None and explicit_event.id != prior_event.id:
            raise SourceRecordEventMismatchError(
                f"source_record_id {source_record_id!r} for source {source_id} "
                f"already belongs to canonical event {prior_event.id}; "
                f"cannot ingest it into event {explicit_event.id}."
            )
        logger.info(
            "source_record_id %r re-ingestion: attaching to canonical event %s",
            source_record_id,
            prior_event.id,
        )
        return prior_event

    async def _canonical_event_for(self, event_id: UUID) -> AccidentEvent:
        seen: set[UUID] = set()
        current_id = event_id
        while True:
            event = await self._uow.events.get(current_id)
            if event is None:
                raise EventNotFoundError(f"Event {current_id} not found")
            if not event.is_merged:
                return event
            if event.merged_into_event_id is None or event.id in seen:
                raise EventAlreadyMergedError(
                    f"Event {event.id} is merged but has no valid canonical target"
                )
            seen.add(event.id)
            current_id = event.merged_into_event_id
