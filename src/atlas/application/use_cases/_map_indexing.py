"""Map-index lifecycle hooks driven by publication state changes (Phase 3).

The single rule enforced here:

    The map index contains exactly the set of PUBLISHED pages that
    have parseable coordinates.

Concretely:

- ``index_published_page_in_map`` upserts the entry for a page that
  just became PUBLISHED — if and only if the projection carries
  parseable lat/lng.
- ``remove_page_from_map`` deletes the entry for a page leaving
  PUBLISHED.

Both are called from the Phase 9 publication use cases inside the
same UoW as the state change, so a failed map write rolls back the
state transition (same pattern as Phase 2's search hook).

Coordinate canonicalisation
---------------------------

Projection JSONB fields may carry latitude/longitude under several
names depending on the source — ``latitude``/``longitude``,
``lat``/``lon``, ``lat``/``lng``.  We try each form once.  If none
parse, the page is *not* indexed and the publish proceeds.  Same
fail-soft philosophy as the search indexer.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from atlas.application.unit_of_work import UnitOfWork
from atlas.application.use_cases._search_indexing import (
    _coerce_int,
    _confidence_band,
    _parse_event_date,
    _str_or_none,
)
from atlas.domain.entities import ProjectedAccidentRecord
from atlas.domain.maps.entities import MapIndexEntry
from atlas.domain.publication.entities import PublicEventPage

logger = logging.getLogger(__name__)


def _coerce_float(value: Any) -> float | None:
    """Best-effort float coercion that fails to None.

    Strings come in from JSONB; ``bool`` is excluded explicitly
    because ``isinstance(True, int)`` is True and we don't want a
    ``True`` flag to be silently indexed as lat=1.0.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


_LAT_KEYS: tuple[str, ...] = ("latitude", "lat")
_LNG_KEYS: tuple[str, ...] = ("longitude", "lng", "lon")


def _extract_coords(
    fields: dict[str, Any],
) -> tuple[float, float] | None:
    """Return ``(latitude, longitude)`` if both are parseable.

    Tries the canonical-shape keys first, then common aliases.
    Validates that the parsed coordinates are inside global lat/lng
    bounds — out-of-range values are treated the same as missing
    values (the map index would otherwise contain phantom points at
    impossible locations).
    """
    lat: float | None = None
    for key in _LAT_KEYS:
        if key in fields:
            lat = _coerce_float(fields[key])
            if lat is not None:
                break
    lng: float | None = None
    for key in _LNG_KEYS:
        if key in fields:
            lng = _coerce_float(fields[key])
            if lng is not None:
                break
    if lat is None or lng is None:
        return None
    if not (-90.0 <= lat <= 90.0):
        return None
    if not (-180.0 <= lng <= 180.0):
        return None
    return lat, lng


def _map_entry_from(
    page: PublicEventPage,
    projection: ProjectedAccidentRecord | None,
) -> MapIndexEntry | None:
    """Build a MapIndexEntry from a page + projection, or None if
    the projection lacks parseable coordinates.

    Returning None is the *intended* signal that this page should
    not be in the map index.  Callers translate that into a no-op
    or a delete (when un-indexing a page whose coordinates have
    since become invalid).
    """
    fields = projection.fields if projection else {}
    coords = _extract_coords(fields)
    if coords is None:
        return None
    lat, lng = coords
    return MapIndexEntry(
        page_id=page.id,
        slug=page.slug,
        title=page.title,
        latitude=lat,
        longitude=lng,
        operator=_str_or_none(fields.get("operator")),
        aircraft_type=_str_or_none(fields.get("aircraft_type")),
        country=_str_or_none(fields.get("country")),
        event_date=_parse_event_date(fields.get("event_date")),
        fatalities_total=_coerce_int(fields.get("fatalities_total")),
        confidence_band=_confidence_band(projection),
        last_published_at=page.last_published_at or page.updated_at,
    )


async def index_published_page_in_map(uow: UnitOfWork, page: PublicEventPage) -> None:
    """Upsert the map index entry for a page that is currently PUBLISHED.

    The page may transition from "no coordinates" to "coordinates
    available" between publishes (an editor confirms a position from
    a new source).  We always re-evaluate: upsert when coordinates
    are present now; delete when they aren't.  That way the index
    is the deterministic projection of "currently PUBLISHED + has
    coordinates".
    """
    projection = await uow.projections.get(page.event_id)
    entry = _map_entry_from(page, projection)
    if entry is None:
        # Defensive delete: if the page had coordinates before and
        # has lost them, the index row should disappear.
        await uow.maps.delete(page.id)
        return
    await uow.maps.upsert(entry)


async def remove_page_from_map(uow: UnitOfWork, page_id: UUID) -> None:
    """Delete the map index entry for a page leaving PUBLISHED.

    Safe to call regardless of prior index presence.
    """
    await uow.maps.delete(page_id)
