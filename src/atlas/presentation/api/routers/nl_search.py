"""NL search router (Phase 7).

Reader-gated for all endpoints.  Saved queries are per-user, scoped
by the caller's user_id.

NOTE: This router intentionally uses ``get_uow`` (system engine) rather than
``get_public_uow``.  Every NL search request writes a ``nl_query_log`` entry
and may write ``saved_nl_queries`` rows — both live in the system DB, not the
public read-only DB.  Splitting into two sessions per request (one public read,
one system write) is a future hardening step once query-logging is factored out
into a background task or a separate write path.

Four endpoints:

- ``POST /api/v1/search/nl`` — execute an NL query.
- ``POST /api/v1/search/nl/saved`` — pin a saved query.
- ``GET /api/v1/search/nl/saved`` — list the caller's saved
  queries (recency-ordered).
- ``DELETE /api/v1/search/nl/saved/{saved_id}`` — remove a saved
  query.  Cross-user delete returns 404 — we don't leak the
  existence of another user's saved queries.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Response

from atlas.application.dto import CurrentUser
from atlas.application.unit_of_work import UnitOfWork
from atlas.application.use_cases.nl_search import (
    DeleteSavedNlQuery,
    ExecuteNlSearch,
    ListSavedNlQueries,
    NlSearchInput,
    SaveNlQuery,
    SaveNlQueryInput,
)
from atlas.domain.enums import Role
from atlas.presentation.api.dependencies import get_uow, require_role
from atlas.presentation.api.responses import offloaded_json_response
from atlas.presentation.api.schemas.nl_search import (
    NlSearchHitItem,
    NlSearchRequest,
    NlSearchResponse,
    ParsedQueryItem,
    SavedNlQueryItem,
    SavedNlQueryListResponse,
    SaveNlQueryRequest,
)

router = APIRouter(prefix="/search/nl", tags=["nl-search"])

# Reader-gated for both reads and the search execution itself.
# Saving a query also requires read access — nothing here writes
# to the public corpus.
_READERS = (Role.ADMIN, Role.REVIEWER, Role.ANALYST)


def _parsed_to_item(parsed) -> ParsedQueryItem:
    return ParsedQueryItem(
        operator=parsed.operator,
        aircraft_type=parsed.aircraft_type,
        country=parsed.country,
        event_date_from=parsed.event_date_from,
        event_date_to=parsed.event_date_to,
        fatalities_min=parsed.fatalities_min,
        fatalities_max=parsed.fatalities_max,
        fatal_only=parsed.fatal_only,
        non_fatal_only=parsed.non_fatal_only,
        hfacs_category_codes=parsed.hfacs_category_codes,
        shelo_factor_classes=parsed.shelo_factor_classes,
        free_text_remainder=parsed.free_text_remainder,
        confidence=parsed.confidence,
    )


@router.post("", response_model=NlSearchResponse)
async def execute_nl_search(
    request: NlSearchRequest,
    uow: UnitOfWork = Depends(get_uow, scope="function"),
    _user: CurrentUser = Depends(require_role(*_READERS)),
) -> Response:
    result = await ExecuteNlSearch(uow).execute(
        NlSearchInput(raw_query=request.query, limit=request.limit)
    )
    payload = NlSearchResponse(
        parsed=_parsed_to_item(result.parsed),
        items=[
            NlSearchHitItem(
                page_id=h.page_id,
                slug=h.slug,
                title=h.title,
                short_summary=h.short_summary,
                operator=h.operator,
                aircraft_type=h.aircraft_type,
                country=h.country,
                event_date=h.event_date,
                fatalities_total=h.fatalities_total,
                confidence_band=h.confidence_band,
                last_published_at=h.last_published_at,
            )
            for h in result.items
        ],
        total_estimated=result.total_estimated,
        log_id=result.log_id,
    )
    return await offloaded_json_response(payload.model_dump(mode="json"))


@router.post("/saved", response_model=SavedNlQueryItem, status_code=201)
async def save_nl_query(
    request: SaveNlQueryRequest,
    uow: UnitOfWork = Depends(get_uow, scope="function"),
    user: CurrentUser = Depends(require_role(*_READERS)),
) -> Response:
    saved = await SaveNlQuery(uow).execute(
        SaveNlQueryInput(
            user_id=user.user_id,
            label=request.label,
            raw_query=request.raw_query,
            frozen_filters=request.frozen_filters,
        )
    )
    payload = SavedNlQueryItem(
        id=saved.id,
        user_id=saved.user_id,
        label=saved.label,
        raw_query=saved.raw_query,
        frozen_filters=saved.frozen_filters,
        created_at=saved.created_at,
    )
    return await offloaded_json_response(payload.model_dump(mode="json"), status_code=201)


@router.get("/saved", response_model=SavedNlQueryListResponse)
async def list_saved_nl_queries(
    uow: UnitOfWork = Depends(get_uow, scope="function"),
    user: CurrentUser = Depends(require_role(*_READERS)),
) -> Response:
    items = await ListSavedNlQueries(uow).execute(user.user_id)
    payload = SavedNlQueryListResponse(
        items=[
            SavedNlQueryItem(
                id=s.id,
                user_id=s.user_id,
                label=s.label,
                raw_query=s.raw_query,
                frozen_filters=s.frozen_filters,
                created_at=s.created_at,
            )
            for s in items
        ]
    )
    return await offloaded_json_response(payload.model_dump(mode="json"))


@router.delete("/saved/{saved_id}", status_code=204)
async def delete_saved_nl_query(
    saved_id: UUID,
    uow: UnitOfWork = Depends(get_uow, scope="function"),
    user: CurrentUser = Depends(require_role(*_READERS)),
) -> Response:
    await DeleteSavedNlQuery(uow).execute(saved_id=saved_id, user_id=user.user_id)
    return Response(status_code=204)
