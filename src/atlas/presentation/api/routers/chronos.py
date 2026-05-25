"""Chronos v0.1 Timeline Engine API router."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends

from atlas.application.unit_of_work import UnitOfWork
from atlas.application.use_cases.extract_chronos_timeline_from_event import (
    ExtractChronosTimelineFromEvent,
)
from atlas.domain.enums import Role
from atlas.presentation.api.dependencies import get_uow, require_role
from atlas.presentation.api.schemas.chronos import (
    ChronosEventLinkResponse,
    ChronosExtractionResponse,
    ChronosSequenceReviewResponse,
    ChronosTimelineEventResponse,
    ChronosTimelineResponse,
)

router = APIRouter(prefix="/chronos", tags=["chronos"])

_READERS = (Role.ADMIN, Role.REVIEWER, Role.ANALYST)
_WRITERS = (Role.ADMIN, Role.REVIEWER)


def _te_response(te) -> ChronosTimelineEventResponse:
    return ChronosTimelineEventResponse(
        id=te.id,
        accident_event_id=te.accident_event_id,
        event_type=te.event_type,
        occurred_at=te.occurred_at,
        timestamp_precision=te.timestamp_precision,
        sequence_index=te.sequence_index,
        description=te.description,
        raw_value=te.raw_value,
        confidence=te.confidence,
        source_claim_id=te.source_claim_id,
        raw_snapshot_id=te.raw_snapshot_id,
        created_at=te.created_at,
        updated_at=te.updated_at,
    )


def _link_response(link) -> ChronosEventLinkResponse:
    return ChronosEventLinkResponse(
        id=link.id,
        accident_event_id=link.accident_event_id,
        predecessor_event_id=link.predecessor_event_id,
        successor_event_id=link.successor_event_id,
        relationship_type=link.relationship_type,
        confidence=link.confidence,
        source_claim_id=link.source_claim_id,
        raw_snapshot_id=link.raw_snapshot_id,
        created_at=link.created_at,
    )


@router.post("/events/{event_id}/extract", response_model=ChronosExtractionResponse)
async def extract_timeline_from_event(
    event_id: UUID,
    uow: UnitOfWork = Depends(get_uow, scope="function"),
    _user=Depends(require_role(*_WRITERS)),
) -> ChronosExtractionResponse:
    result = await ExtractChronosTimelineFromEvent(uow).execute(event_id)
    return ChronosExtractionResponse(
        event_id=result.event_id,
        timeline_events_created_count=result.timeline_events_created_count,
        timeline_events_reused_count=result.timeline_events_reused_count,
        event_links_created_count=result.event_links_created_count,
        timeline_event_ids=result.timeline_event_ids,
        event_link_ids=result.event_link_ids,
    )


@router.get("/events/{event_id}/timeline", response_model=ChronosTimelineResponse)
async def get_event_timeline(
    event_id: UUID,
    uow: UnitOfWork = Depends(get_uow, scope="function"),
    _user=Depends(require_role(*_READERS)),
) -> ChronosTimelineResponse:
    timeline_events = await uow.chronos_timeline_events.list_for_accident_event(event_id)
    event_links = await uow.chronos_event_links.list_for_accident_event(event_id)

    def _sort_key(te):
        return (
            te.sequence_index if te.sequence_index is not None else 9999,
            te.occurred_at.isoformat() if te.occurred_at else "~",
            te.created_at.isoformat(),
        )

    timeline_events_sorted = sorted(timeline_events, key=_sort_key)
    await uow.rollback()
    return ChronosTimelineResponse(
        event_id=event_id,
        timeline_events=[_te_response(te) for te in timeline_events_sorted],
        event_links=[_link_response(link) for link in event_links],
    )


@router.get("/reviews/pending", response_model=list[ChronosSequenceReviewResponse])
async def get_pending_reviews(
    uow: UnitOfWork = Depends(get_uow, scope="function"),
    _user=Depends(require_role(*_READERS)),
) -> list[ChronosSequenceReviewResponse]:
    reviews = await uow.chronos_sequence_reviews.list_pending()
    await uow.rollback()
    return [
        ChronosSequenceReviewResponse(
            id=r.id,
            accident_event_id=r.accident_event_id,
            timeline_event_id_a=r.timeline_event_id_a,
            timeline_event_id_b=r.timeline_event_id_b,
            reason=r.reason,
            status=r.status,
            created_at=r.created_at,
            resolved_at=r.resolved_at,
            resolved_by=r.resolved_by,
            resolution_note=r.resolution_note,
        )
        for r in reviews
    ]
