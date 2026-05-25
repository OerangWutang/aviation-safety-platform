"""Admin full-reindex use case (Phase 2 + 3).

Walks every PUBLISHED public event page and rebuilds both the search
index (Phase 2) and the map index (Phase 3) from scratch.  Designed
for recovery from a schema change in the index payload, or to
bootstrap the indices after an out-of-band restore.

Bounded and synchronous.  For larger production scales a resumable,
batched, outbox-driven reindex is the natural follow-up; the
``_entry_from`` helpers in the indexing modules are the seam.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from atlas.application.unit_of_work import UnitOfWork
from atlas.application.use_cases._map_indexing import _map_entry_from
from atlas.application.use_cases._search_indexing import _entry_from
from atlas.domain.maps.entities import MapIndexEntry
from atlas.domain.publication.entities import PublicationStatus
from atlas.domain.search.entities import SearchIndexEntry

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReindexResult:
    """Returned to the caller so the operator can confirm completion.

    ``pages_reindexed`` is the search count (every PUBLISHED page).
    ``map_pages_reindexed`` is a subset — only pages with parseable
    coordinates appear in the map index.
    """

    pages_reindexed: int
    map_pages_reindexed: int


# Defensive ceiling on how many pages a single reindex call walks.
# Production systems have on the order of low thousands of PUBLISHED
# pages; if it ever crosses this, a paginated/resumable reindex is
# the right path, not raising this constant.
_MAX_REINDEX_PAGES = 50_000

# Per-page fetch size for the editorial-list walk.
_REINDEX_BATCH_SIZE = 100


class ReindexPublicEvents:
    """Rebuild the search and map indices from PUBLISHED pages.

    Iterates ``list_editorial`` with the PUBLISHED filter and a
    keyset cursor — same code path as the editorial UI, so the walk
    stays consistent.  Projections are fetched once per page and
    fed into both index builders.
    """

    def __init__(self, uow: UnitOfWork):
        self._uow = uow

    async def execute(self) -> ReindexResult:
        published_only = frozenset({PublicationStatus.PUBLISHED})
        search_entries: list[SearchIndexEntry] = []
        map_entries: list[MapIndexEntry] = []
        after_id = None
        while True:
            page = await self._uow.public_event_pages.list_editorial(
                statuses=published_only,
                limit=_REINDEX_BATCH_SIZE,
                after_id=after_id,
            )
            for row in page.items:
                projection = await self._uow.projections.get(row.event_id)
                search_entries.append(_entry_from(row, projection))
                map_entry = _map_entry_from(row, projection)
                if map_entry is not None:
                    map_entries.append(map_entry)
                if len(search_entries) >= _MAX_REINDEX_PAGES:
                    # Fail closed loudly rather than silently truncate
                    # the index.  Operators should re-run with a
                    # bigger ceiling (and a follow-up issue to make
                    # reindex resumable).
                    logger.error(
                        "Reindex aborted: exceeded %d-page ceiling.",
                        _MAX_REINDEX_PAGES,
                    )
                    raise RuntimeError(
                        f"Reindex exceeds {_MAX_REINDEX_PAGES}-page ceiling; "
                        f"resumable reindex required."
                    )
            if page.next_cursor is None:
                break
            after_id = page.next_cursor

        search_count = await self._uow.search.rebuild_all_from(search_entries)
        map_count = await self._uow.maps.rebuild_all_from(map_entries)
        await self._uow.commit()
        return ReindexResult(
            pages_reindexed=search_count,
            map_pages_reindexed=map_count,
        )
