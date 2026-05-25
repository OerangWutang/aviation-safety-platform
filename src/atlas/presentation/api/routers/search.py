"""Public search router (Phase 2).

Single GET endpoint plus an admin reindex hook (mounted under the
admin prefix by ``app.py``).  Same auth shape as the rest of the
public surface: reader-or-higher for search, admin-only for reindex.
"""

from __future__ import annotations

from datetime import date
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response

from atlas.application.dto import CurrentUser
from atlas.application.unit_of_work import UnitOfWork
from atlas.application.use_cases.search_events import SearchPublicEvents
from atlas.domain.enums import Role
from atlas.domain.search.entities import (
    DEFAULT_SEARCH_LIMIT,
    MAX_QUERY_LENGTH,
    MAX_SEARCH_LIMIT,
    SearchQuery,
)
from atlas.presentation.api.dependencies import get_public_uow, require_role
from atlas.presentation.api.responses import offloaded_json_response
from atlas.presentation.api.schemas.search import SearchResponse, hits_to_response

router = APIRouter(prefix="/search", tags=["search"])

_READERS = (Role.ADMIN, Role.REVIEWER, Role.ANALYST)


# The query parameter names mirror :class:`SearchQuery` so that the
# OpenAPI surface and the domain model stay in lockstep.
@router.get("/events", response_model=SearchResponse)
async def search_events(
    q: str | None = Query(
        default=None,
        max_length=MAX_QUERY_LENGTH,
        description="Plain-text query.  Omit to list newest pages.",
    ),
    operator: str | None = Query(default=None, max_length=300),
    aircraft_type: str | None = Query(default=None, max_length=300),
    country: str | None = Query(default=None, max_length=300),
    event_date_from: date | None = Query(default=None),
    event_date_to: date | None = Query(default=None),
    fatalities_min: int | None = Query(default=None, ge=0),
    fatalities_max: int | None = Query(default=None, ge=0),
    confidence_bands: list[str] | None = Query(
        default=None,
        description=("Filter by one or more confidence bands: high, medium, low, unknown."),
    ),
    limit: int = Query(default=DEFAULT_SEARCH_LIMIT, ge=1, le=MAX_SEARCH_LIMIT),
    after_rank: float | None = Query(default=None),
    after_id: UUID | None = Query(default=None),
    debug_rank: bool = Query(
        default=False,
        description=(
            "Set to true to include the raw rank score in each hit. "
            "Reserved for index-tuning; absent by default so the "
            "public response shape stays stable across ranking tweaks."
        ),
    ),
    uow: UnitOfWork = Depends(get_public_uow, scope="function"),
    _user: CurrentUser = Depends(require_role(*_READERS)),
) -> Response:
    # Construct the domain query.  Validation happens inside
    # ``SearchQuery.__post_init__`` and surfaces as
    # ``SearchQueryMalformedError`` → 422 via the generic handler.
    query = SearchQuery(
        q=q,
        operator=operator,
        aircraft_type=aircraft_type,
        country=country,
        event_date_from=event_date_from,
        event_date_to=event_date_to,
        fatalities_min=fatalities_min,
        fatalities_max=fatalities_max,
        confidence_bands=frozenset(confidence_bands) if confidence_bands else None,
        limit=limit,
        after_rank=after_rank,
        after_id=after_id,
    )
    result = await SearchPublicEvents(uow).execute(query)
    payload = hits_to_response(result, include_rank=debug_rank)
    return await offloaded_json_response(payload)
