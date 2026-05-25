"""Map domain entities and value objects.

Pure data shapes — no backend-specific behaviour.  The Postgres+PostGIS
backend reads/writes go through raw SQL in the repository.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from uuid import UUID

from atlas.domain.entities import DomainModel

# Hard caps on response sizes.  Bounded globally so an adversarial
# zoom-out request can't force the planner to materialise the entire
# world.
MAX_POINTS_PER_RESPONSE: int = 500
DEFAULT_POINTS_PER_RESPONSE: int = 200
MAX_CLUSTER_CELLS_PER_RESPONSE: int = 2000

# Cluster precision is the number of cells across the bounding box's
# longitude span.  16 is a comfortable default for a typical desktop
# viewport; higher values yield finer clusters.
DEFAULT_CLUSTER_PRECISION: int = 16
MIN_CLUSTER_PRECISION: int = 4
MAX_CLUSTER_PRECISION: int = 64


class MapIndexEntry(DomainModel):
    """One row of the materialised map index.

    Mirrors the columns in ``map_index_entries``.  The ``geom`` PostGIS
    column is opaque to the application layer; we always store
    coordinates as separate ``latitude``/``longitude`` floats on this
    entity and let the repository convert.
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
    indexed_at: datetime | None = None


@dataclass(frozen=True)
class MapBoundingBox:
    """A WGS84 bounding box.

    Conventionally ``south <= north``.  Longitude is allowed to wrap
    (``west > east`` denotes a box that crosses the antimeridian) —
    the repository handles the wrap by splitting into two boxes.

    Validation lives in ``__post_init__`` so callers can't construct
    an invalid box.
    """

    south: float
    west: float
    north: float
    east: float

    def __post_init__(self) -> None:
        from atlas.domain.maps.exceptions import MapQueryMalformedError

        if not (-90.0 <= self.south <= 90.0):
            raise MapQueryMalformedError(f"south latitude {self.south} out of range [-90, 90]")
        if not (-90.0 <= self.north <= 90.0):
            raise MapQueryMalformedError(f"north latitude {self.north} out of range [-90, 90]")
        if not (-180.0 <= self.west <= 180.0):
            raise MapQueryMalformedError(f"west longitude {self.west} out of range [-180, 180]")
        if not (-180.0 <= self.east <= 180.0):
            raise MapQueryMalformedError(f"east longitude {self.east} out of range [-180, 180]")
        if self.south > self.north:
            raise MapQueryMalformedError(f"south {self.south} must be <= north {self.north}")

    @property
    def crosses_antimeridian(self) -> bool:
        return self.west > self.east

    @property
    def longitude_span(self) -> float:
        """The width of the box in degrees, accounting for antimeridian wrap."""
        if self.crosses_antimeridian:
            return (180.0 - self.west) + (self.east - (-180.0))
        return self.east - self.west

    @property
    def latitude_span(self) -> float:
        return self.north - self.south


@dataclass(frozen=True)
class MapQuery:
    """A validated map query.

    Two response modes:

    - ``cluster=False`` returns individual points up to
      ``DEFAULT_POINTS_PER_RESPONSE`` (cap ``MAX_POINTS_PER_RESPONSE``).
    - ``cluster=True`` returns grid-cell aggregates up to
      ``MAX_CLUSTER_CELLS_PER_RESPONSE``.

    Filter facets mirror :class:`SearchQuery` for muscle-memory
    consistency: operator, aircraft_type, country, date range,
    fatalities range, confidence_bands.
    """

    bbox: MapBoundingBox
    operator: str | None = None
    aircraft_type: str | None = None
    country: str | None = None
    event_date_from: date | None = None
    event_date_to: date | None = None
    fatalities_min: int | None = None
    fatalities_max: int | None = None
    confidence_bands: frozenset[str] | None = None
    cluster: bool = False
    cluster_precision: int = DEFAULT_CLUSTER_PRECISION
    limit: int = DEFAULT_POINTS_PER_RESPONSE

    def __post_init__(self) -> None:
        from atlas.domain.maps.exceptions import MapQueryMalformedError

        if (
            self.event_date_from is not None
            and self.event_date_to is not None
            and self.event_date_from > self.event_date_to
        ):
            raise MapQueryMalformedError("event_date_from must be on or before event_date_to")
        if self.fatalities_min is not None and self.fatalities_min < 0:
            raise MapQueryMalformedError("fatalities_min must be >= 0")
        if (
            self.fatalities_min is not None
            and self.fatalities_max is not None
            and self.fatalities_min > self.fatalities_max
        ):
            raise MapQueryMalformedError("fatalities_min must be <= fatalities_max")
        if not (1 <= self.limit <= MAX_POINTS_PER_RESPONSE):
            raise MapQueryMalformedError(f"limit must be in [1, {MAX_POINTS_PER_RESPONSE}]")
        if not (MIN_CLUSTER_PRECISION <= self.cluster_precision <= MAX_CLUSTER_PRECISION):
            raise MapQueryMalformedError(
                f"cluster_precision must be in [{MIN_CLUSTER_PRECISION}, {MAX_CLUSTER_PRECISION}]"
            )
        if self.confidence_bands is not None:
            illegal = self.confidence_bands - {
                "high",
                "medium",
                "low",
                "unknown",
            }
            if illegal:
                raise MapQueryMalformedError(f"Unknown confidence band(s): {sorted(illegal)}")


class MapPoint(DomainModel):
    """A single point on the map."""

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


@dataclass(frozen=True)
class MapSearchResult:
    """Bounding-box list response."""

    items: list[MapPoint]
    truncated: bool
    limit: int


@dataclass(frozen=True)
class MapClusterCell:
    """One cluster cell in a grid-bucketed cluster response.

    ``centroid_*`` is the average of the points' coordinates inside
    the cell — easier for a UI than the cell's geometric centre,
    because it pulls toward the actual data.
    ``count`` is the number of points inside the cell.
    """

    cell_west: float
    cell_south: float
    cell_east: float
    cell_north: float
    centroid_latitude: float
    centroid_longitude: float
    count: int


@dataclass(frozen=True)
class MapClusterResult:
    """Cluster response."""

    cells: list[MapClusterCell]
    truncated: bool
    cluster_precision: int
