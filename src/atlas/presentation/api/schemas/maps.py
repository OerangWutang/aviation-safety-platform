"""Pydantic response schemas for the public map router (Phase 3).

Two response shapes: a point list and a cluster grid.  Same
``extra='forbid'`` whitelist contract as the rest of the public
surface so no internal-style fields leak into the wire format.
"""

from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class _MapModel(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)


class MapPointItem(_MapModel):
    """Single point on the map.

    ``page_id`` is included so a UI can deep-link to the public
    detail page; ``slug`` is included so the link can be rendered as
    a stable URL without a second lookup.
    """

    page_id: UUID
    slug: str
    title: str
    latitude: float
    longitude: float
    operator: str | None = None
    aircraft_type: str | None = None
    country: str | None = None
    event_date: date | None = None
    fatalities_total: int | None = None
    confidence_band: str
    last_published_at: datetime


class MapSearchResponse(_MapModel):
    items: list[MapPointItem]
    truncated: bool = Field(
        description=(
            "True when the query matched more than ``limit`` points. "
            "Callers should narrow the bounding box or apply filters "
            "to see the rest."
        )
    )
    limit: int


class MapClusterCellItem(_MapModel):
    """One cluster cell with count + centroid.

    The cell's bounding box (``cell_west``/``south``/``east``/``north``)
    lets a UI render the cell footprint faithfully; the centroid is
    the average of the points' coordinates so the marker sits where
    the data is densest within the cell, not at the geometric cell
    centre.
    """

    cell_west: float
    cell_south: float
    cell_east: float
    cell_north: float
    centroid_latitude: float
    centroid_longitude: float
    count: int


class MapClusterResponse(_MapModel):
    cells: list[MapClusterCellItem]
    truncated: bool
    cluster_precision: int
