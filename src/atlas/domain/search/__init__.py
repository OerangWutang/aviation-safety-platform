"""Search bounded context (Phase 2).

Search is intentionally a separate package from publication: the
search index is a *projection* of the publication layer (one-way data
flow from publication state changes into the index), not part of it.

What lives here:

- :class:`SearchIndexEntry`: the materialised index payload (one per
  PUBLISHED page).
- :class:`SearchQuery`: validated request shape used by both public
  and admin callers.
- :class:`SearchHit`: result row carrying rank + de-normalised facets.

The repository interface lives with the other repository protocols
in :mod:`atlas.domain.interfaces.repositories`.  Concrete backends
(Postgres FTS today, possibly OpenSearch later) implement that
interface; nothing in the domain layer should reach for a backend
type directly.
"""

from __future__ import annotations

from atlas.domain.search.entities import (
    SearchHit,
    SearchIndexEntry,
    SearchQuery,
    SearchResult,
)
from atlas.domain.search.exceptions import SearchQueryMalformedError

__all__ = [
    "SearchHit",
    "SearchIndexEntry",
    "SearchQuery",
    "SearchQueryMalformedError",
    "SearchResult",
]
