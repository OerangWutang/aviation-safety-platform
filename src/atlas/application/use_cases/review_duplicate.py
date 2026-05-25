"""ReviewDuplicate: curator accepts or rejects a PendingDuplicateReview."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from uuid import UUID

from atlas.application.unit_of_work import UnitOfWork
from atlas.domain.enums import DuplicateReviewStatus
from atlas.domain.exceptions import (
    DomainValidationError,
    ReviewAlreadyResolvedError,
    ReviewNotFoundError,
)

if TYPE_CHECKING:
    from atlas.application.use_cases.merge_duplicate_events import MergeResult

logger = logging.getLogger(__name__)


class ReviewDuplicate:
    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    async def execute(
        self,
        review_id: UUID,
        action: str,
        resolved_by: UUID,
        note: str = "",
        source_event_id: UUID | None = None,
    ) -> MergeResult | None:
        """Accept or reject a pending duplicate-event review.

        Parameters
        ----------
        review_id:
            The ``PendingDuplicateReview`` to action.
        action:
            ``'CONFIRM'`` triggers a merge; ``'REJECT'`` marks the pair as
            distinct accidents.
        resolved_by:
            User ID of the curator performing the action.
        note:
            Optional free-text note recorded on the merge / resolution.
        source_event_id:
            Controls *which* event is absorbed when confirming.  Must be
            either ``review.event_id_a`` or ``review.event_id_b``.  The
            *other* event becomes the surviving target.

            If omitted, the default is ``event_id_b`` (the newer/challenger
            event is absorbed into ``event_id_a``), which matches the
            original queuing convention - ``event_id_a`` is the pre-existing
            event, ``event_id_b`` is the newcomer.

            For ``REJECT`` actions this parameter is ignored.

        Returns
        -------
        MergeResult | None
            The merge result when action is ``'CONFIRM'``, ``None`` when
            action is ``'REJECT'``.
        """
        review = await self._uow.duplicate_reviews.get(review_id)
        if review is None:
            raise ReviewNotFoundError(f"Duplicate review {review_id} not found")
        if review.status != DuplicateReviewStatus.PENDING:
            raise ReviewAlreadyResolvedError(f"Review {review_id} is already {review.status.value}")

        if action.upper() == "CONFIRM":
            # Determine merge direction.  The caller may supply source_event_id
            # to choose which event is absorbed; if absent we default to B->A.
            if source_event_id is not None:
                if source_event_id not in (review.event_id_a, review.event_id_b):
                    raise DomainValidationError(
                        f"source_event_id {source_event_id} is not part of review "
                        f"{review_id} (events: {review.event_id_a}, {review.event_id_b})"
                    )
                resolved_source = source_event_id
                resolved_target = (
                    review.event_id_b if source_event_id == review.event_id_a else review.event_id_a
                )
            else:
                # Default: event_b (newcomer) is absorbed into event_a (pre-existing).
                resolved_source = review.event_id_b
                resolved_target = review.event_id_a

            from atlas.application.use_cases.merge_duplicate_events import MergeDuplicateEvents

            merge_result = await MergeDuplicateEvents(self._uow).execute(
                source_event_id=resolved_source,
                target_event_id=resolved_target,
                resolved_by=resolved_by,
                note=note or "Confirmed duplicate via review",
                review_id=review_id,
            )
            return merge_result

        elif action.upper() == "REJECT":
            await self._uow.duplicate_reviews.update_status(
                id=review_id,
                status=DuplicateReviewStatus.REJECTED,
                resolved_by=resolved_by,
                resolution_note=(note or "Not a duplicate")[:500],
            )
            await self._uow.commit()
            return None

        else:
            raise DomainValidationError(
                f"Invalid action {action!r}. Must be 'CONFIRM' or 'REJECT'."
            )
