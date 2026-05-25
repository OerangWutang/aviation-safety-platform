"""Fake Chronos timeline and sequence-review repositories."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from atlas.domain.entities import (
    ChronosEventLink,
    ChronosSequenceReview,
    ChronosTimelineEvent,
)
from atlas.domain.enums import (
    ChronosSequenceReviewStatus,
    ChronosTimelineEventType,
)
from tests.domain.fakes._store import (
    _ChronosStore,
)


class FakeChronosTimelineEventRepository:
    def __init__(self, store: _ChronosStore) -> None:
        self._s = store

    async def get(self, id: UUID) -> ChronosTimelineEvent | None:
        return next((event for event in self._s.timeline_events if event.id == id), None)

    async def add(self, event: ChronosTimelineEvent) -> None:
        self._s.timeline_events.append(event)

    async def upsert_event(self, event: ChronosTimelineEvent) -> tuple[ChronosTimelineEvent, bool]:
        existing = await self.find_existing(
            event.accident_event_id, event.event_type, event.raw_value
        )
        if existing is not None:
            return existing, False
        self._s.timeline_events.append(event)
        return event, True

    async def list_for_accident_event(self, accident_event_id: UUID) -> list[ChronosTimelineEvent]:
        return [
            event
            for event in self._s.timeline_events
            if event.accident_event_id == accident_event_id
        ]

    async def find_existing(
        self, accident_event_id: UUID, event_type: ChronosTimelineEventType, raw_value: str | None
    ) -> ChronosTimelineEvent | None:
        return next(
            (
                event
                for event in self._s.timeline_events
                if event.accident_event_id == accident_event_id
                and event.event_type == event_type
                and event.raw_value == raw_value
            ),
            None,
        )


class FakeChronosEventLinkRepository:
    def __init__(self, store: _ChronosStore) -> None:
        self._s = store

    async def add(self, link: ChronosEventLink) -> None:
        self._s.event_links.append(link)

    async def upsert_link(self, link: ChronosEventLink) -> tuple[ChronosEventLink, bool]:
        existing = next(
            (
                existing_link
                for existing_link in self._s.event_links
                if existing_link.accident_event_id == link.accident_event_id
                and existing_link.predecessor_event_id == link.predecessor_event_id
                and existing_link.successor_event_id == link.successor_event_id
                and existing_link.relationship_type == link.relationship_type
            ),
            None,
        )
        if existing is not None:
            return existing, False
        self._s.event_links.append(link)
        return link, True

    async def list_for_accident_event(self, accident_event_id: UUID) -> list[ChronosEventLink]:
        return [link for link in self._s.event_links if link.accident_event_id == accident_event_id]


class FakeChronosSequenceReviewRepository:
    def __init__(self, store: _ChronosStore) -> None:
        self._s = store

    async def add(self, review: ChronosSequenceReview) -> None:
        for existing in self._s.sequence_reviews:
            same_pair = {existing.timeline_event_id_a, existing.timeline_event_id_b} == {
                review.timeline_event_id_a,
                review.timeline_event_id_b,
            }
            if (
                existing.accident_event_id == review.accident_event_id
                and existing.status == ChronosSequenceReviewStatus.PENDING
                and same_pair
            ):
                return
        self._s.sequence_reviews.append(review)

    async def list_pending(self, limit: int = 50, offset: int = 0) -> list[ChronosSequenceReview]:
        pending = [
            review
            for review in self._s.sequence_reviews
            if review.status == ChronosSequenceReviewStatus.PENDING
        ]
        return pending[offset : offset + limit]

    async def mark_confirmed(
        self, review_id: UUID, resolved_by: UUID, note: str | None = None
    ) -> None:
        for review in self._s.sequence_reviews:
            if review.id == review_id:
                review.status = ChronosSequenceReviewStatus.CONFIRMED
                review.resolved_at = datetime.now(UTC)
                review.resolved_by = resolved_by
                review.resolution_note = note
                break

    async def mark_rejected(
        self, review_id: UUID, resolved_by: UUID, note: str | None = None
    ) -> None:
        for review in self._s.sequence_reviews:
            if review.id == review_id:
                review.status = ChronosSequenceReviewStatus.REJECTED
                review.resolved_at = datetime.now(UTC)
                review.resolved_by = resolved_by
                review.resolution_note = note
                break
