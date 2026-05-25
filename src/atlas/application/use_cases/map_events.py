"""Public map use cases (Phase 3).

Thin wrappers around the map repository.  All filter and pagination
validation lives in :class:`MapQuery`'s ``__post_init__``, so these
just compose the call.
"""

from __future__ import annotations

from atlas.application.unit_of_work import UnitOfWork
from atlas.domain.maps.entities import (
    MapClusterResult,
    MapQuery,
    MapSearchResult,
)


class SearchMapPoints:
    """Return individual points inside a bounding box."""

    def __init__(self, uow: UnitOfWork):
        self._uow = uow

    async def execute(self, query: MapQuery) -> MapSearchResult:
        result = await self._uow.maps.search_bbox(query)
        await self._uow.rollback()
        return result


class ClusterMapPoints:
    """Return grid-bucketed cluster cells inside a bounding box."""

    def __init__(self, uow: UnitOfWork):
        self._uow = uow

    async def execute(self, query: MapQuery) -> MapClusterResult:
        result = await self._uow.maps.cluster_bbox(query)
        await self._uow.rollback()
        return result
