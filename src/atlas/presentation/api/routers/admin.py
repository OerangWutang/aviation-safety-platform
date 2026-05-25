from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query

from atlas.application.unit_of_work import UnitOfWork
from atlas.application.use_cases.list_pending_reviews import ListPendingDuplicateReviews
from atlas.application.use_cases.merge_duplicate_events import MergeDuplicateEvents
from atlas.application.use_cases.query_operational_metrics import QueryOperationalMetrics
from atlas.application.use_cases.rebuild_all_projections import RebuildAllProjections
from atlas.application.use_cases.reindex_public_events import ReindexPublicEvents
from atlas.application.use_cases.reproject_event import ReProjectEvent
from atlas.application.use_cases.review_duplicate import ReviewDuplicate
from atlas.application.use_cases.set_source_field_mapping import SetSourceFieldMapping
from atlas.application.use_cases.verify_projection_consistency import VerifyProjectionConsistency
from atlas.config import get_settings
from atlas.domain.enums import Role
from atlas.presentation.api.dependencies import get_uow, require_role
from atlas.presentation.api.responses import offloaded_json_response
from atlas.presentation.api.schemas.admin import (
    MergeRequest,
    MergeResponse,
    RebuildRequest,
    RebuildResponse,
    ReviewActionRequest,
    ReviewActionResponse,
    SetSourceFieldMappingRequest,
    SetSourceFieldMappingResponse,
)
from atlas.presentation.api.schemas.search import ReindexResponse

router = APIRouter(tags=["admin"])


@router.post("/projections/rebuild", response_model=RebuildResponse)
async def rebuild_projection(
    body: RebuildRequest,
    uow: UnitOfWork = Depends(get_uow, scope="function"),
    _user=Depends(require_role(Role.ADMIN)),
):
    if body.all:
        settings = get_settings()
        max_events = None if body.max_events == -1 else body.max_events
        if settings.is_production:
            if max_events is None and not settings.admin_allow_unbounded_projection_rebuilds:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        "Unlimited projection rebuilds are disabled in production. "
                        "Set max_events to a bounded value or explicitly enable "
                        "ADMIN_ALLOW_UNBOUNDED_PROJECTION_REBUILDS=true for a maintenance window."
                    ),
                )
            if max_events is not None and max_events > settings.admin_max_projection_rebuild_events:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        "Projection rebuild cap exceeds production limit "
                        f"({settings.admin_max_projection_rebuild_events})."
                    ),
                )
        result = await RebuildAllProjections(uow).execute(
            batch_size=body.batch_size,
            max_events=max_events,
        )
        cap_label = "unlimited" if max_events is None else str(max_events)
        return RebuildResponse(
            processed=result.processed,
            skipped=result.skipped,
            failed_event_ids=result.failed_event_ids,
            errors=result.errors,
            message=(
                f"Rebuilt {result.processed} projections "
                f"({result.skipped} skipped, cap={cap_label})"
            ),
        )
    if body.event_id:
        await ReProjectEvent(uow).execute(body.event_id)
        return RebuildResponse(processed=1, message=f"Projection rebuilt for {body.event_id}")
    return RebuildResponse(processed=0, message="Specify event_id or all=true")


@router.get("/projections/verify")
async def verify_projection(
    event_id: UUID,
    uow: UnitOfWork = Depends(get_uow, scope="function"),
    _user=Depends(require_role(Role.ADMIN)),
):
    """Read-only consistency check; recomputes in memory without persisting."""
    payload = await VerifyProjectionConsistency(uow).execute(event_id)
    await uow.rollback()
    if payload is None:
        raise HTTPException(status_code=404, detail="No projection found for this event")
    return await offloaded_json_response(payload)


@router.get("/outbox")
async def list_outbox(
    limit: int = Query(default=50, ge=1, le=500),
    uow: UnitOfWork = Depends(get_uow, scope="function"),
    _user=Depends(require_role(Role.ADMIN)),
):
    payload = [event.model_dump() for event in await uow.outbox.list_recent(limit)]
    await uow.rollback()
    return await offloaded_json_response(payload)


@router.post("/outbox/process")
async def process_outbox(
    limit: int = Query(default=100, ge=1, le=1000),
    _user=Depends(require_role(Role.ADMIN)),
):
    # Note: this endpoint intentionally does NOT accept a ``get_uow`` dependency.
    # ``OutboxWorker.process_batch`` manages its own transaction lifecycle via
    # ``create_uow()``: it commits the lock batch in one session, then processes
    # each event in a separate session.  Putting the worker inside the request
    # session would fuse those transaction boundaries and break the fencing logic.
    from atlas.infrastructure.event_bus.outbox_worker import OutboxWorker

    processed = await OutboxWorker(worker_id="api-admin-manual").process_batch(limit=limit)
    return {"processed": processed}


@router.get("/metrics")
async def get_metrics(
    uow: UnitOfWork = Depends(get_uow, scope="function"),
    _user=Depends(require_role(Role.ADMIN)),
):
    """Key operational metrics - useful for monitoring dashboards and alerting.

    All values are computed via repository aggregate queries; no ORM model or
    SQLAlchemy import crosses into the presentation layer.  For time-series
    monitoring, scrape this endpoint on an interval and push to your metrics
    store (Prometheus, Datadog, etc.).
    """
    metrics = await QueryOperationalMetrics(uow).execute()
    payload = metrics.as_dict()
    await uow.rollback()
    return await offloaded_json_response(payload)


@router.post("/events/merge", response_model=MergeResponse, status_code=200)
async def merge_duplicate_events(
    body: MergeRequest,
    uow: UnitOfWork = Depends(get_uow, scope="function"),
    current_user=Depends(require_role(Role.ADMIN)),
):
    """Merge a duplicate event into the surviving event.

    The source event is absorbed: its active claims are reproduced on the
    target event, the source is marked ``merged_into_event_id = target``,
    and its claims are superseded.  A CLAIMS_UPDATED outbox event is queued
    for the target projection rebuild.  Any pending duplicate review for the
    pair is resolved to MERGED.

    This operation is irreversible via the API.  To undo it you would need
    to restore from a backup or manually clear ``merged_into_event_id``.
    """
    result = await MergeDuplicateEvents(uow).execute(
        source_event_id=body.source_event_id,
        target_event_id=body.target_event_id,
        resolved_by=current_user.user_id,
        note=body.note,
    )
    return MergeResponse(
        target_event_id=result.target_event_id,
        source_event_id=result.source_event_id,
        claims_moved=result.claims_transferred,
        message=(
            f"Merged {result.source_event_id} -> {result.target_event_id}; "
            f"{result.claims_transferred} claim(s) moved."
        ),
    )


@router.post("/reviews/{review_id}/resolve", response_model=ReviewActionResponse)
async def resolve_duplicate_review(
    review_id: UUID,
    body: ReviewActionRequest,
    uow: UnitOfWork = Depends(get_uow, scope="function"),
    current_user=Depends(require_role(Role.REVIEWER, Role.ADMIN)),
):
    """Confirm or reject a pending duplicate-event review.

    ``action='confirm'`` triggers a merge of the two events.
    ``action='reject'`` marks the pair as distinct accidents (no merge).

    ``source_event_id`` in the request body controls which event is absorbed
    when confirming. Defaults to ``event_id_b`` (the newer event).

    The response includes a populated ``merge_result`` field when confirming.
    """
    # Pydantic validates action via Literal["confirm", "reject"] - no manual
    # check needed here. Domain errors bubble to the global structured handlers.
    merge_result = await ReviewDuplicate(uow).execute(
        review_id=review_id,
        action=body.action,
        resolved_by=current_user.user_id,
        note=body.note,
        source_event_id=body.source_event_id,
    )

    if body.action == "confirm" and merge_result is not None:
        from atlas.presentation.api.schemas.admin import MergeResponse

        mr = MergeResponse(
            target_event_id=merge_result.target_event_id,
            source_event_id=merge_result.source_event_id,
            claims_moved=merge_result.claims_transferred,
            message=(
                f"Merged {merge_result.source_event_id} -> {merge_result.target_event_id}; "
                f"{merge_result.claims_transferred} claim(s) moved."
            ),
        )
        return ReviewActionResponse(
            review_id=review_id,
            action="confirm",
            message="Confirmed duplicate: events have been merged.",
            merge_result=mr,
        )
    return ReviewActionResponse(
        review_id=review_id,
        action=body.action,
        message="Review rejected: events recorded as distinct accidents.",
    )


@router.get("/reviews")
async def list_pending_reviews(
    limit: int = Query(default=50, ge=1, le=500),
    cursor: UUID | None = Query(default=None),
    uow: UnitOfWork = Depends(get_uow, scope="function"),
    _user=Depends(require_role(Role.REVIEWER, Role.ADMIN)),
):
    """List PENDING duplicate-event reviews awaiting curator decision."""
    page = await ListPendingDuplicateReviews(uow).execute_page(limit=limit, cursor=cursor)
    payload = {
        "items": [r.model_dump() for r in page.items],
        "pagination": {"limit": page.limit, "next_cursor": page.next_cursor},
    }
    await uow.rollback()
    return await offloaded_json_response(payload)


@router.put(
    "/sources/{source_id}/field-mapping",
    response_model=SetSourceFieldMappingResponse,
)
async def set_source_field_mapping(
    source_id: UUID,
    body: SetSourceFieldMappingRequest,
    uow: UnitOfWork = Depends(get_uow, scope="function"),
    _user=Depends(require_role(Role.ADMIN)),
):
    """Replace the durable per-source raw-to-canonical field mapping.

    The body's ``field_mapping`` object completely replaces the current
    ``Source.field_mapping_json`` row for this source.  Pass an empty object
    to clear the mapping.  Unknown canonical targets (typos like
    ``"event_dat"`` for ``"event_date"``) and raw keys that collide under
    tolerant normalisation are rejected with 422 *before* any write, so an
    invalid request never leaves a partial state on the row.

    This mutation is intentionally not part of the idempotency hash material
    for ``IngestSourceData`` - changing the mapping does not destabilise
    replay of prior submissions - but it IS recorded in the
    ``submission_fingerprint_json`` of every subsequent ingestion as
    ``source_mapping_hash`` for audit.
    """
    updated = await SetSourceFieldMapping(uow).execute(
        source_id=source_id,
        field_mapping=body.field_mapping,
    )

    return SetSourceFieldMappingResponse(
        source_id=updated.id,
        field_mapping=updated.field_mapping_json,
        entry_count=len(updated.field_mapping_json),
    )


@router.post("/search/reindex", response_model=ReindexResponse)
async def reindex_search(
    uow: UnitOfWork = Depends(get_uow, scope="function"),
    _user=Depends(require_role(Role.ADMIN)),
) -> ReindexResponse:
    """Rebuild the public search index from PUBLISHED pages.

    Synchronous and bounded.  See :class:`ReindexPublicEvents` for
    the ceiling and the deferred resumable-reindex follow-up.
    """
    result = await ReindexPublicEvents(uow).execute()
    return ReindexResponse(
        pages_reindexed=result.pages_reindexed,
        map_pages_reindexed=result.map_pages_reindexed,
    )
