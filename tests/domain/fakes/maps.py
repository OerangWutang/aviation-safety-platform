"""Fake map repository."""

from __future__ import annotations

from uuid import UUID

from atlas.domain.interfaces.repositories import (
    MapRepository,
)
from atlas.domain.maps.entities import (
    MAX_CLUSTER_CELLS_PER_RESPONSE,
    MapClusterCell,
    MapClusterResult,
    MapIndexEntry,
    MapPoint,
    MapQuery,
    MapSearchResult,
)
from tests.domain.fakes._store import (
    _MapStore,
)


class FakeMapRepository(MapRepository):
    """In-memory map index.

    Geometry is just a (lat, lng) pair on the entity; bbox predicates
    are plain comparisons.  Antimeridian-crossing boxes are handled
    in two halves, matching the SQL repo.

    Clustering uses the same grid math the SQL repo uses, so the
    cluster shape regression test pins behaviour that matches the
    production query at the cell level (modulo PostGIS rounding).
    """

    def __init__(self, s: _MapStore) -> None:
        self._s = s

    async def upsert(self, entry: MapIndexEntry) -> None:
        self._s.entries[entry.page_id] = entry.model_copy(deep=True)

    async def delete(self, page_id: UUID) -> None:
        self._s.entries.pop(page_id, None)

    async def search_bbox(self, query: MapQuery) -> MapSearchResult:
        candidates = [
            e
            for e in self._s.entries.values()
            if _in_bbox(e, query.bbox) and _matches_filters(e, query)
        ]
        # Same order as SQL: last_published_at DESC, page_id DESC.
        candidates.sort(
            key=lambda e: (e.last_published_at, e.page_id),
            reverse=True,
        )
        truncated = len(candidates) > query.limit
        items = [_entry_to_point(e) for e in candidates[: query.limit]]
        return MapSearchResult(items=items, truncated=truncated, limit=query.limit)

    async def cluster_bbox(self, query: MapQuery) -> MapClusterResult:
        bbox = query.bbox
        cell_w = bbox.longitude_span / query.cluster_precision
        cell_h = (
            bbox.latitude_span / query.cluster_precision
            if bbox.latitude_span > 0
            else (1.0 / query.cluster_precision)
        )

        # Group into cells.  Use the same shifted-lng trick the SQL
        # repo uses for antimeridian-crossing boxes so the cell
        # indices are monotonic.
        buckets: dict[tuple[int, int], list[MapIndexEntry]] = {}
        for entry in self._s.entries.values():
            if not _in_bbox(entry, bbox):
                continue
            if not _matches_filters(entry, query):
                continue
            lng = entry.longitude
            if bbox.crosses_antimeridian and lng < bbox.west:
                lng = lng + 360.0
            xi = int((lng - bbox.west) // cell_w)
            yi = int((entry.latitude - bbox.south) // cell_h)
            buckets.setdefault((xi, yi), []).append(entry)

        # Build cells, sort by count DESC, truncate.
        cells_list: list[MapClusterCell] = []
        for (xi, yi), members in buckets.items():
            avg_lng = sum(m.longitude for m in members) / len(members)
            avg_lat = sum(m.latitude for m in members) / len(members)
            cell_west = bbox.west + xi * cell_w
            cell_south = bbox.south + yi * cell_h
            cells_list.append(
                MapClusterCell(
                    cell_west=cell_west,
                    cell_south=cell_south,
                    cell_east=cell_west + cell_w,
                    cell_north=cell_south + cell_h,
                    centroid_latitude=avg_lat,
                    centroid_longitude=avg_lng,
                    count=len(members),
                )
            )
        cells_list.sort(key=lambda c: c.count, reverse=True)
        truncated = len(cells_list) > MAX_CLUSTER_CELLS_PER_RESPONSE
        return MapClusterResult(
            cells=cells_list[:MAX_CLUSTER_CELLS_PER_RESPONSE],
            truncated=truncated,
            cluster_precision=query.cluster_precision,
        )

    async def rebuild_all_from(self, entries: list[MapIndexEntry]) -> int:
        self._s.entries.clear()
        for e in entries:
            await self.upsert(e)
        return len(entries)


def _in_bbox(entry: MapIndexEntry, bbox) -> bool:
    """Bounding-box hit-test with antimeridian-aware longitude check."""
    if not (bbox.south <= entry.latitude <= bbox.north):
        return False
    if bbox.crosses_antimeridian:
        # Two halves: [west, 180] OR [-180, east].
        return entry.longitude >= bbox.west or entry.longitude <= bbox.east
    return bbox.west <= entry.longitude <= bbox.east


def _matches_filters(entry: MapIndexEntry, query: MapQuery) -> bool:
    if query.operator and entry.operator != query.operator:
        return False
    if query.aircraft_type and entry.aircraft_type != query.aircraft_type:
        return False
    if query.country and entry.country != query.country:
        return False
    if query.event_date_from and (
        entry.event_date is None or entry.event_date < query.event_date_from
    ):
        return False
    if query.event_date_to and (entry.event_date is None or entry.event_date > query.event_date_to):
        return False
    if query.fatalities_min is not None and (
        entry.fatalities_total is None or entry.fatalities_total < query.fatalities_min
    ):
        return False
    if query.fatalities_max is not None and (
        entry.fatalities_total is None or entry.fatalities_total > query.fatalities_max
    ):
        return False
    if query.confidence_bands is not None and entry.confidence_band not in query.confidence_bands:
        return False
    return True


def _entry_to_point(entry: MapIndexEntry) -> MapPoint:
    return MapPoint(
        page_id=entry.page_id,
        slug=entry.slug,
        title=entry.title,
        latitude=entry.latitude,
        longitude=entry.longitude,
        operator=entry.operator,
        aircraft_type=entry.aircraft_type,
        country=entry.country,
        event_date=entry.event_date,
        fatalities_total=entry.fatalities_total,
        confidence_band=entry.confidence_band,
        last_published_at=entry.last_published_at,
    )


# ── CMS fakes (Phase 10) ────────────────────────────────────────────────────
