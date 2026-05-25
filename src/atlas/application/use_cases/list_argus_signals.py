"""ListArgusSignals — keyset pagination over the Argus signal table.

The legacy ``ArgusSignalRepository.list`` uses offset pagination and remains
in place for backward compatibility.  This use case wraps the newer
``list_page`` method that walks the composite index
``ix_argus_signals_last_detected_id_desc`` (migration 032), avoiding the
silent skip/duplicate hazards of offset pagination on a non-unique sort key.

API shape mirrors ``ListPendingDuplicateReviews.execute_page``: fetch
``limit + 1``, slice to ``limit``, expose the last item's ``id`` as the
next cursor.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from atlas.application.unit_of_work import UnitOfWork
from atlas.domain.entities import ArgusSignal
from atlas.domain.enums import ArgusSeverity, ArgusSignalStatus, ArgusSignalType
from atlas.domain.exceptions import DomainValidationError

MAX_ARGUS_SIGNALS_PAGE_SIZE = 500
DEFAULT_ARGUS_SIGNALS_PAGE_SIZE = 50


@dataclass(frozen=True)
class ArgusSignalsPage:
    items: list[ArgusSignal]
    limit: int
    next_cursor: UUID | None


def _bounded_limit(limit: int) -> int:
    return max(1, min(limit, MAX_ARGUS_SIGNALS_PAGE_SIZE))


class ListArgusSignals:
    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    async def execute_page(
        self,
        *,
        status: ArgusSignalStatus | None = None,
        signal_type: ArgusSignalType | None = None,
        severity: ArgusSeverity | None = None,
        limit: int = DEFAULT_ARGUS_SIGNALS_PAGE_SIZE,
        cursor: UUID | None = None,
    ) -> ArgusSignalsPage:
        if limit < 1:
            # Mirror the validation style of the other paginated use cases —
            # bare-bones ``DomainValidationError`` maps to a 400 via the
            # global handler.  The router also caps ``limit`` via FastAPI
            # ``Query(ge=1, le=500)``, so this is defence in depth.
            raise DomainValidationError(f"limit must be >= 1, got {limit}")
        page_limit = _bounded_limit(limit)
        # Fetch one extra row so we know whether a next page exists without
        # a second COUNT(*) query.  If the result length exceeds page_limit,
        # the last *returned* item's id is the cursor for the next call.
        rows = await self._uow.argus_signals.list_page(
            status=status,
            signal_type=signal_type,
            severity=severity,
            limit=page_limit + 1,
            after_id=cursor,
        )
        items = rows[:page_limit]
        next_cursor = items[-1].id if len(rows) > page_limit and items else None
        return ArgusSignalsPage(items=items, limit=page_limit, next_cursor=next_cursor)
