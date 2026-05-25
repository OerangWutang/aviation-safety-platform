"""ReviewArgusSignal — record a review decision on an Argus signal.

Concurrency model
-----------------
Two reviewers may load the same signal in their UI and click confirm/dismiss
within seconds of each other.  Without optimistic concurrency, the later
write silently overrides the earlier one and the activity log records two
contradictory decisions on the same row.

To prevent this we require the reviewer to pass ``expected_version`` — the
``ArgusSignal.version`` they saw when they loaded the signal.  The use case:

1. Loads the current signal; raises ``NotFoundError`` if it doesn't exist.
2. Pre-checks ``expected_version`` and raises ``ArgusSignalModifiedError``
   with the current state if it's stale.  This is the *informative* path —
   the response includes the up-to-date signal so the client can re-render.
3. Inserts an immutable ``ArgusSignalReview`` audit row.
4. Calls ``update_with_version_check`` which performs the atomic
   ``UPDATE … WHERE id = ? AND version = ?``.  If a concurrent reviewer
   raced and won between (2) and (4), the SQL returns zero rows and we
   raise ``ArgusSignalModifiedError`` again.

The audit ``ArgusSignalReview`` row is inserted even when the
``update_with_version_check`` race-loses, because under SQLAlchemy's session
semantics the whole transaction will be rolled back by the use case's
``rollback`` call — leaving the database consistent.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from atlas.application.unit_of_work import UnitOfWork
from atlas.domain.entities import ArgusSignal, ArgusSignalReview
from atlas.domain.enums import ArgusReviewDecision, ArgusSignalStatus
from atlas.domain.exceptions import (
    ArgusSignalModifiedError,
    ArgusSignalNotFoundError,
)


@dataclass
class ReviewArgusSignalInput:
    signal_id: UUID
    decision: ArgusReviewDecision
    expected_version: int
    reviewer_id: UUID | None = None
    note: str | None = None


_DECISION_TO_STATUS: dict[ArgusReviewDecision, ArgusSignalStatus] = {
    ArgusReviewDecision.CONFIRMED: ArgusSignalStatus.CONFIRMED,
    ArgusReviewDecision.DISMISSED: ArgusSignalStatus.DISMISSED,
    ArgusReviewDecision.NEEDS_MORE_REVIEW: ArgusSignalStatus.NEEDS_MORE_REVIEW,
}


class ReviewArgusSignal:
    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    async def execute(self, input: ReviewArgusSignalInput) -> ArgusSignal:
        signal = await self._uow.argus_signals.get(input.signal_id)
        if signal is None:
            raise ArgusSignalNotFoundError(f"ArgusSignal {input.signal_id} not found")

        # Informative pre-check so the 409 response carries the current
        # signal state.  A concurrent racer can still slip in between this
        # check and the update below — the SQL ``WHERE version = ?`` is the
        # actual authority.
        if signal.version != input.expected_version:
            raise ArgusSignalModifiedError(
                signal_id=signal.id,
                current_version=signal.version,
                current_signal=signal.model_dump(mode="json"),
            )

        review = ArgusSignalReview(
            signal_id=signal.id,
            decision=input.decision,
            reviewer_id=input.reviewer_id,
            note=input.note,
        )
        await self._uow.argus_signal_reviews.add(review)

        new_status = _DECISION_TO_STATUS[input.decision]
        updated = await self._uow.argus_signals.update_with_version_check(
            signal_id=signal.id,
            expected_version=input.expected_version,
            updates={"status": new_status.value},
        )
        if updated is None:
            # Lost the race after the pre-check passed.  Re-read the latest
            # state so the client gets a useful payload, then roll back the
            # tentative review insert.
            latest = await self._uow.argus_signals.get(signal.id)
            await self._uow.rollback()
            raise ArgusSignalModifiedError(
                signal_id=signal.id,
                current_version=latest.version if latest is not None else signal.version + 1,
                current_signal=latest.model_dump(mode="json") if latest is not None else None,
            )

        await self._uow.commit()
        return updated
