"""Hermes v0.1 — Source Registry & Fetch Queue API router."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query

from atlas.application.unit_of_work import UnitOfWork
from atlas.application.use_cases.create_hermes_crawl_target import (
    CreateHermesCrawlTarget,
    CreateHermesCrawlTargetInput,
)
from atlas.application.use_cases.enqueue_hermes_fetch_job import (
    EnqueueHermesFetchJob,
    EnqueueHermesFetchJobInput,
)
from atlas.application.use_cases.register_hermes_source import (
    RegisterHermesSource,
    RegisterHermesSourceInput,
)
from atlas.application.use_cases.run_hermes_fetch_job import RunHermesFetchJob
from atlas.config import get_settings
from atlas.domain.enums import HermesFetchJobStatus, HermesTargetStatus, Role
from atlas.presentation.api.dependencies import get_uow, require_role
from atlas.presentation.api.schemas.hermes import (
    HermesCrawlTargetCreateRequest,
    HermesCrawlTargetResponse,
    HermesFetchedDocumentResponse,
    HermesFetchJobEnqueueRequest,
    HermesFetchJobResponse,
    HermesFetchResultResponse,
    HermesSourceChangeResponse,
    HermesSourceCreateRequest,
    HermesSourceResponse,
)

router = APIRouter(prefix="/hermes", tags=["hermes"])

_READERS = (Role.ADMIN, Role.REVIEWER, Role.ANALYST)
_WRITERS = (Role.ADMIN, Role.REVIEWER)


@router.post("/sources", response_model=HermesSourceResponse, status_code=200)
async def register_source(
    body: HermesSourceCreateRequest,
    uow: UnitOfWork = Depends(get_uow, scope="function"),
    _: None = Depends(require_role(*_WRITERS)),
):
    source = await RegisterHermesSource(uow).execute(
        RegisterHermesSourceInput(
            name=body.name,
            source_type=body.source_type,
            base_url=body.base_url,
            reliability_tier=body.reliability_tier,
        )
    )
    return HermesSourceResponse.model_validate(source)


@router.get("/sources", response_model=list[HermesSourceResponse])
async def list_sources(
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    uow: UnitOfWork = Depends(get_uow, scope="function"),
    _: None = Depends(require_role(*_READERS)),
):
    sources = await uow.hermes_sources.list_active(limit=limit, offset=offset)
    return [HermesSourceResponse.model_validate(s) for s in sources]


@router.post("/targets", response_model=HermesCrawlTargetResponse, status_code=200)
async def create_target(
    body: HermesCrawlTargetCreateRequest,
    uow: UnitOfWork = Depends(get_uow, scope="function"),
    _: None = Depends(require_role(*_WRITERS)),
):
    target = await CreateHermesCrawlTarget(uow).execute(
        CreateHermesCrawlTargetInput(
            source_id=body.source_id,
            url=body.url,
            label=body.label,
        )
    )
    return HermesCrawlTargetResponse.model_validate(target)


@router.get("/targets", response_model=list[HermesCrawlTargetResponse])
async def list_targets(
    status: HermesTargetStatus | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    uow: UnitOfWork = Depends(get_uow, scope="function"),
    _: None = Depends(require_role(*_READERS)),
):
    targets = await uow.hermes_crawl_targets.list(status=status, limit=limit, offset=offset)
    return [HermesCrawlTargetResponse.model_validate(t) for t in targets]


@router.post("/targets/{target_id}/enqueue", response_model=HermesFetchJobResponse)
async def enqueue_job(
    target_id: UUID,
    body: HermesFetchJobEnqueueRequest,
    uow: UnitOfWork = Depends(get_uow, scope="function"),
    _: None = Depends(require_role(*_WRITERS)),
):
    job = await EnqueueHermesFetchJob(uow).execute(
        EnqueueHermesFetchJobInput(
            target_id=target_id,
            priority=body.priority,
            scheduled_at=body.scheduled_at,
        )
    )
    return HermesFetchJobResponse.model_validate(job)


@router.get("/jobs", response_model=list[HermesFetchJobResponse])
async def list_jobs(
    status: HermesFetchJobStatus | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    uow: UnitOfWork = Depends(get_uow, scope="function"),
    _: None = Depends(require_role(*_READERS)),
):
    jobs = await uow.hermes_fetch_jobs.list(status=status, limit=limit, offset=offset)
    return [HermesFetchJobResponse.model_validate(j) for j in jobs]


@router.post("/jobs/{job_id}/run", response_model=HermesFetchResultResponse)
async def run_job(
    job_id: UUID,
    uow: UnitOfWork = Depends(get_uow, scope="function"),
    _: None = Depends(require_role(*_WRITERS)),
):
    settings = get_settings()
    try:
        settings.validate_hermes_worker_settings()
    except RuntimeError as exc:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "HERMES_NOT_CONFIGURED",
                "message": str(exc),
            },
        ) from exc
    result = await RunHermesFetchJob(
        uow,
        allowed_hosts=tuple(settings.hermes_allowed_hosts),
    ).execute(job_id)
    return HermesFetchResultResponse.model_validate(result)


@router.get("/targets/{target_id}/documents", response_model=list[HermesFetchedDocumentResponse])
async def list_documents(
    target_id: UUID,
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    uow: UnitOfWork = Depends(get_uow, scope="function"),
    _: None = Depends(require_role(*_READERS)),
):
    docs = await uow.hermes_fetched_documents.list_for_target(target_id, limit=limit, offset=offset)
    return [HermesFetchedDocumentResponse.model_validate(d) for d in docs]


@router.get("/targets/{target_id}/changes", response_model=list[HermesSourceChangeResponse])
async def list_target_changes(
    target_id: UUID,
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    uow: UnitOfWork = Depends(get_uow, scope="function"),
    _: None = Depends(require_role(*_READERS)),
):
    changes = await uow.hermes_source_changes.list_for_target(target_id, limit=limit, offset=offset)
    return [HermesSourceChangeResponse.model_validate(c) for c in changes]


@router.get("/changes/recent", response_model=list[HermesSourceChangeResponse])
async def list_recent_changes(
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    uow: UnitOfWork = Depends(get_uow, scope="function"),
    _: None = Depends(require_role(*_READERS)),
):
    changes = await uow.hermes_source_changes.list_recent(limit=limit, offset=offset)
    return [HermesSourceChangeResponse.model_validate(c) for c in changes]
