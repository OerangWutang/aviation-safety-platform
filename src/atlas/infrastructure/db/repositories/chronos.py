"""SQLAlchemy repositories for the chronos aggregate.

Carved out of the old ``repositories.py`` monolith in r9; behaviour
unchanged.  Public ``Sql*`` classes are re-exported from
``atlas.infrastructure.db.repositories`` so existing imports keep
working.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import ColumnElement

from atlas.domain.entities import (
    ChronosEventLink,
    ChronosSequenceReview,
    ChronosTimelineEvent,
)
from atlas.domain.enums import (
    ChronosSequenceReviewStatus,
    ChronosTimelineEventType,
    ChronosTimestampPrecision,
)
from atlas.domain.exceptions import ConcurrentUpsertError
from atlas.domain.interfaces.repositories import (
    ChronosEventLinkRepository,
    ChronosSequenceReviewRepository,
    ChronosTimelineEventRepository,
)
from atlas.infrastructure.db.orm_models import (
    ChronosEventLinkModel,
    ChronosSequenceReviewModel,
    ChronosTimelineEventModel,
)
from atlas.infrastructure.db.repositories._helpers import (
    _domain_data,
)


def _chronos_te_to_domain(m: ChronosTimelineEventModel) -> ChronosTimelineEvent:
    return ChronosTimelineEvent(
        id=m.id,
        accident_event_id=m.accident_event_id,
        event_type=ChronosTimelineEventType(m.event_type),
        occurred_at=m.occurred_at,
        timestamp_precision=ChronosTimestampPrecision(m.timestamp_precision),
        sequence_index=m.sequence_index,
        description=m.description,
        raw_value=m.raw_value,
        confidence=m.confidence,
        source_claim_id=m.source_claim_id,
        raw_snapshot_id=m.raw_snapshot_id,
        created_at=m.created_at,
        updated_at=m.updated_at,
    )


def _chronos_link_to_domain(m: ChronosEventLinkModel) -> ChronosEventLink:
    return ChronosEventLink(
        id=m.id,
        accident_event_id=m.accident_event_id,
        predecessor_event_id=m.predecessor_event_id,
        successor_event_id=m.successor_event_id,
        relationship_type=m.relationship_type,
        confidence=m.confidence,
        source_claim_id=m.source_claim_id,
        raw_snapshot_id=m.raw_snapshot_id,
        created_at=m.created_at,
    )


def _chronos_review_to_domain(m: ChronosSequenceReviewModel) -> ChronosSequenceReview:
    return ChronosSequenceReview(
        id=m.id,
        accident_event_id=m.accident_event_id,
        timeline_event_id_a=m.timeline_event_id_a,
        timeline_event_id_b=m.timeline_event_id_b,
        reason=m.reason,
        status=ChronosSequenceReviewStatus(m.status),
        created_at=m.created_at,
        resolved_at=m.resolved_at,
        resolved_by=m.resolved_by,
        resolution_note=m.resolution_note,
    )


class SqlChronosTimelineEventRepository(ChronosTimelineEventRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, id: UUID) -> ChronosTimelineEvent | None:
        row = await self._session.get(ChronosTimelineEventModel, id)
        return _chronos_te_to_domain(row) if row else None

    async def add(self, event: ChronosTimelineEvent) -> None:
        self._session.add(ChronosTimelineEventModel(**_domain_data(event)))

    async def upsert_event(self, event: ChronosTimelineEvent) -> tuple[ChronosTimelineEvent, bool]:
        """Insert a Chronos timeline event idempotently.

        The unique index ``uq_chronos_timeline_events_idempotent`` on
        ``(accident_event_id, event_type, raw_value)`` makes
        ``INSERT … ON CONFLICT DO NOTHING`` race-safe for non-NULL raw_value.

        ``raw_value`` must not be None: callers are responsible for supplying
        the raw string that uniquely identifies a timeline event.  All current
        callers (``ExtractChronosTimelineFromEvent``) guarantee this via the
        ``if raw_val is None: continue`` guard before constructing a
        ``ChronosTimelineEvent``.  Null raw_value is now an explicit
        programming error rather than a silent edge case.
        """
        if event.raw_value is None:
            raise ValueError(
                f"ChronosTimelineEvent.raw_value must not be None "
                f"(event_type={event.event_type!r}, "
                f"accident_event_id={event.accident_event_id}).  "
                "Callers must supply the raw string that uniquely identifies "
                "this timeline event before calling upsert_event."
            )

        # ON CONFLICT DO NOTHING is safe because raw_value is non-NULL here,
        # so the unique index on (accident_event_id, event_type, raw_value)
        # provides genuine deduplication.
        stmt = (
            insert(ChronosTimelineEventModel)
            .values(**_domain_data(event))
            .on_conflict_do_nothing(index_elements=["accident_event_id", "event_type", "raw_value"])
            .returning(ChronosTimelineEventModel)
        )
        result = await self._session.execute(stmt)
        row = result.scalar_one_or_none()
        if row is not None:
            return _chronos_te_to_domain(row), True
        # DO NOTHING branch — re-select the pre-existing row.
        existing = await self.find_existing(
            event.accident_event_id, event.event_type, event.raw_value
        )
        if existing is not None:
            return existing, False
        raise ConcurrentUpsertError(
            f"ChronosTimelineEvent upsert: ON CONFLICT fired for "
            f"(accident_event_id={event.accident_event_id}, event_type={event.event_type!r}, "
            f"raw_value={event.raw_value!r}) but re-select found no existing row."
        )

    async def list_for_accident_event(self, accident_event_id: UUID) -> list[ChronosTimelineEvent]:
        result = await self._session.execute(
            select(ChronosTimelineEventModel).where(
                ChronosTimelineEventModel.accident_event_id == accident_event_id
            )
        )
        return [_chronos_te_to_domain(row) for row in result.scalars().all()]

    async def find_existing(
        self, accident_event_id: UUID, event_type: ChronosTimelineEventType, raw_value: str | None
    ) -> ChronosTimelineEvent | None:
        raw_condition: ColumnElement[bool]
        if raw_value is None:
            raw_condition = ChronosTimelineEventModel.raw_value.is_(None)
        else:
            raw_condition = ChronosTimelineEventModel.raw_value == raw_value
        stmt = select(ChronosTimelineEventModel).where(
            ChronosTimelineEventModel.accident_event_id == accident_event_id,
            ChronosTimelineEventModel.event_type == event_type.value,
            raw_condition,
        )
        result = await self._session.execute(stmt)
        row = result.scalar_one_or_none()
        return _chronos_te_to_domain(row) if row else None


class SqlChronosEventLinkRepository(ChronosEventLinkRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, link: ChronosEventLink) -> None:
        self._session.add(ChronosEventLinkModel(**_domain_data(link)))

    async def upsert_link(self, link: ChronosEventLink) -> tuple[ChronosEventLink, bool]:
        """Insert a Chronos event link idempotently.

        Uses ``INSERT … ON CONFLICT DO NOTHING`` against the unique index
        ``uq_chronos_event_links_pair`` on
        ``(accident_event_id, predecessor_event_id, successor_event_id,
        relationship_type)`` so two concurrent extractors linking the same
        edge cannot both insert and collide.
        """
        stmt = (
            insert(ChronosEventLinkModel)
            .values(**_domain_data(link))
            .on_conflict_do_nothing(
                index_elements=[
                    "accident_event_id",
                    "predecessor_event_id",
                    "successor_event_id",
                    "relationship_type",
                ]
            )
            .returning(ChronosEventLinkModel)
        )
        result = await self._session.execute(stmt)
        row = result.scalar_one_or_none()
        if row is not None:
            return _chronos_link_to_domain(row), True
        # DO NOTHING: re-select the pre-existing row.
        existing = await self._session.execute(
            select(ChronosEventLinkModel).where(
                ChronosEventLinkModel.accident_event_id == link.accident_event_id,
                ChronosEventLinkModel.predecessor_event_id == link.predecessor_event_id,
                ChronosEventLinkModel.successor_event_id == link.successor_event_id,
                ChronosEventLinkModel.relationship_type == link.relationship_type,
            )
        )
        row = existing.scalar_one_or_none()
        if row is not None:
            return _chronos_link_to_domain(row), False
        raise ConcurrentUpsertError(
            f"ChronosEventLink upsert: ON CONFLICT fired for "
            f"(accident_event_id={link.accident_event_id}, "
            f"predecessor={link.predecessor_event_id}, "
            f"successor={link.successor_event_id}, "
            f"relationship_type={link.relationship_type!r}) "
            "but re-select found no existing row."
        )

    async def list_for_accident_event(self, accident_event_id: UUID) -> list[ChronosEventLink]:
        result = await self._session.execute(
            select(ChronosEventLinkModel).where(
                ChronosEventLinkModel.accident_event_id == accident_event_id
            )
        )
        return [_chronos_link_to_domain(row) for row in result.scalars().all()]


class SqlChronosSequenceReviewRepository(ChronosSequenceReviewRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, review: ChronosSequenceReview) -> None:
        """Idempotently insert a PENDING sequence review for an unordered pair.

        Uses ``INSERT … ON CONFLICT DO NOTHING`` targeting the partial
        expression index ``uq_chronos_sequence_reviews_pending_pair``
        (migration 029), which normalises pair order via LEAST/GREATEST so
        that (A, B) and (B, A) hash to the same conflict target.  This
        makes the insert race-safe under concurrent Chronos extraction
        workers: both see "no existing PENDING pair" in their SELECT, but
        only one INSERT wins and the other silently does nothing.

        The index is now also declared in ``ChronosSequenceReviewModel.__table_args__``
        to prevent Alembic autogenerate drift.
        """
        stmt = (
            insert(ChronosSequenceReviewModel)
            .values(**_domain_data(review))
            .on_conflict_do_nothing(
                index_elements=[
                    text("LEAST(timeline_event_id_a::text, timeline_event_id_b::text)"),
                    text("GREATEST(timeline_event_id_a::text, timeline_event_id_b::text)"),
                ],
                index_where=text("status = 'PENDING'"),
            )
        )
        await self._session.execute(stmt)

    async def list_pending(self, limit: int = 50, offset: int = 0) -> list[ChronosSequenceReview]:
        stmt = (
            select(ChronosSequenceReviewModel)
            .where(ChronosSequenceReviewModel.status == ChronosSequenceReviewStatus.PENDING.value)
            .order_by(ChronosSequenceReviewModel.created_at)
            .offset(offset)
            .limit(limit)
        )
        result = await self._session.execute(stmt)
        return [_chronos_review_to_domain(row) for row in result.scalars().all()]

    async def mark_confirmed(
        self, review_id: UUID, resolved_by: UUID, note: str | None = None
    ) -> None:
        row = await self._session.get(ChronosSequenceReviewModel, review_id)
        if row:
            row.status = ChronosSequenceReviewStatus.CONFIRMED.value
            row.resolved_at = datetime.now(UTC)
            row.resolved_by = resolved_by
            row.resolution_note = note

    async def mark_rejected(
        self, review_id: UUID, resolved_by: UUID, note: str | None = None
    ) -> None:
        row = await self._session.get(ChronosSequenceReviewModel, review_id)
        if row:
            row.status = ChronosSequenceReviewStatus.REJECTED.value
            row.resolved_at = datetime.now(UTC)
            row.resolved_by = resolved_by
            row.resolution_note = note


# ── Hermes SQL Repositories ──────────────────────────────────────────────────
