"""Public map router (Phase 3).

Two endpoints mounted under ``/api/v1/maps``:

- ``GET /events`` — points inside a bounding box (default mode).
- ``GET /events/cluster`` — grid-bucketed cluster cells inside the
  same bounding box.

A single endpoint with a ``cluster=true`` toggle would have been
slightly fewer routes, but two endpoints keep the response shape
documented separately in OpenAPI, which makes UI integration
easier.  Both endpoints share the same query parameter set so
callers can toggle between them without renaming params.
"""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, Query, Response

from atlas.application.dto import CurrentUser
from atlas.application.unit_of_work import UnitOfWork
from atlas.application.use_cases.map_events import (
    ClusterMapPoints,
    SearchMapPoints,
)
from atlas.domain.enums import Role
from atlas.domain.maps.entities import (
    DEFAULT_CLUSTER_PRECISION,
    DEFAULT_POINTS_PER_RESPONSE,
    MAX_CLUSTER_PRECISION,
    MAX_POINTS_PER_RESPONSE,
    MIN_CLUSTER_PRECISION,
    MapBoundingBox,
    MapQuery,
)
from atlas.presentation.api.dependencies import get_public_uow, require_role
from atlas.presentation.api.responses import offloaded_json_response
from atlas.presentation.api.schemas.maps import (
    MapClusterCellItem,
    MapClusterResponse,
    MapPointItem,
    MapSearchResponse,
)

router = APIRouter(prefix="/maps", tags=["maps"])

_READERS = (Role.ADMIN, Role.REVIEWER, Role.ANALYST)


def _build_query(
    south: float,
    west: float,
    north: float,
    east: float,
    operator: str | None,
    aircraft_type: str | None,
    country: str | None,
    event_date_from: date | None,
    event_date_to: date | None,
    fatalities_min: int | None,
    fatalities_max: int | None,
    confidence_bands: list[str] | None,
    *,
    cluster: bool,
    cluster_precision: int,
    limit: int,
) -> MapQuery:
    """Construct a validated :class:`MapQuery` from path params.

    Validation lives on :class:`MapBoundingBox.__post_init__` and
    :class:`MapQuery.__post_init__`; both raise
    :class:`MapQueryMalformedError` which surfaces as HTTP 422 via
    the generic ``DomainValidationError`` handler.
    """
    bbox = MapBoundingBox(south=south, west=west, north=north, east=east)
    return MapQuery(
        bbox=bbox,
        operator=operator,
        aircraft_type=aircraft_type,
        country=country,
        event_date_from=event_date_from,
        event_date_to=event_date_to,
        fatalities_min=fatalities_min,
        fatalities_max=fatalities_max,
        confidence_bands=frozenset(confidence_bands) if confidence_bands else None,
        cluster=cluster,
        cluster_precision=cluster_precision,
        limit=limit,
    )


@router.get("/events", response_model=MapSearchResponse)
async def map_events(
    south: float = Query(..., ge=-90.0, le=90.0),
    west: float = Query(..., ge=-180.0, le=180.0),
    north: float = Query(..., ge=-90.0, le=90.0),
    east: float = Query(..., ge=-180.0, le=180.0),
    operator: str | None = Query(default=None, max_length=300),
    aircraft_type: str | None = Query(default=None, max_length=300),
    country: str | None = Query(default=None, max_length=300),
    event_date_from: date | None = Query(default=None),
    event_date_to: date | None = Query(default=None),
    fatalities_min: int | None = Query(default=None, ge=0),
    fatalities_max: int | None = Query(default=None, ge=0),
    confidence_bands: list[str] | None = Query(default=None),
    limit: int = Query(
        default=DEFAULT_POINTS_PER_RESPONSE,
        ge=1,
        le=MAX_POINTS_PER_RESPONSE,
    ),
    uow: UnitOfWork = Depends(get_public_uow, scope="function"),
    _user: CurrentUser = Depends(require_role(*_READERS)),
) -> Response:
    query = _build_query(
        south,
        west,
        north,
        east,
        operator,
        aircraft_type,
        country,
        event_date_from,
        event_date_to,
        fatalities_min,
        fatalities_max,
        confidence_bands,
        cluster=False,
        cluster_precision=DEFAULT_CLUSTER_PRECISION,
        limit=limit,
    )
    result = await SearchMapPoints(uow).execute(query)
    payload = MapSearchResponse(
        items=[
            MapPointItem(
                page_id=p.page_id,
                slug=p.slug,
                title=p.title,
                latitude=p.latitude,
                longitude=p.longitude,
                operator=p.operator,
                aircraft_type=p.aircraft_type,
                country=p.country,
                event_date=p.event_date,
                fatalities_total=p.fatalities_total,
                confidence_band=p.confidence_band,
                last_published_at=p.last_published_at,
            )
            for p in result.items
        ],
        truncated=result.truncated,
        limit=result.limit,
    )
    return await offloaded_json_response(payload.model_dump(mode="json"))


@router.get("/events/cluster", response_model=MapClusterResponse)
async def map_events_cluster(
    south: float = Query(..., ge=-90.0, le=90.0),
    west: float = Query(..., ge=-180.0, le=180.0),
    north: float = Query(..., ge=-90.0, le=90.0),
    east: float = Query(..., ge=-180.0, le=180.0),
    cluster_precision: int = Query(
        default=DEFAULT_CLUSTER_PRECISION,
        ge=MIN_CLUSTER_PRECISION,
        le=MAX_CLUSTER_PRECISION,
        description=("Number of grid cells across the bounding box's longitude span."),
    ),
    operator: str | None = Query(default=None, max_length=300),
    aircraft_type: str | None = Query(default=None, max_length=300),
    country: str | None = Query(default=None, max_length=300),
    event_date_from: date | None = Query(default=None),
    event_date_to: date | None = Query(default=None),
    fatalities_min: int | None = Query(default=None, ge=0),
    fatalities_max: int | None = Query(default=None, ge=0),
    confidence_bands: list[str] | None = Query(default=None),
    uow: UnitOfWork = Depends(get_public_uow, scope="function"),
    _user: CurrentUser = Depends(require_role(*_READERS)),
) -> Response:
    query = _build_query(
        south,
        west,
        north,
        east,
        operator,
        aircraft_type,
        country,
        event_date_from,
        event_date_to,
        fatalities_min,
        fatalities_max,
        confidence_bands,
        cluster=True,
        cluster_precision=cluster_precision,
        limit=DEFAULT_POINTS_PER_RESPONSE,
    )
    result = await ClusterMapPoints(uow).execute(query)
    payload = MapClusterResponse(
        cells=[
            MapClusterCellItem(
                cell_west=c.cell_west,
                cell_south=c.cell_south,
                cell_east=c.cell_east,
                cell_north=c.cell_north,
                centroid_latitude=c.centroid_latitude,
                centroid_longitude=c.centroid_longitude,
                count=c.count,
            )
            for c in result.cells
        ],
        truncated=result.truncated,
        cluster_precision=result.cluster_precision,
    )
    return await offloaded_json_response(payload.model_dump(mode="json"))
