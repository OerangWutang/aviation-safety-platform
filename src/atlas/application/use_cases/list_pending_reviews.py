"""ListPendingDuplicateReviews: query all PENDING duplicate-event reviews.

Keeps the presentation layer clean: no SQLAlchemy imports, no raw session access.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from atlas.application.unit_of_work import UnitOfWork
from atlas.domain.entities import PendingDuplicateReview
from atlas.domain.exceptions import DomainValidationError

MAX_PENDING_REVIEW_PAGE_SIZE = 500
DEFAULT_PENDING_REVIEW_PAGE_SIZE = 50


@dataclass(frozen=True)
class PendingDuplicateReviewPage:
    items: list[PendingDuplicateReview]
    limit: int
    next_cursor: UUID | None


def _bounded_limit(limit: int) -> int:
    return max(1, min(limit, MAX_PENDING_REVIEW_PAGE_SIZE))


class ListPendingDuplicateReviews:
    """Return PENDING duplicate-event reviews ordered newest-first.

    Parameters
    ----------
    uow:
        Active Unit of Work (read-only use; no commit needed).
    """

    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    async def execute(
        self, *, limit: int = DEFAULT_PENDING_REVIEW_PAGE_SIZE
    ) -> list[PendingDuplicateReview]:
        """Backward-compatible list helper for older tests/callers."""
        if limit < 1:
            raise DomainValidationError(f"limit must be >= 1, got {limit}")
        return await self._uow.duplicate_reviews.list_pending(limit=_bounded_limit(limit))

    async def execute_page(
        self,
        *,
        limit: int = DEFAULT_PENDING_REVIEW_PAGE_SIZE,
        cursor: UUID | None = None,
    ) -> PendingDuplicateReviewPage:
        if limit < 1:
            raise DomainValidationError(f"limit must be >= 1, got {limit}")
        page_limit = _bounded_limit(limit)
        rows = await self._uow.duplicate_reviews.list_pending(
            limit=page_limit + 1,
            after_id=cursor,
        )
        items = rows[:page_limit]
        next_cursor = items[-1].id if len(rows) > page_limit and items else None
        return PendingDuplicateReviewPage(items=items, limit=page_limit, next_cursor=next_cursor)
