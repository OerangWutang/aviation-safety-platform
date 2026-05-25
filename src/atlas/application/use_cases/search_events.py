"""Public search use case (Phase 2).

The use case is a thin wrapper around the search repository plus the
publication-layer validation pattern.  All filter and pagination
validation happens in :class:`SearchQuery`'s ``__post_init__``, so
this layer just composes the call.
"""

from __future__ import annotations

from atlas.application.unit_of_work import UnitOfWork
from atlas.domain.search.entities import SearchQuery, SearchResult


class SearchPublicEvents:
    """Run a validated search against the public search index.

    The search index intentionally contains only PUBLISHED public pages.
    Tenant-private overlays are served under the tenant-scoped prefix.
    """

    def __init__(self, uow: UnitOfWork):
        self._uow = uow

    async def execute(self, query: SearchQuery) -> SearchResult:
        result = await self._uow.search.search(query)
        # Read-only path — release the implicit transaction so the
        # connection returns to the pool quickly.
        await self._uow.rollback()
        return result
