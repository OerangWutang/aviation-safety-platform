"""Argus v0.1 — Signal Detection & Review API router.

Metrics
-------
The router records Prometheus metrics around use-case execution:

- ``argus_detection_duration_seconds`` wraps the whole ``execute()`` call,
  including DB I/O and the commit/rollback.
- ``argus_signals_created_total{signal_type=...}`` is incremented per
  newly-created signal using the use case's per-type breakdown.
- ``argus_engine_errors_total{engine=...}`` mirrors ``engines_errored``.
- ``argus_signal_reviews_total{decision=...}`` is incremented after a
  successful review (race-losers, which roll back, are not counted).

Metrics are emitted from the *router* rather than the *use case* because the
``application/`` layer must not depend on ``infrastructure/``.  The recorders
themselves silently no-op if ``prometheus_client`` is not installed, so this
adds zero runtime risk to test or CI environments.
"""

from __future__ import annotations

import time
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query

from atlas.application.unit_of_work import UnitOfWork
from atlas.application.use_cases.list_argus_signals import ListArgusSignals
from atlas.application.use_cases.review_argus_signal import (
    ReviewArgusSignal,
    ReviewArgusSignalInput,
)
from atlas.application.use_cases.run_argus_signal_detection import (
    RunArgusSignalDetection,
    RunArgusSignalDetectionInput,
)
from atlas.domain.enums import ArgusSeverity, ArgusSignalStatus, ArgusSignalType, Role
from atlas.infrastructure.observability.argus_metrics import (
    observe_detection_duration,
    record_engine_error,
    record_signal_created,
    record_signal_review,
)
from atlas.presentation.api.dependencies import get_current_user, get_uow, require_role
from atlas.presentation.api.schemas.argus import (
    ArgusDetectionResponse,
    ArgusReviewSignalRequest,
    ArgusRunDetectionRequest,
    ArgusSignalDetailResponse,
    ArgusSignalEvidenceResponse,
    ArgusSignalResponse,
    ArgusSignalReviewResponse,
    ArgusSignalsPageResponse,
    ArgusSignalsPagination,
)

router = APIRouter(prefix="/argus", tags=["argus"])

_WRITERS = (Role.ADMIN, Role.REVIEWER)
_READERS = (Role.ADMIN, Role.REVIEWER, Role.ANALYST)


@router.post("/run", response_model=ArgusDetectionResponse)
async def run_detection(
    body: ArgusRunDetectionRequest,
    uow: UnitOfWork = Depends(get_uow, scope="function"),
    _: None = Depends(require_role(*_WRITERS)),
):
    # ``time.perf_counter`` is monotonic and unaffected by NTP/wall-clock
    # adjustments — the right primitive for measuring duration intervals.
    started = time.perf_counter()
    try:
        result = await RunArgusSignalDetection(uow).execute(
            RunArgusSignalDetectionInput(
                include_chronos=body.include_chronos,
                include_hermes=body.include_hermes,
                include_atlas=body.include_atlas,
                include_orion=body.include_orion,
                recent_limit=body.recent_limit,
                high_conflict_threshold=body.high_conflict_threshold,
            )
        )
    finally:
        # Observe even on failure: a 500-class error is still a real
        # measurement of "how long Argus was busy".  The exception keeps
        # propagating after the ``finally`` runs.
        observe_detection_duration(time.perf_counter() - started)

    # ``signals_created_by_type`` is the source of truth for the per-type
    # counter; iterating it (rather than ``signal_ids``) avoids a re-fetch
    # by id just to read ``signal_type``.
    for signal_type_str, count in result.signals_created_by_type.items():
        try:
            signal_type = ArgusSignalType(signal_type_str)
        except ValueError:
            # Defensive: the dict keys are always valid enum values today,
            # but skip cleanly if a stale value somehow appears.
            continue
        for _i in range(count):
            record_signal_created(signal_type)

    for engine in result.engines_errored:
        record_engine_error(engine)

    return ArgusDetectionResponse.model_validate(result)


@router.get("/signals", response_model=list[ArgusSignalResponse])
async def list_signals(
    status: ArgusSignalStatus | None = None,
    signal_type: ArgusSignalType | None = None,
    severity: ArgusSeverity | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    uow: UnitOfWork = Depends(get_uow, scope="function"),
    _: None = Depends(require_role(*_READERS)),
):
    signals = await uow.argus_signals.list(
        status=status, signal_type=signal_type, severity=severity, limit=limit, offset=offset
    )
    return [ArgusSignalResponse.model_validate(s) for s in signals]


@router.get("/signals/page", response_model=ArgusSignalsPageResponse)
async def list_signals_page(
    status: ArgusSignalStatus | None = None,
    signal_type: ArgusSignalType | None = None,
    severity: ArgusSeverity | None = None,
    limit: int = Query(default=50, ge=1, le=500),
    cursor: UUID | None = Query(default=None),
    uow: UnitOfWork = Depends(get_uow, scope="function"),
    _: None = Depends(require_role(*_READERS)),
):
    """Keyset-paginated variant of :func:`list_signals`.

    Use this endpoint for new integrations.  The legacy ``GET /argus/signals``
    keeps working (offset pagination) but can silently skip or duplicate
    rows when many signals share the same ``last_detected_at`` (e.g. all
    signals from a single detection run).
    """
    page = await ListArgusSignals(uow).execute_page(
        status=status,
        signal_type=signal_type,
        severity=severity,
        limit=limit,
        cursor=cursor,
    )
    return ArgusSignalsPageResponse(
        items=[ArgusSignalResponse.model_validate(s) for s in page.items],
        pagination=ArgusSignalsPagination(limit=page.limit, next_cursor=page.next_cursor),
    )


@router.get("/signals/{signal_id}", response_model=ArgusSignalDetailResponse)
async def get_signal(
    signal_id: UUID,
    uow: UnitOfWork = Depends(get_uow, scope="function"),
    _: None = Depends(require_role(*_READERS)),
):
    signal = await uow.argus_signals.get(signal_id)
    if signal is None:
        raise HTTPException(status_code=404, detail="Signal not found")
    evidence = await uow.argus_signal_evidence.list_for_signal(signal_id)
    reviews = await uow.argus_signal_reviews.list_for_signal(signal_id)
    return ArgusSignalDetailResponse(
        signal=ArgusSignalResponse.model_validate(signal),
        evidence=[ArgusSignalEvidenceResponse.model_validate(e) for e in evidence],
        reviews=[ArgusSignalReviewResponse.model_validate(r) for r in reviews],
    )


@router.post("/signals/{signal_id}/review", response_model=ArgusSignalResponse)
async def review_signal(
    signal_id: UUID,
    body: ArgusReviewSignalRequest,
    uow: UnitOfWork = Depends(get_uow, scope="function"),
    current_user=Depends(get_current_user),
    _: None = Depends(require_role(*_WRITERS)),
):
    # ``ArgusSignalNotFoundError`` (404) and ``ArgusSignalModifiedError`` (409)
    # flow through the global exception handlers in ``app.py`` — no router
    # try/except wrapper.  That keeps the router thin and the error envelope
    # uniform across the API surface.  We do NOT record a review metric when
    # those raise — race-losers and missing-signal lookups shouldn't show up
    # as completed reviews.
    updated = await ReviewArgusSignal(uow).execute(
        ReviewArgusSignalInput(
            signal_id=signal_id,
            decision=body.decision,
            expected_version=body.expected_version,
            reviewer_id=current_user.user_id,
            note=body.note,
        )
    )
    record_signal_review(body.decision)
    return ArgusSignalResponse.model_validate(updated)
