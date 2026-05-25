from typing import Any
from uuid import UUID

from atlas.application.unit_of_work import UnitOfWork
from atlas.domain.exceptions import ConflictNotFoundError

MAX_CONFLICT_HISTORY_PAGE_SIZE = 500
DEFAULT_CONFLICT_HISTORY_PAGE_SIZE = 50


def _bounded_limit(limit: int) -> int:
    return max(1, min(limit, MAX_CONFLICT_HISTORY_PAGE_SIZE))


class QueryConflictHistory:
    def __init__(self, uow: UnitOfWork):
        self._uow = uow

    async def execute(
        self,
        conflict_id: UUID,
        include_archive: bool = False,
        *,
        limit: int = DEFAULT_CONFLICT_HISTORY_PAGE_SIZE,
        cursor: UUID | None = None,
    ) -> dict[str, Any]:
        if include_archive:
            raise NotImplementedError("Archive retrieval is not supported yet")

        conflict = await self._uow.conflicts.get(conflict_id)
        if conflict is None:
            raise ConflictNotFoundError(f"Conflict {conflict_id} not found")

        page_limit = _bounded_limit(limit)
        rows = await self._uow.conflict_activity.find_by_conflict(
            conflict_id,
            limit=page_limit + 1,
            after_id=cursor,
        )
        page = rows[:page_limit]
        next_cursor = page[-1].id if len(rows) > page_limit and page else None
        return {
            "conflict_id": conflict_id,
            "archive_available": False,
            "transitions": [entry.model_dump() for entry in page],
            "pagination": {"limit": page_limit, "next_cursor": next_cursor},
        }
