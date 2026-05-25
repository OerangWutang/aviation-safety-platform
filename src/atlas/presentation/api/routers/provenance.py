from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response

from atlas.application.dto import CurrentUser
from atlas.application.unit_of_work import UnitOfWork
from atlas.application.use_cases.query_provenance import (
    DEFAULT_PROVENANCE_LIMIT,
    MAX_PROVENANCE_LIMIT,
    QueryProvenance,
)
from atlas.domain.enums import Role
from atlas.presentation.api.dependencies import get_uow, require_role
from atlas.presentation.api.responses import offloaded_json_response

router = APIRouter(prefix="/accidents", tags=["provenance"])

# Provenance exposes raw claims, sources, and history - gate behind the same
# reader roles as the public projection view rather than leaving it open.
_READERS = (Role.ADMIN, Role.REVIEWER, Role.ANALYST)


@router.get("/{event_id}/provenance")
async def get_provenance(
    event_id: UUID,
    include_archive: bool = Query(default=False),
    canonicalize: bool = Query(
        default=True,
        description=(
            "When True (default), follows merged_into_event_id to the surviving event "
            "and returns its provenance.  Set False to read the absorbed event's own "
            "evidence trail (useful for audit / debugging)."
        ),
    ),
    limit: int = Query(
        default=DEFAULT_PROVENANCE_LIMIT,
        ge=1,
        le=MAX_PROVENANCE_LIMIT,
        description="Maximum rows returned per high-cardinality provenance section.",
    ),
    cursor: UUID | None = Query(
        default=None,
        description=(
            "Backward-compatible shorthand cursor for claim_histories. Prefer the "
            "section-specific cursors returned in pagination.next_cursors."
        ),
    ),
    claims_cursor: UUID | None = Query(default=None),
    claim_history_cursor: UUID | None = Query(default=None),
    conflicts_cursor: UUID | None = Query(default=None),
    conflict_activity_cursor: UUID | None = Query(default=None),
    projection_history_cursor: UUID | None = Query(default=None),
    uow: UnitOfWork = Depends(get_uow, scope="function"),
    _user: CurrentUser = Depends(require_role(*_READERS)),
) -> Response:
    try:
        payload = await QueryProvenance(uow).execute(
            event_id,
            include_archive,
            canonicalize,
            limit=limit,
            cursor=cursor,
            claims_cursor=claims_cursor,
            claim_history_cursor=claim_history_cursor,
            conflicts_cursor=conflicts_cursor,
            conflict_activity_cursor=conflict_activity_cursor,
            projection_history_cursor=projection_history_cursor,
        )
        await uow.rollback()
        return await offloaded_json_response(payload)
    except NotImplementedError as exc:
        raise HTTPException(status_code=501, detail=str(exc)) from exc
