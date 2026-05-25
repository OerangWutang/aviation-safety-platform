"""PostGIS-backed map repository (Phase 3).

The ``geom`` column on ``map_index_entries`` is
``geography(Point, 4326)`` at the database level.  All PostGIS
expressions in this module go through ``sqlalchemy.text()`` rather
than via GeoAlchemy2 — the latter would add a build dependency and
the SQL footprint here is small enough that hand-rolled text is
clearer.

Read shapes:

- :meth:`search_bbox` returns individual rows inside a bounding box
  ordered by ``last_published_at DESC, page_id DESC``.  Over-fetches
  one row to surface a ``truncated`` flag without a separate count.
- :meth:`cluster_bbox` does grid-bucketed aggregation: divide the
  bounding box into ``cluster_precision`` cells per longitude span,
  group points by cell, return per-cell count + centroid.

Both reads honour the same filter facets exposed on
:class:`SearchQuery` so the map and search surfaces feel consistent.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from sqlalchemy import delete as sa_delete
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from atlas.domain.interfaces.repositories import MapRepository
from atlas.domain.maps.entities import (
    MAX_CLUSTER_CELLS_PER_RESPONSE,
    MapBoundingBox,
    MapClusterCell,
    MapClusterResult,
    MapIndexEntry,
    MapPoint,
    MapQuery,
    MapSearchResult,
)
from atlas.infrastructure.db.orm_models import MapIndexEntryModel

logger = logging.getLogger(__name__)


class SqlPostGisMapRepository(MapRepository):
    def __init__(self, session: AsyncSession):
        self._session = session

    async def upsert(self, entry: MapIndexEntry) -> None:
        # Single text() statement so the PostGIS geom expression is
        # server-evaluated.  Using pg_insert() here would force us to
        # plumb the GEOGRAPHY type through SQLAlchemy's typing
        # layer, which doesn't pay off for a one-shape table.
        sql = text(
            """
            INSERT INTO map_index_entries (
                page_id, slug, title, operator, aircraft_type, country,
                event_date, fatalities_total, confidence_band,
                last_published_at, indexed_at, geom
            ) VALUES (
                :page_id, :slug, :title, :operator, :aircraft_type, :country,
                :event_date, :fatalities_total, :confidence_band,
                :last_published_at, now(),
                ST_SetSRID(ST_MakePoint(:lng, :lat), 4326)::geography
            )
            ON CONFLICT (page_id) DO UPDATE SET
                slug = EXCLUDED.slug,
                title = EXCLUDED.title,
                operator = EXCLUDED.operator,
                aircraft_type = EXCLUDED.aircraft_type,
                country = EXCLUDED.country,
                event_date = EXCLUDED.event_date,
                fatalities_total = EXCLUDED.fatalities_total,
                confidence_band = EXCLUDED.confidence_band,
                last_published_at = EXCLUDED.last_published_at,
                indexed_at = now(),
                geom = EXCLUDED.geom
            """
        )
        await self._session.execute(
            sql,
            {
                "page_id": entry.page_id,
                "slug": entry.slug,
                "title": entry.title,
                "operator": entry.operator,
                "aircraft_type": entry.aircraft_type,
                "country": entry.country,
                "event_date": entry.event_date,
                "fatalities_total": entry.fatalities_total,
                "confidence_band": entry.confidence_band,
                "last_published_at": entry.last_published_at,
                "lng": entry.longitude,
                "lat": entry.latitude,
            },
        )

    async def delete(self, page_id: UUID) -> None:
        await self._session.execute(
            sa_delete(MapIndexEntryModel).where(MapIndexEntryModel.page_id == page_id)
        )

    async def search_bbox(self, query: MapQuery) -> MapSearchResult:
        bbox_sql, bbox_params = _bbox_predicate_sql(query.bbox)
        filters_sql, filter_params = _filters_predicate_sql(query)

        sql = text(
            f"""
            SELECT
                page_id, slug, title, operator, aircraft_type, country,
                event_date, fatalities_total, confidence_band,
                last_published_at,
                ST_X(geom::geometry) AS lng,
                ST_Y(geom::geometry) AS lat
            FROM map_index_entries
            WHERE {bbox_sql}{filters_sql}
            ORDER BY last_published_at DESC, page_id DESC
            LIMIT :_limit
            """
        )
        result = await self._session.execute(
            sql,
            {
                **bbox_params,
                **filter_params,
                "_limit": query.limit + 1,
            },
        )
        rows = list(result.mappings())
        truncated = len(rows) > query.limit
        if truncated:
            rows = rows[: query.limit]

        items = [
            MapPoint(
                page_id=r["page_id"],
                slug=r["slug"],
                title=r["title"],
                latitude=float(r["lat"]),
                longitude=float(r["lng"]),
                operator=r["operator"],
                aircraft_type=r["aircraft_type"],
                country=r["country"],
                event_date=r["event_date"],
                fatalities_total=r["fatalities_total"],
                confidence_band=r["confidence_band"],
                last_published_at=r["last_published_at"],
            )
            for r in rows
        ]
        return MapSearchResult(items=items, truncated=truncated, limit=query.limit)

    async def cluster_bbox(self, query: MapQuery) -> MapClusterResult:
        bbox = query.bbox
        cell_w = bbox.longitude_span / query.cluster_precision
        cell_h = (
            bbox.latitude_span / query.cluster_precision
            if bbox.latitude_span > 0
            else (1.0 / query.cluster_precision)
        )

        bbox_sql, bbox_params = _bbox_predicate_sql(bbox)
        filters_sql, filter_params = _filters_predicate_sql(query)

        # Antimeridian-crossing boxes: shift negative lngs into
        # [180, 360) so FLOOR() over the cell width stays monotonic
        # across the wrap.
        if bbox.crosses_antimeridian:
            shifted_lng = (
                "CASE WHEN ST_X(geom::geometry) < :_bbox_west "
                "THEN ST_X(geom::geometry) + 360.0 "
                "ELSE ST_X(geom::geometry) END"
            )
        else:
            shifted_lng = "ST_X(geom::geometry)"

        sql = text(
            f"""
            SELECT
                FLOOR(({shifted_lng} - :_bbox_west) / :_cell_w)::int AS xi,
                FLOOR(
                    (ST_Y(geom::geometry) - :_bbox_south) / :_cell_h
                )::int AS yi,
                AVG(ST_X(geom::geometry)) AS centroid_lng,
                AVG(ST_Y(geom::geometry)) AS centroid_lat,
                COUNT(*) AS cnt
            FROM map_index_entries
            WHERE {bbox_sql}{filters_sql}
            GROUP BY xi, yi
            ORDER BY cnt DESC
            LIMIT :_cell_cap
            """
        )
        result = await self._session.execute(
            sql,
            {
                **bbox_params,
                **filter_params,
                "_bbox_west": bbox.west,
                "_bbox_south": bbox.south,
                "_cell_w": cell_w,
                "_cell_h": cell_h,
                "_cell_cap": MAX_CLUSTER_CELLS_PER_RESPONSE + 1,
            },
        )
        rows = list(result.mappings())
        truncated = len(rows) > MAX_CLUSTER_CELLS_PER_RESPONSE
        if truncated:
            rows = rows[:MAX_CLUSTER_CELLS_PER_RESPONSE]

        cells: list[MapClusterCell] = []
        for r in rows:
            xi = int(r["xi"])
            yi = int(r["yi"])
            cell_west = bbox.west + xi * cell_w
            cell_south = bbox.south + yi * cell_h
            cells.append(
                MapClusterCell(
                    cell_west=cell_west,
                    cell_south=cell_south,
                    cell_east=cell_west + cell_w,
                    cell_north=cell_south + cell_h,
                    centroid_latitude=float(r["centroid_lat"]),
                    centroid_longitude=float(r["centroid_lng"]),
                    count=int(r["cnt"]),
                )
            )
        return MapClusterResult(
            cells=cells,
            truncated=truncated,
            cluster_precision=query.cluster_precision,
        )

    async def rebuild_all_from(self, entries: list[MapIndexEntry]) -> int:
        await self._session.execute(sa_delete(MapIndexEntryModel))
        for entry in entries:
            await self.upsert(entry)
        return len(entries)


# ── Predicate-fragment builders ─────────────────────────────────────────────


def _bbox_predicate_sql(
    bbox: MapBoundingBox,
) -> tuple[str, dict[str, Any]]:
    """Bounding-box SQL fragment + named parameters.

    Antimeridian-crossing boxes split into two ``ST_Intersects``
    calls OR'd together; either half hits the GiST index.
    """
    if bbox.crosses_antimeridian:
        return (
            "(ST_Intersects(geom::geometry, ST_MakeEnvelope("
            ":_bbox_west, :_bbox_south, 180.0, :_bbox_north, 4326))"
            " OR ST_Intersects(geom::geometry, ST_MakeEnvelope("
            "-180.0, :_bbox_south, :_bbox_east, :_bbox_north, 4326)))",
            {
                "_bbox_west": bbox.west,
                "_bbox_south": bbox.south,
                "_bbox_east": bbox.east,
                "_bbox_north": bbox.north,
            },
        )
    return (
        "ST_Intersects(geom::geometry, ST_MakeEnvelope("
        ":_bbox_west, :_bbox_south, :_bbox_east, :_bbox_north, 4326))",
        {
            "_bbox_west": bbox.west,
            "_bbox_south": bbox.south,
            "_bbox_east": bbox.east,
            "_bbox_north": bbox.north,
        },
    )


def _filters_predicate_sql(
    query: MapQuery,
) -> tuple[str, dict[str, Any]]:
    """Build AND-fragment for query filters.  Returns ``("", {})``
    when no filters are set so the caller can splice cleanly into
    the WHERE clause."""
    clauses: list[str] = []
    params: dict[str, Any] = {}
    if query.operator:
        clauses.append("operator = :_f_operator")
        params["_f_operator"] = query.operator
    if query.aircraft_type:
        clauses.append("aircraft_type = :_f_aircraft_type")
        params["_f_aircraft_type"] = query.aircraft_type
    if query.country:
        clauses.append("country = :_f_country")
        params["_f_country"] = query.country
    if query.event_date_from is not None:
        clauses.append("event_date >= :_f_date_from")
        params["_f_date_from"] = query.event_date_from
    if query.event_date_to is not None:
        clauses.append("event_date <= :_f_date_to")
        params["_f_date_to"] = query.event_date_to
    if query.fatalities_min is not None:
        clauses.append("fatalities_total >= :_f_fat_min")
        params["_f_fat_min"] = query.fatalities_min
    if query.fatalities_max is not None:
        clauses.append("fatalities_total <= :_f_fat_max")
        params["_f_fat_max"] = query.fatalities_max
    if query.confidence_bands is not None:
        # Explicit IN-list expansion.  asyncpg expects a tuple here,
        # and SQLAlchemy handles the rewrite via ``expanding=True``
        # binding when ``IN`` is used with a Python sequence.
        clauses.append("confidence_band IN :_f_confidence_bands")
        params["_f_confidence_bands"] = tuple(sorted(query.confidence_bands))
    if not clauses:
        return "", {}
    return " AND " + " AND ".join(clauses), params
