"""Geospatial map bounded context (Phase 3).

A projection of the publication layer onto a spatial index, mirroring
the Phase 2 search bounded context.  Same isolation invariants apply:

- The map index contains exactly the set of PUBLISHED public event
  pages that have parseable coordinates.
- Phase 9's publication lifecycle hooks own writes (publish upserts,
  archive/retract delete).
- No tenant-private data enters the map index, by construction —
  it queries the public projection table, never the tenant tables.
"""

from __future__ import annotations

from atlas.domain.maps.entities import (
    MapBoundingBox,
    MapClusterCell,
    MapClusterResult,
    MapIndexEntry,
    MapPoint,
    MapQuery,
    MapSearchResult,
)
from atlas.domain.maps.exceptions import MapQueryMalformedError

__all__ = [
    "MapBoundingBox",
    "MapClusterCell",
    "MapClusterResult",
    "MapIndexEntry",
    "MapPoint",
    "MapQuery",
    "MapQueryMalformedError",
    "MapSearchResult",
]
