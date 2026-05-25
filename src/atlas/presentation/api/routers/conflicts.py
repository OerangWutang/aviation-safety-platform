from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi.encoders import jsonable_encoder

from atlas.application.dto import CurrentUser
from atlas.application.unit_of_work import UnitOfWork
from atlas.application.use_cases.query_conflict_history import QueryConflictHistory
from atlas.application.use_cases.reopen_conflict import ReopenConflict
from atlas.application.use_cases.resolve_conflict import ResolveConflict
from atlas.domain.enums import Role
from atlas.domain.exceptions import ConflictModifiedError
from atlas.presentation.api.dependencies import get_uow, require_role
from atlas.presentation.api.responses import offloaded_json_response
from atlas.presentation.api.schemas.conflicts import (
    ConflictHistoryResponse,
    ReopenConflictRequest,
    ResolveConflictRequest,
    ResolveConflictResponse,
)

router = APIRouter(prefix="/conflicts", tags=["conflicts"])
logger = logging.getLogger(__name__)

# Roles allowed to read conflict data (all authenticated roles).
_READERS = (Role.ADMIN, Role.REVIEWER, Role.ANALYST)


def _dump(obj: Any) -> Any:
    if obj is None:
        return None
    if hasattr(obj, "model_dump"):
        return jsonable_encoder(obj.model_dump())
    return jsonable_encoder(obj)


@router.get("")
async def list_conflicts(
    event_id: UUID | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    uow: UnitOfWork = Depends(get_uow, scope="function"),
    _user: CurrentUser = Depends(require_role(*_READERS)),
) -> Response:
    """List conflicts for a given event, with simple offset-based pagination."""
    if event_id is None:
        # Validation problem with the request - 422 mirrors how Pydantic
        # surfaces missing fields, keeping the API consistent.
        raise HTTPException(status_code=422, detail="event_id query parameter is required")
    conflicts = await uow.conflicts.find_by_event(event_id, limit=limit, offset=offset)
    payload = [_dump(c) for c in conflicts]
    await uow.rollback()
    return await offloaded_json_response(payload)


@router.get("/{conflict_id}")
async def get_conflict(
    conflict_id: UUID,
    uow: UnitOfWork = Depends(get_uow, scope="function"),
    _user: CurrentUser = Depends(require_role(*_READERS)),
) -> Any:
    conflict = await uow.conflicts.get(conflict_id)
    if not conflict:
        raise HTTPException(status_code=404, detail="Conflict not found")
    payload = _dump(conflict)
    await uow.rollback()
    return await offloaded_json_response(payload)


@router.get("/{conflict_id}/history", response_model=ConflictHistoryResponse)
async def conflict_history(
    conflict_id: UUID,
    include_archive: bool = Query(default=False),
    limit: int = Query(default=50, ge=1, le=500),
    cursor: UUID | None = Query(default=None),
    uow: UnitOfWork = Depends(get_uow, scope="function"),
    _user: CurrentUser = Depends(require_role(*_READERS)),
) -> Any:
    try:
        payload = await QueryConflictHistory(uow).execute(
            conflict_id,
            include_archive,
            limit=limit,
            cursor=cursor,
        )
        await uow.rollback()
        return payload
    except NotImplementedError as exc:
        raise HTTPException(status_code=501, detail=str(exc)) from exc


def _conflict_modified_response(exc: ConflictModifiedError) -> HTTPException:
    return HTTPException(
        status_code=409,
        detail={
            "detail": "Conflict has been modified since you loaded it.",
            "modifier_reason": str(exc.modifier_reason) if exc.modifier_reason else None,
            "current_version": exc.current_version,
            "latest_activity": _dump(exc.latest_activity),
            "conflict": _dump(exc.current_conflict),
            "accident_record": _dump(exc.current_projection),
        },
    )


@router.post("/{conflict_id}/resolve", response_model=ResolveConflictResponse)
async def resolve_conflict(
    conflict_id: UUID,
    body: ResolveConflictRequest,
    uow: UnitOfWork = Depends(get_uow, scope="function"),
    current_user: CurrentUser = Depends(require_role(Role.REVIEWER, Role.ADMIN)),
) -> Any:
    # ClaimNotInConflictError is intentionally NOT caught here - the global
    # exception handler maps it to 422, which is the consistent semantic for
    # "request was syntactically valid but referenced a claim that does not
    # belong to this conflict". The previous local 400 conversion was the
    # source of the documented 400-vs-422 inconsistency.
    try:
        conflict, projection = await ResolveConflict(uow).execute(
            conflict_id=conflict_id,
            expected_version=body.expected_version,
            winning_claim_id=body.winning_claim_id,
            manual_override_value=body.manual_override_value,
            manual_override_provided=body.manual_override_provided,
            current_user_id=current_user.user_id,
            reason=body.reason,
        )
        return {"conflict": _dump(conflict), "accident_record": _dump(projection)}
    except ConflictModifiedError as exc:
        raise _conflict_modified_response(exc) from exc


@router.post("/{conflict_id}/reopen", response_model=ResolveConflictResponse)
async def reopen_conflict(
    conflict_id: UUID,
    body: ReopenConflictRequest,
    uow: UnitOfWork = Depends(get_uow, scope="function"),
    current_user: CurrentUser = Depends(require_role(Role.REVIEWER, Role.ADMIN)),
) -> Any:
    """Manually reopen a previously resolved conflict.

    Only valid for conflicts in the RESOLVED state. The previous winner's
    losers are reactivated so the conflict actually has competing claims again.
    """
    try:
        conflict, projection = await ReopenConflict(uow).execute(
            conflict_id=conflict_id,
            expected_version=body.expected_version,
            current_user_id=current_user.user_id,
            reason=body.reason,
        )
        return {"conflict": _dump(conflict), "accident_record": _dump(projection)}
    except ConflictModifiedError as exc:
        raise _conflict_modified_response(exc) from exc
