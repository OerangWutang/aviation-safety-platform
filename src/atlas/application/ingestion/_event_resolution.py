"""EventResolutionService - identity-index match -> attach / review / new event."""

from __future__ import annotations

import logging
import re
from typing import Any
from uuid import UUID, uuid4

from atlas.application.ingestion._identity_index_updater import _build_identity_entry
from atlas.application.unit_of_work import UnitOfWork
from atlas.domain.entities import AccidentEvent, PendingDuplicateReview
from atlas.domain.enums import DuplicateReviewStatus
from atlas.domain.exceptions import (
    DomainValidationError,
    EventAlreadyMergedError,
    EventNotFoundError,
)
from atlas.domain.services.event_matching import EventMatcher, _norm, _norm_date

logger = logging.getLogger(__name__)


class EventResolutionService:
    """Resolve or create an ``AccidentEvent`` for anonymous incoming claims.

    Why identity index, not projected_accident_records
    --------------------------------------------------
    ``projected_accident_records`` is populated asynchronously by the outbox
    worker.  The ``event_identity_index`` is written in the same transaction as
    ingestion, so it is always up-to-date for the very next ingestion that
    follows.  Querying it eliminates the projection-lag window entirely.

    For the fully concurrent case, ``lock_for_identity_resolution`` acquires a
    transaction-scoped advisory lock keyed on ``(event_date_norm,
    registration_norm)``.  The second transaction blocks at the lock call, then
    finds the index entry already written by the first.

    Returns
    -------
    (event, pending_reviews, event_created, attached_by)
    """

    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    async def resolve(
        self,
        source_id: UUID,
        claims_data_fields: dict[str, Any],
        ingestion_run_id: UUID,
        source_record_id: str | None = None,
    ) -> tuple[AccidentEvent, PendingDuplicateReview | None, bool, str]:
        """Backward-compatible resolver returning only the primary review id."""
        event, reviews, created, attached_by = await self.resolve_with_reviews(
            source_id=source_id,
            claims_data_fields=claims_data_fields,
            ingestion_run_id=ingestion_run_id,
            source_record_id=source_record_id,
        )
        return event, reviews[0] if reviews else None, created, attached_by

    async def resolve_with_reviews(
        self,
        source_id: UUID,
        claims_data_fields: dict[str, Any],
        ingestion_run_id: UUID,
        source_record_id: str | None = None,
        *,
        max_duplicate_reviews: int = 10,
    ) -> tuple[AccidentEvent, list[PendingDuplicateReview], bool, str]:
        if max_duplicate_reviews <= 0:
            raise DomainValidationError("max_duplicate_reviews must be greater than zero")

        event_date_raw = claims_data_fields.get("event_date")
        if event_date_raw is None:
            # No event_date: cannot match against any index entry; create a new event.
            event = AccidentEvent(id=uuid4())
            await self._uow.events.add(event)
            await self._uow.identity_index.upsert(
                _build_identity_entry(event.id, claims_data_fields, source_record_id)
            )
            return event, [], True, "new_event"

        event_date_norm = _norm_date(str(event_date_raw))
        registration_raw = claims_data_fields.get("registration")
        registration_norm = (
            re.sub(r"[-/\s]", "", _norm(registration_raw)) if registration_raw else None
        )

        # Serialise concurrent ingestions with the same identity key.
        await self._uow.identity_index.lock_for_identity_resolution(
            event_date_norm, registration_norm
        )

        # Query the synchronous identity index.
        candidates = await self._uow.identity_index.find_candidates(
            event_date_norm=event_date_norm, limit=50
        )

        # Also query by registration directly to bypass the 50-row date cap.
        if registration_norm:
            reg_candidates = await self._uow.identity_index.find_by_registration(
                registration_norm=registration_norm,
                event_date_norm=event_date_norm,
            )
            seen_ids = {c.event_id for c in candidates}
            for rc in reg_candidates:
                if rc.event_id not in seen_ids:
                    candidates.append(rc)
                    seen_ids.add(rc.event_id)

        decision = EventMatcher().decide(claims_data_fields, candidates)

        if decision.action == "attach":
            existing = await self._canonical_event_for(decision.candidate_event_id)
            if existing is not None:
                logger.info(
                    "High-confidence identity match (score=%.2f, fields=%s): "
                    "attaching to canonical event %s",
                    decision.score,
                    decision.matched_fields,
                    existing.id,
                )
                if decision.candidate_event_id == existing.id:
                    await self._uow.identity_index.upsert(
                        _build_identity_entry(existing.id, claims_data_fields, source_record_id)
                    )
                else:
                    await self._uow.identity_index.enrich_identity_index_from_alias(
                        _build_identity_entry(existing.id, claims_data_fields, source_record_id)
                    )
                    logger.info(
                        "Identity match used merged-event alias %s -> canonical %s; "
                        "enriched canonical identity row without overwriting scalars",
                        decision.candidate_event_id,
                        existing.id,
                    )
                return existing, [], False, "identity_match"
            # Candidate merged/gone - fall through to create a new event.

        event = AccidentEvent(id=uuid4())
        await self._uow.events.add(event)
        await self._uow.identity_index.upsert(
            _build_identity_entry(event.id, claims_data_fields, source_record_id)
        )

        candidate_event_ids = (
            decision.tied_candidate_event_ids
            if decision.tied_candidate_event_ids
            else ([decision.candidate_event_id] if decision.candidate_event_id is not None else [])
        )
        if decision.action == "review" and candidate_event_ids:
            canonical_candidate_ids: list[UUID] = []
            truncated_candidate_ids: list[UUID] = []
            for raw_candidate_event_id in candidate_event_ids:
                try:
                    canonical = await self._canonical_event_for(raw_candidate_event_id)
                    candidate_event_id = (
                        canonical.id if canonical is not None else raw_candidate_event_id
                    )
                except EventNotFoundError:
                    candidate_event_id = raw_candidate_event_id
                if candidate_event_id == event.id or candidate_event_id in canonical_candidate_ids:
                    continue
                if len(canonical_candidate_ids) >= max_duplicate_reviews:
                    truncated_candidate_ids.append(candidate_event_id)
                    continue
                canonical_candidate_ids.append(candidate_event_id)

            reviews_for_response: list[PendingDuplicateReview] = []
            queued_review_ids: list[UUID] = []
            for candidate_event_id in canonical_candidate_ids:
                existing_review = await self._uow.duplicate_reviews.find_existing_pair(
                    candidate_event_id, event.id
                )
                if existing_review is not None:
                    reviews_for_response.append(existing_review)
                    continue

                review = PendingDuplicateReview(
                    id=uuid4(),
                    event_id_a=candidate_event_id,
                    event_id_b=event.id,
                    status=DuplicateReviewStatus.PENDING,
                    match_score=decision.score,
                    matched_fields=decision.matched_fields,
                )
                stored_review = await self._uow.duplicate_reviews.add(review)
                stored_or_new = stored_review or review
                reviews_for_response.append(stored_or_new)
                queued_review_ids.append(stored_or_new.id)

            if reviews_for_response:
                logger.info(
                    "Uncertain identity match (score=%.2f, fields=%s): queued/kept "
                    "duplicate reviews for candidate events %s against new event %s",
                    decision.score,
                    decision.matched_fields,
                    [str(candidate_id) for candidate_id in canonical_candidate_ids],
                    event.id,
                    extra={
                        "review_id": str(reviews_for_response[0].id),
                        "review_ids": [str(review.id) for review in reviews_for_response],
                        "queued_review_ids": [str(review_id) for review_id in queued_review_ids],
                        "candidate_event_ids": [
                            str(candidate_id) for candidate_id in canonical_candidate_ids
                        ],
                        "truncated_candidate_event_ids": [
                            str(candidate_id) for candidate_id in truncated_candidate_ids
                        ],
                        "new_event_id": str(event.id),
                        "match_score": decision.score,
                    },
                )
                if truncated_candidate_ids:
                    logger.warning(
                        "Duplicate-review fan-out capped at %s for new event %s; "
                        "truncated candidate events %s",
                        max_duplicate_reviews,
                        event.id,
                        [str(candidate_id) for candidate_id in truncated_candidate_ids],
                    )
                return event, reviews_for_response, True, "duplicate_review"

        return event, [], True, "new_event"

    async def _canonical_event_for(self, event_id: UUID) -> AccidentEvent | None:
        seen: set[UUID] = set()
        current_id = event_id
        while True:
            event = await self._uow.events.get(current_id)
            if event is None:
                return None
            if not event.is_merged:
                return event
            if event.merged_into_event_id is None or event.id in seen:
                raise EventAlreadyMergedError(
                    f"Event {event.id} is merged but has no valid canonical target"
                )
            seen.add(event.id)
            current_id = event.merged_into_event_id
