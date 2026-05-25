"""Enterprise tenant router.

Routes mounted under ``/api/v1/enterprise/tenants/{tenant_id}/...``.
Every route depends on :func:`require_tenant_membership` which
enforces:

1. The API key is bound to a tenant (HTTP 403 NOT_A_TENANT_API_KEY).
2. The bound tenant matches the path tenant_id (HTTP 403
   CROSS_TENANT_ACCESS).
3. The tenant is active (HTTP 403 TENANT_INACTIVE).
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response

from atlas.application.dto import CurrentTenantUser
from atlas.application.unit_of_work import UnitOfWork
from atlas.application.use_cases.echo_crossref import (
    RequestEchoCrossReference,
    RequestEchoCrossReferenceInput,
    RunEchoCrossReference,
    RunEchoCrossReferenceInput,
)
from atlas.application.use_cases.tenancy import (
    GetTenantEventOverlay,
    ListTenantEvents,
    RegisterTenantSource,
    RegisterTenantSourceInput,
    UpsertTenantEventOverlay,
    UpsertTenantEventOverlayInput,
)
from atlas.application.use_cases.tenant_ingestion import (
    CompleteTenantIngestionRun,
    CompleteTenantIngestionRunInput,
    IncomingClaim,
    ListTenantEvidenceForEvent,
    OpenTenantIngestionRun,
    OpenTenantIngestionRunInput,
    SubmitTenantClaimsBatch,
    SubmitTenantClaimsBatchInput,
    SubmitTenantSafetyReport,
    SubmitTenantSafetyReportInput,
)
from atlas.domain.tenancy.entities import (
    TenantClaimKind,
    TenantEventAssociationKind,
    TenantIngestionRunStatus,
    TenantSafetyReportKind,
)
from atlas.presentation.api.dependencies import get_tenant_uow, require_tenant_membership
from atlas.presentation.api.responses import offloaded_json_response
from atlas.presentation.api.schemas.tenancy import (
    CompleteIngestionRunRequest,
    CrossrefMatchComponentItem,
    CrossrefMatchItem,
    CrossrefResultResponse,
    EventAssociationItem,
    IngestionRunResponse,
    OpenIngestionRunRequest,
    RegisterTenantSourceRequest,
    RequestCrossrefResponse,
    SafetyReportItem,
    SubmitClaimsBatchRequest,
    SubmitClaimsBatchResponse,
    SubmitSafetyReportRequest,
    SubmitSafetyReportResponse,
    TenantClaimItem,
    TenantEventListItemResponse,
    TenantEventListResponse,
    TenantEventOverlayResponse,
    TenantEvidenceForEventResponse,
    TenantOverlayItem,
    TenantSourceResponse,
    UpsertTenantEventOverlayRequest,
)

router = APIRouter(
    prefix="/enterprise/tenants/{tenant_id}",
    tags=["enterprise"],
)

# Path-bound tenant_id is captured by the dependency; we don't need
# to also declare it as a function parameter on every route.


# ── Sources ──────────────────────────────────────────────────────────────────


@router.post(
    "/sources",
    response_model=TenantSourceResponse,
    status_code=201,
)
async def register_source(
    tenant_id: UUID,
    request: RegisterTenantSourceRequest,
    caller: CurrentTenantUser = Depends(require_tenant_membership()),
    uow: UnitOfWork = Depends(get_tenant_uow),
) -> Response:
    source = await RegisterTenantSource(uow).execute(
        RegisterTenantSourceInput(
            tenant_id=tenant_id,
            caller_tenant_id=caller.tenant_id,
            caller_tenant_role=caller.tenant_role,
            name=request.name,
            kind=request.kind,
            reliability_tier=request.reliability_tier,
        )
    )
    payload = TenantSourceResponse(
        id=source.id,
        tenant_id=source.tenant_id,
        name=source.name,
        kind=source.kind,
        reliability_tier=source.reliability_tier,
        created_at=source.created_at,
    )
    return await offloaded_json_response(payload.model_dump(mode="json"), status_code=201)


# ── Event overlay (read + upsert) ───────────────────────────────────────────


@router.get(
    "/events/{event_id}/overlay",
    response_model=TenantEventOverlayResponse,
)
async def get_event_overlay(
    tenant_id: UUID,
    event_id: UUID,
    caller: CurrentTenantUser = Depends(require_tenant_membership()),
    uow: UnitOfWork = Depends(get_tenant_uow),
) -> Response:
    view = await GetTenantEventOverlay(uow).execute(
        tenant_id=tenant_id,
        caller_tenant_id=caller.tenant_id,
        event_id=event_id,
    )
    payload = TenantEventOverlayResponse(
        event_id=view.event_id,
        overlay=(
            TenantOverlayItem(
                notes_markdown=view.overlay.notes_markdown,
                overlay_fields=view.overlay.overlay_fields,
                created_at=view.overlay.created_at,
                updated_at=view.overlay.updated_at,
            )
            if view.overlay is not None
            else None
        ),
        public_fields=view.public_fields,
        public_completeness_score=view.public_completeness_score,
        public_projection_version=view.public_projection_version,
    )
    return await offloaded_json_response(payload.model_dump(mode="json"))


@router.put(
    "/events/{event_id}/overlay",
    response_model=TenantEventOverlayResponse,
)
async def upsert_event_overlay(
    tenant_id: UUID,
    event_id: UUID,
    request: UpsertTenantEventOverlayRequest,
    caller: CurrentTenantUser = Depends(require_tenant_membership()),
    uow: UnitOfWork = Depends(get_tenant_uow),
) -> Response:
    overlay = await UpsertTenantEventOverlay(uow).execute(
        UpsertTenantEventOverlayInput(
            tenant_id=tenant_id,
            caller_tenant_id=caller.tenant_id,
            caller_tenant_role=caller.tenant_role,
            event_id=event_id,
            notes_markdown=request.notes_markdown,
            overlay_fields=request.overlay_fields,
        )
    )
    # Re-read the public projection for the response context — we
    # could thread it through the use case, but a second small read
    # keeps the use case's input shape clean.
    projection = await uow.projections.get(event_id)
    await uow.rollback()
    payload = TenantEventOverlayResponse(
        event_id=overlay.event_id,
        overlay=TenantOverlayItem(
            notes_markdown=overlay.notes_markdown,
            overlay_fields=overlay.overlay_fields,
            created_at=overlay.created_at,
            updated_at=overlay.updated_at,
        ),
        public_fields=projection.fields if projection else {},
        public_completeness_score=(projection.completeness_score if projection else 0.0),
        public_projection_version=(projection.projection_version if projection else 0),
    )
    return await offloaded_json_response(payload.model_dump(mode="json"))


# ── Event list ───────────────────────────────────────────────────────────────


@router.get("/events", response_model=TenantEventListResponse)
async def list_events(
    tenant_id: UUID,
    limit: int = Query(default=25, ge=1, le=100),
    cursor: UUID | None = Query(default=None),
    caller: CurrentTenantUser = Depends(require_tenant_membership()),
    uow: UnitOfWork = Depends(get_tenant_uow),
) -> Response:
    result = await ListTenantEvents(uow).execute(
        tenant_id=tenant_id,
        caller_tenant_id=caller.tenant_id,
        limit=limit,
        after_id=cursor,
    )
    payload = TenantEventListResponse(
        items=[
            TenantEventListItemResponse(
                event_id=item.event_id,
                has_overlay=item.has_overlay,
                overlay_updated_at=item.overlay_updated_at,
                notes_preview=item.notes_preview,
            )
            for item in result.items
        ],
        limit=result.limit,
        next_cursor=result.next_cursor,
    )
    return await offloaded_json_response(payload.model_dump(mode="json"))


# ── Phase 6: ingestion + safety reports ────────────────────────────────────


def _coerce_claim_kind(value: str) -> TenantClaimKind:
    """Map an incoming string to ``TenantClaimKind``.

    422 surfaces via Pydantic for unknown values when we construct
    the enum; this helper produces a clearer error than the raw
    ValueError that ``StrEnum(...)`` raises.
    """
    try:
        return TenantClaimKind(value)
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "INVALID_CLAIM_KIND",
                "message": (
                    f"claim_kind must be one of "
                    f"{sorted(k.value for k in TenantClaimKind)}; got {value!r}"
                ),
            },
        ) from exc


def _coerce_report_kind(value: str) -> TenantSafetyReportKind:
    try:
        return TenantSafetyReportKind(value)
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "INVALID_REPORT_KIND",
                "message": (
                    f"report_kind must be one of "
                    f"{sorted(k.value for k in TenantSafetyReportKind)}; "
                    f"got {value!r}"
                ),
            },
        ) from exc


def _coerce_association_kind(value: str) -> TenantEventAssociationKind:
    try:
        return TenantEventAssociationKind(value)
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "INVALID_ASSOCIATION_KIND",
                "message": (
                    f"association_kind must be one of "
                    f"{sorted(k.value for k in TenantEventAssociationKind)}; "
                    f"got {value!r}"
                ),
            },
        ) from exc


def _coerce_final_status(value: str) -> TenantIngestionRunStatus:
    """Map 'succeeded' / 'failed' onto the enum.  'running' rejected
    here so the use-case-layer "no completing into RUNNING" check
    has a cleaner surface."""
    try:
        status = TenantIngestionRunStatus(value)
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "INVALID_RUN_STATUS",
                "message": (f"final_status must be 'succeeded' or 'failed'; got {value!r}"),
            },
        ) from exc
    if status == TenantIngestionRunStatus.RUNNING:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "INVALID_RUN_STATUS",
                "message": (
                    "Cannot complete an ingestion run into RUNNING; use 'succeeded' or 'failed'."
                ),
            },
        )
    return status


@router.post(
    "/ingestions",
    response_model=IngestionRunResponse,
    status_code=201,
)
async def open_ingestion_run(
    tenant_id: UUID,
    request: OpenIngestionRunRequest,
    caller: CurrentTenantUser = Depends(require_tenant_membership()),
    uow: UnitOfWork = Depends(get_tenant_uow),
) -> Response:
    run = await OpenTenantIngestionRun(uow).execute(
        OpenTenantIngestionRunInput(
            tenant_id=tenant_id,
            caller_tenant_id=caller.tenant_id,
            caller_tenant_role=caller.tenant_role,
            tenant_source_id=request.tenant_source_id,
        )
    )
    payload = IngestionRunResponse(
        id=run.id,
        tenant_id=run.tenant_id,
        tenant_source_id=run.tenant_source_id,
        status=run.status,
        started_at=run.started_at,
        finished_at=run.finished_at,
    )
    return await offloaded_json_response(payload.model_dump(mode="json"), status_code=201)


@router.post(
    "/ingestions/{run_id}/claims",
    response_model=SubmitClaimsBatchResponse,
)
async def submit_claims_batch(
    tenant_id: UUID,
    run_id: UUID,
    request: SubmitClaimsBatchRequest,
    caller: CurrentTenantUser = Depends(require_tenant_membership()),
    uow: UnitOfWork = Depends(get_tenant_uow),
) -> Response:
    incoming = [
        IncomingClaim(
            event_id=item.event_id,
            field_name=item.field_name,
            field_value=item.field_value,
            claim_kind=_coerce_claim_kind(item.claim_kind),
            confidence=item.confidence,
        )
        for item in request.claims
    ]
    result = await SubmitTenantClaimsBatch(uow).execute(
        SubmitTenantClaimsBatchInput(
            tenant_id=tenant_id,
            caller_tenant_id=caller.tenant_id,
            caller_tenant_role=caller.tenant_role,
            run_id=run_id,
            claims=incoming,
        )
    )
    payload = SubmitClaimsBatchResponse(inserted_count=result.inserted_count)
    return await offloaded_json_response(payload.model_dump(mode="json"))


@router.post(
    "/ingestions/{run_id}/complete",
    response_model=IngestionRunResponse,
)
async def complete_ingestion_run(
    tenant_id: UUID,
    run_id: UUID,
    request: CompleteIngestionRunRequest,
    caller: CurrentTenantUser = Depends(require_tenant_membership()),
    uow: UnitOfWork = Depends(get_tenant_uow),
) -> Response:
    final_status = _coerce_final_status(request.final_status)
    run = await CompleteTenantIngestionRun(uow).execute(
        CompleteTenantIngestionRunInput(
            tenant_id=tenant_id,
            caller_tenant_id=caller.tenant_id,
            caller_tenant_role=caller.tenant_role,
            run_id=run_id,
            final_status=final_status,
        )
    )
    payload = IngestionRunResponse(
        id=run.id,
        tenant_id=run.tenant_id,
        tenant_source_id=run.tenant_source_id,
        status=run.status,
        started_at=run.started_at,
        finished_at=run.finished_at,
    )
    return await offloaded_json_response(payload.model_dump(mode="json"))


@router.post(
    "/safety-reports",
    response_model=SubmitSafetyReportResponse,
    status_code=201,
)
async def submit_safety_report(
    tenant_id: UUID,
    request: SubmitSafetyReportRequest,
    caller: CurrentTenantUser = Depends(require_tenant_membership()),
    uow: UnitOfWork = Depends(get_tenant_uow),
) -> Response:
    result = await SubmitTenantSafetyReport(uow).execute(
        SubmitTenantSafetyReportInput(
            tenant_id=tenant_id,
            caller_tenant_id=caller.tenant_id,
            caller_tenant_role=caller.tenant_role,
            submitter_user_id=caller.user_id,
            report_kind=_coerce_report_kind(request.report_kind),
            narrative_markdown=request.narrative_markdown,
            deidentified_attested=request.deidentified_attested,
            external_report_ref=request.external_report_ref,
            associate_with_event_id=request.associate_with_event_id,
            association_kind=_coerce_association_kind(request.association_kind),
            association_note=request.association_note,
        )
    )
    payload = SubmitSafetyReportResponse(
        report=SafetyReportItem(
            id=result.report.id,
            tenant_id=result.report.tenant_id,
            report_kind=result.report.report_kind.value
            if hasattr(result.report.report_kind, "value")
            else result.report.report_kind,
            narrative_markdown=result.report.narrative_markdown,
            deidentified_attested=result.report.deidentified_attested,
            external_report_ref=result.report.external_report_ref,
            submitter_user_id=result.report.submitter_user_id,
            created_at=result.report.created_at,
        ),
        association=(
            EventAssociationItem(
                id=result.association.id,
                tenant_id=result.association.tenant_id,
                event_id=result.association.event_id,
                claim_id=result.association.claim_id,
                safety_report_id=result.association.safety_report_id,
                association_kind=result.association.association_kind.value
                if hasattr(result.association.association_kind, "value")
                else result.association.association_kind,
                note=result.association.note,
                created_by_user_id=result.association.created_by_user_id,
                created_at=result.association.created_at,
            )
            if result.association is not None
            else None
        ),
        scrub_replacements=result.scrub_replacements,
    )
    return await offloaded_json_response(payload.model_dump(mode="json"), status_code=201)


@router.get(
    "/events/{event_id}/tenant-evidence",
    response_model=TenantEvidenceForEventResponse,
)
async def get_tenant_evidence_for_event(
    tenant_id: UUID,
    event_id: UUID,
    caller: CurrentTenantUser = Depends(require_tenant_membership()),
    uow: UnitOfWork = Depends(get_tenant_uow),
) -> Response:
    view = await ListTenantEvidenceForEvent(uow).execute(
        tenant_id=tenant_id,
        caller_tenant_id=caller.tenant_id,
        event_id=event_id,
    )

    def _claim_item(c) -> TenantClaimItem:
        return TenantClaimItem(
            id=c.id,
            tenant_id=c.tenant_id,
            event_id=c.event_id,
            tenant_source_id=c.tenant_source_id,
            tenant_ingestion_run_id=c.tenant_ingestion_run_id,
            field_name=c.field_name,
            field_value=c.field_value,
            claim_kind=c.claim_kind.value if hasattr(c.claim_kind, "value") else c.claim_kind,
            confidence=c.confidence,
            created_at=c.created_at,
        )

    payload = TenantEvidenceForEventResponse(
        event_id=view.event_id,
        foqa_claims=[_claim_item(c) for c in view.foqa_claims],
        asap_claims=[_claim_item(c) for c in view.asap_claims],
        other_claims=[_claim_item(c) for c in view.other_claims],
        associated_reports=[
            SafetyReportItem(
                id=r.id,
                tenant_id=r.tenant_id,
                report_kind=r.report_kind.value
                if hasattr(r.report_kind, "value")
                else r.report_kind,
                narrative_markdown=r.narrative_markdown,
                deidentified_attested=r.deidentified_attested,
                external_report_ref=r.external_report_ref,
                submitter_user_id=r.submitter_user_id,
                created_at=r.created_at,
            )
            for r in view.associated_reports
        ],
        associations=[
            EventAssociationItem(
                id=a.id,
                tenant_id=a.tenant_id,
                event_id=a.event_id,
                claim_id=a.claim_id,
                safety_report_id=a.safety_report_id,
                association_kind=a.association_kind.value
                if hasattr(a.association_kind, "value")
                else a.association_kind,
                note=a.note,
                created_by_user_id=a.created_by_user_id,
                created_at=a.created_at,
            )
            for a in view.associations
        ],
    )
    return await offloaded_json_response(payload.model_dump(mode="json"))


# ── Echo cross-reference ──────────────────────────────────────────────────────


async def _run_crossref_background(
    tenant_id: UUID,
    crossref_result_id: UUID,
) -> None:
    """Background task: run Echo matching and persist results.

    Runs in a fresh UoW pair (tenant-scoped + public) so it is
    decoupled from the request session that created the PENDING row.
    Failures are logged and written back to the result row as FAILED;
    they never propagate to the caller (the response was already sent).
    """
    import logging as _logging

    from atlas.infrastructure.db.unit_of_work import create_public_uow, create_tenant_uow

    _log = _logging.getLogger(__name__ + "._run_crossref_background")
    try:
        async with create_tenant_uow(tenant_id) as tenant_uow, create_public_uow() as public_uow:
            await RunEchoCrossReference(
                tenant_uow=tenant_uow,
                public_uow=public_uow,
            ).execute(
                RunEchoCrossReferenceInput(
                    tenant_id=tenant_id,
                    crossref_result_id=crossref_result_id,
                )
            )
    except Exception:
        _log.exception(
            "Echo background run failed: tenant=%s result=%s",
            tenant_id,
            crossref_result_id,
        )


@router.post(
    "/reports/{report_id}/crossref",
    response_model=RequestCrossrefResponse,
    status_code=202,
    summary="Request Echo cross-reference for a safety report",
    description=(
        "Enqueues an Echo precedent-matching run for the given safety report. "
        "Returns **202** immediately with a ``crossref_result_id``. "
        "A durable outbox worker executes the run asynchronously.\n\n"
        "**Polling**: ``GET /reports/{report_id}/crossref/{crossref_result_id}`` "
        "until ``status`` is ``COMPLETE`` or ``FAILED``.\n"
        "Recommended interval: every **2 seconds**.\n"
        "Recommended timeout: **120 seconds**.\n\n"
        "**Roles**: OWNER or MEMBER. READ_ONLY cannot request a run.\n\n"
        "**Epistemic note**: matches are *evidence support* (analogous public "
        "accidents exist), not predictions of recurrence."
    ),
)
async def request_crossref(
    tenant_id: UUID,
    report_id: UUID,
    request: Request,
    caller: CurrentTenantUser = Depends(require_tenant_membership()),
    uow: UnitOfWork = Depends(get_tenant_uow),
) -> Response:
    result = await RequestEchoCrossReference(uow).execute(
        RequestEchoCrossReferenceInput(
            tenant_id=tenant_id,
            caller_tenant_id=caller.tenant_id,
            caller_tenant_role=caller.tenant_role,
            safety_report_id=report_id,
        )
    )
    poll_url = str(request.url) + f"/{result.crossref_result_id}"
    payload = RequestCrossrefResponse(
        crossref_result_id=result.crossref_result_id,
        poll_url=poll_url,
        status="PENDING",
    )
    return await offloaded_json_response(payload.model_dump(mode="json"), status_code=202)


@router.get(
    "/reports/{report_id}/crossref/{result_id}",
    response_model=CrossrefResultResponse,
    summary="Poll Echo cross-reference result",
    description=(
        "Returns the current state of an Echo cross-reference run.\n\n"
        "**Status lifecycle**: ``PENDING`` → ``COMPLETE`` (or ``FAILED``).\n"
        "Keep polling every **2 seconds** while ``PENDING``.\n"
        "Stop polling when ``COMPLETE`` or ``FAILED``.\n\n"
        "**On COMPLETE**: ``matches`` contains ranked public-accident precedents, "
        "ordered by ``score`` descending. Each match includes an explainable "
        "``components`` breakdown. ``score`` and ``support`` are similarity "
        "measures — not probabilities of recurrence.\n\n"
        "**On FAILED**: ``error_detail`` explains why. Surface the message to "
        "the user and offer a manual re-request option.\n\n"
        "**Roles**: any tenant role (OWNER, MEMBER, READ_ONLY) can poll."
    ),
)
async def get_crossref_result(
    tenant_id: UUID,
    report_id: UUID,
    result_id: UUID,
    caller: CurrentTenantUser = Depends(require_tenant_membership()),
    uow: UnitOfWork = Depends(get_tenant_uow),
) -> Response:

    result = await uow.tenant_crossref_results.get(tenant_id=tenant_id, result_id=result_id)
    if result is None or result.safety_report_id != report_id:
        raise HTTPException(status_code=404, detail="Cross-reference result not found")

    matches = [
        CrossrefMatchItem(
            event_id=m["event_id"],
            score=m["score"],
            support=m["support"],
            components=[CrossrefMatchComponentItem(**c) for c in m.get("components", [])],
            shared_finding_categories=m.get("shared_finding_categories", []),
            shared_terms=m.get("shared_terms", []),
            display_occurred_on=m.get("display_occurred_on"),
            display_location=m.get("display_location"),
            display_aircraft=m.get("display_aircraft"),
            display_probable_cause=m.get("display_probable_cause"),
        )
        for m in result.matches_json
    ]
    payload = CrossrefResultResponse(
        id=result.id,
        tenant_id=result.tenant_id,
        safety_report_id=result.safety_report_id,
        claim_id=result.claim_id,
        status=result.status.value if hasattr(result.status, "value") else result.status,
        matches=matches,
        match_count=result.match_count,
        matcher_config=result.matcher_config_json,
        requested_at=result.requested_at,
        completed_at=result.completed_at,
        error_detail=result.error_detail,
    )
    return await offloaded_json_response(payload.model_dump(mode="json"))
