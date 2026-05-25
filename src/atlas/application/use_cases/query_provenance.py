"""QueryProvenance: return bounded evidence trail for an accident event.

Canonicalization policy
-----------------------
When ``canonicalize=True`` (the default) and the requested event has been
absorbed by a merge, the query transparently follows the chain to the surviving
(canonical) event and returns its provenance.  The response includes an
``absorbed_event_id`` field so callers know the redirect happened.

When ``canonicalize=False``, the query returns the absorbed event's own
provenance - useful for audit / debugging when you need to see exactly what
evidence was attached to the source event before it was merged.

Pagination policy
-----------------
Provenance can contain years of claim history, conflict activity, and projection
snapshots.  All high-cardinality collections are therefore keyset-paginated.
The legacy arrays are still present, but each contains at most ``limit`` rows.
Use ``pagination.next_cursors`` to request the next page for a specific stream.
"""

from collections.abc import Sequence
from typing import Any, Protocol, TypeVar
from uuid import UUID

from atlas.application.unit_of_work import UnitOfWork
from atlas.domain.exceptions import EventAlreadyMergedError, EventNotFoundError


class _HasUuidId(Protocol):
    """Protocol for provenance items used as keyset-cursor anchors.

    All repository rows surfaced through ``_trim_page`` already expose ``id``
    as a ``UUID``; encoding that here lets strict-mode mypy verify it without
    falling back to ``getattr``.
    """

    @property
    def id(self) -> UUID: ...


T = TypeVar("T", bound=_HasUuidId)

DEFAULT_PROVENANCE_LIMIT = 50
MAX_PROVENANCE_LIMIT = 200


class QueryProvenance:
    def __init__(self, uow: UnitOfWork):
        self._uow = uow

    async def execute(
        self,
        event_id: UUID,
        include_archive: bool = False,
        canonicalize: bool = True,
        *,
        limit: int = DEFAULT_PROVENANCE_LIMIT,
        cursor: UUID | None = None,
        claims_cursor: UUID | None = None,
        claim_history_cursor: UUID | None = None,
        conflicts_cursor: UUID | None = None,
        conflict_activity_cursor: UUID | None = None,
        projection_history_cursor: UUID | None = None,
    ) -> dict[str, Any]:
        if include_archive:
            raise NotImplementedError("Archive retrieval is not supported yet")

        page_limit = max(1, min(limit, MAX_PROVENANCE_LIMIT))
        fetch_limit = page_limit + 1
        # Backward-friendly shorthand for the noisiest provenance stream.
        claim_history_cursor = claim_history_cursor or cursor

        # Resolve to the canonical (surviving) event when requested. Always
        # verify the originally requested event exists first; otherwise a
        # random UUID could look like a valid but empty provenance record.
        requested_event = await self._uow.events.get(event_id)
        if requested_event is None:
            raise EventNotFoundError(f"Event {event_id} not found")

        absorbed_event_id: UUID | None = None
        canonical_event_id = event_id

        if canonicalize:
            seen: set[UUID] = set()
            current_event = requested_event
            for _ in range(16):
                if not current_event.is_merged:
                    canonical_event_id = current_event.id
                    break
                if current_event.merged_into_event_id is None or current_event.id in seen:
                    raise EventAlreadyMergedError(
                        f"Event {current_event.id} is merged but has no valid canonical target"
                    )
                absorbed_event_id = event_id
                seen.add(current_event.id)
                next_event = await self._uow.events.get(current_event.merged_into_event_id)
                if next_event is None:
                    raise EventAlreadyMergedError(
                        f"Event {current_event.id} points to missing canonical target "
                        f"{current_event.merged_into_event_id}"
                    )
                current_event = next_event
            else:
                raise EventAlreadyMergedError(
                    f"Merge chain for event {event_id} exceeds the 16-hop safety limit"
                )

        claims_page = await self._uow.claims.find_all_by_event(
            canonical_event_id,
            limit=fetch_limit,
            after_id=claims_cursor,
        )
        histories_page = await self._uow.claim_history.find_by_event(
            canonical_event_id,
            limit=fetch_limit,
            after_id=claim_history_cursor,
        )
        conflicts_page = await self._uow.conflicts.find_by_event(
            canonical_event_id,
            limit=fetch_limit,
            after_id=conflicts_cursor,
        )
        projection = await self._uow.projections.get(canonical_event_id)
        projection_history_page = await self._uow.projection_history.find_by_event(
            canonical_event_id,
            limit=fetch_limit,
            after_id=projection_history_cursor,
        )
        conflict_logs_page = await self._uow.conflict_activity.find_by_event(
            canonical_event_id,
            limit=fetch_limit,
            after_id=conflict_activity_cursor,
        )

        claims, next_claims_cursor = _trim_page(claims_page, page_limit)
        histories, next_claim_history_cursor = _trim_page(histories_page, page_limit)
        conflicts, next_conflicts_cursor = _trim_page(conflicts_page, page_limit)
        projection_history, next_projection_history_cursor = _trim_page(
            projection_history_page,
            page_limit,
        )
        conflict_logs, next_conflict_activity_cursor = _trim_page(
            conflict_logs_page,
            page_limit,
        )

        sources = await self._uow.sources.get_by_ids(list({claim.source_id for claim in claims}))
        sources_by_id = {source.id: source for source in sources}

        def _source_name(source_id: UUID) -> str | None:
            source = sources_by_id.get(source_id)
            return source.name if source is not None else None

        next_cursors = {
            "claims": next_claims_cursor,
            "claim_histories": next_claim_history_cursor,
            "conflicts": next_conflicts_cursor,
            "conflict_activity_logs": next_conflict_activity_cursor,
            "projection_history": next_projection_history_cursor,
        }

        return {
            # The event_id in the response is always the canonical (surviving) id.
            "event_id": canonical_event_id,
            # Non-None only when the caller asked about an absorbed event and
            # canonicalize=True.  Front-ends can use this to show a redirect notice.
            "absorbed_event_id": absorbed_event_id,
            "canonicalized": absorbed_event_id is not None,
            "projection": projection.model_dump() if projection else None,
            "claims": [
                {
                    **claim.model_dump(),
                    "source_name": _source_name(claim.source_id),
                }
                for claim in claims
            ],
            "claim_histories": [history.model_dump() for history in histories],
            "conflicts": [conflict.model_dump() for conflict in conflicts],
            "conflict_activity_logs": [log.model_dump() for log in conflict_logs],
            "projection_history": [entry.model_dump() for entry in projection_history],
            "pagination": {
                "limit": page_limit,
                # Convenience alias for clients using ?cursor=... against the
                # dominant historical stream.
                "next_cursor": next_claim_history_cursor,
                "next_cursors": next_cursors,
                "has_more": any(value is not None for value in next_cursors.values()),
            },
            "archive_available": False,
        }


def _trim_page(items: Sequence[T], limit: int) -> tuple[list[T], UUID | None]:
    page = list(items[:limit])
    if len(items) <= limit:
        return page, None
    last = page[-1]
    return page, last.id
