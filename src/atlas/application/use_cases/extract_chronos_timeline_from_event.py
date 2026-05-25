"""Extract evidence-backed Chronos timeline events from an Atlas accident projection."""

from __future__ import annotations

import logging
from itertools import pairwise
from uuid import UUID

from atlas.application.unit_of_work import UnitOfWork
from atlas.application.use_cases._provenance import select_projected_claims_by_field
from atlas.domain.entities import (
    ChronosEventLink,
    ChronosExtractionResult,
    ChronosSequenceReview,
    ChronosTimelineEvent,
)
from atlas.domain.enums import ChronosSequenceReviewStatus, ChronosTimelineEventType
from atlas.domain.services.chronos_normalizers import safe_str
from atlas.domain.services.chronos_time_parser import parse_chronos_timestamp

logger = logging.getLogger(__name__)

_FIELD_MAPPINGS: dict[ChronosTimelineEventType, tuple[str, ...]] = {
    ChronosTimelineEventType.SCHEDULED_DEPARTURE: (
        "scheduled_departure",
        "scheduled_departure_time",
    ),
    ChronosTimelineEventType.ACTUAL_DEPARTURE: ("actual_departure", "departure_time"),
    ChronosTimelineEventType.TAKEOFF: ("takeoff", "takeoff_time"),
    ChronosTimelineEventType.LAST_CONTACT: ("last_contact", "last_contact_time"),
    ChronosTimelineEventType.EMERGENCY_DECLARED: (
        "emergency_declared",
        "emergency_time",
        "mayday_time",
    ),
    ChronosTimelineEventType.IMPACT: ("impact", "impact_time", "accident_time", "crash_time"),
    ChronosTimelineEventType.LANDING: ("landing", "landing_time"),
    ChronosTimelineEventType.RESCUE_STARTED: ("rescue_started", "rescue_time"),
    ChronosTimelineEventType.INVESTIGATION_OPENED: (
        "investigation_opened",
        "investigation_start_date",
    ),
    ChronosTimelineEventType.REPORT_PUBLISHED: (
        "report_published",
        "final_report_date",
        "report_date",
    ),
}

_SEQUENCE_ORDER: list[ChronosTimelineEventType] = [
    ChronosTimelineEventType.SCHEDULED_DEPARTURE,
    ChronosTimelineEventType.ACTUAL_DEPARTURE,
    ChronosTimelineEventType.TAKEOFF,
    ChronosTimelineEventType.LAST_CONTACT,
    ChronosTimelineEventType.EMERGENCY_DECLARED,
    ChronosTimelineEventType.IMPACT,
    ChronosTimelineEventType.LANDING,
    ChronosTimelineEventType.RESCUE_STARTED,
    ChronosTimelineEventType.INVESTIGATION_OPENED,
    ChronosTimelineEventType.REPORT_PUBLISHED,
]
_SEQUENCE_INDEX: dict[ChronosTimelineEventType, int] = {
    et: idx for idx, et in enumerate(_SEQUENCE_ORDER)
}


class ExtractChronosTimelineFromEvent:
    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    async def execute(self, event_id: UUID) -> ChronosExtractionResult:
        uow = self._uow
        result = ChronosExtractionResult(event_id=event_id)

        projection = await uow.projections.get(event_id)
        if projection is None:
            logger.warning("chronos_extract: no projection for event_id=%s", event_id)
            return result

        fields: dict[str, object] = projection.fields or {}

        # Field-level provenance uses the same WinnerPolicy inputs as Atlas
        # projection (including source reliability tier) and honors resolved
        # conflicts where possible.  Claims whose values do not match the
        # projected field are never attached as support.
        claims_by_field = await select_projected_claims_by_field(
            uow,
            event_id=event_id,
            fields=fields,
            safe_str=safe_str,
        )

        timeline_events: list[ChronosTimelineEvent] = []

        for event_type in _SEQUENCE_ORDER:
            matched_field: str | None = None
            raw_val: str | None = None

            for fname in _FIELD_MAPPINGS[event_type]:
                val = safe_str(fields.get(fname))
                if val is not None:
                    matched_field = fname
                    raw_val = val
                    break

            if raw_val is None:
                continue

            occurred_at, precision = parse_chronos_timestamp(raw_val)
            best_claim = claims_by_field.get(matched_field) if matched_field else None

            candidate = ChronosTimelineEvent(
                accident_event_id=event_id,
                event_type=event_type,
                occurred_at=occurred_at,
                timestamp_precision=precision,
                sequence_index=_SEQUENCE_INDEX[event_type],
                raw_value=raw_val,
                confidence=1.0,
                source_claim_id=getattr(best_claim, "id", None),
                raw_snapshot_id=getattr(best_claim, "raw_snapshot_id", None),
            )

            event, created = await uow.chronos_timeline_events.upsert_event(candidate)
            if created:
                result.timeline_events_created_count += 1
            else:
                result.timeline_events_reused_count += 1
            if event.id not in result.timeline_event_ids:
                result.timeline_event_ids.append(event.id)
            timeline_events.append(event)

        ordered = sorted(
            timeline_events,
            key=lambda e: e.sequence_index if e.sequence_index is not None else 999,
        )

        for pred, succ in pairwise(ordered):
            link = ChronosEventLink(
                accident_event_id=event_id,
                predecessor_event_id=pred.id,
                successor_event_id=succ.id,
                relationship_type="ORDERED_BEFORE",
                confidence=1.0,
            )
            persisted_link, link_created = await uow.chronos_event_links.upsert_link(link)
            if link_created:
                result.event_links_created_count += 1
            if persisted_link.id not in result.event_link_ids:
                result.event_link_ids.append(persisted_link.id)

        for pred, succ in pairwise(ordered):
            if (
                pred.occurred_at is not None
                and succ.occurred_at is not None
                and pred.occurred_at > succ.occurred_at
            ):
                review = ChronosSequenceReview(
                    accident_event_id=event_id,
                    timeline_event_id_a=pred.id,
                    timeline_event_id_b=succ.id,
                    reason="Timestamp order conflicts with default event sequence.",
                    status=ChronosSequenceReviewStatus.PENDING,
                )
                await uow.chronos_sequence_reviews.add(review)

        await uow.commit()
        return result
