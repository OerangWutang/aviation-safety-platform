from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response

from atlas.application.dto import CurrentUser
from atlas.application.unit_of_work import UnitOfWork
from atlas.application.use_cases.query_accident import QueryAccidentPublicView
from atlas.application.use_cases.reproject_event import ReProjectEvent
from atlas.domain.enums import Role
from atlas.presentation.api.dependencies import get_uow, require_role
from atlas.presentation.api.responses import offloaded_json_response

router = APIRouter(prefix="/accidents", tags=["accidents"])

_READERS = (Role.ADMIN, Role.REVIEWER, Role.ANALYST)


@router.get("/{event_id}")
async def get_accident(
    event_id: UUID,
    uow: UnitOfWork = Depends(get_uow, scope="function"),
    _user: CurrentUser = Depends(require_role(*_READERS)),
) -> Response:
    projection = await QueryAccidentPublicView(uow).execute(event_id)
    if not projection:
        raise HTTPException(status_code=404, detail="Projected accident record not found")
    payload = projection.model_dump()
    await uow.rollback()
    return await offloaded_json_response(payload)


@router.post("/{event_id}/reproject")
async def reproject_event(
    event_id: UUID,
    uow: UnitOfWork = Depends(get_uow, scope="function"),
    _user: CurrentUser = Depends(require_role(Role.ADMIN)),
) -> dict:
    return (await ReProjectEvent(uow).execute(event_id)).model_dump()
