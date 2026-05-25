"""Fake search index repository."""

from __future__ import annotations

from uuid import UUID

from atlas.domain.interfaces.repositories import (
    SearchRepository,
)
from atlas.domain.search.entities import (
    SearchHit,
    SearchIndexEntry,
    SearchQuery,
    SearchResult,
)
from tests.domain.fakes._store import (
    _SearchStore,
)


class FakeSearchRepository(SearchRepository):
    """In-memory search backend with a token-set match.

    Not a faithful FTS implementation — it ranks by token-overlap
    rather than ts_rank_cd — but the goal here is to verify the use-
    case behaviour (filter composition, publication lifecycle drives
    upserts/deletes, cursor pagination shape).  The Postgres FTS
    backend has its own integration test path.

    Ranking model: title hits weighted 4x, summary 2x, facets 1x.
    The exact numbers don't matter as long as ordering is stable
    enough for the regression tests.
    """

    def __init__(self, s: _SearchStore) -> None:
        self._s = s

    async def upsert(self, entry: SearchIndexEntry) -> None:
        # Defensive deep copy — same semantics as the publication
        # fake.  Without it, callers mutating the entry post-upsert
        # would silently mutate the "stored" row.
        self._s.entries[entry.page_id] = entry.model_copy(deep=True)

    async def delete(self, page_id: UUID) -> None:
        self._s.entries.pop(page_id, None)

    async def search(self, query: SearchQuery) -> SearchResult:
        # Compute (rank, entry) tuples, filter, then sort.
        scored: list[tuple[float, SearchIndexEntry]] = []
        for entry in self._s.entries.values():
            if not _matches_filters(entry, query):
                continue
            if query.has_text_query:
                rank = _score(entry, query)
                if rank <= 0.0:
                    continue
            else:
                # Mirror the SQL backend: no-text rank is the
                # publication epoch so the (rank, page_id) cursor
                # shape is uniform across query modes.
                rank = entry.last_published_at.timestamp()
            scored.append((rank, entry))

        # (rank DESC, page_id DESC).  We deliberately do *not* include
        # last_published_at in the sort key: the cursor predicate
        # operates over (rank, page_id), and any extra ordering term
        # the cursor doesn't know about creates skip/duplicate hazards
        # at page boundaries.  page_id is already a stable unique
        # tiebreaker.
        scored.sort(
            key=lambda r: (r[0], r[1].page_id),
            reverse=True,
        )

        # Cursor: drop everything at or before (after_rank, after_id)
        # in the sort order.
        if query.after_rank is not None and query.after_id is not None:
            cursor_key = (query.after_rank, query.after_id)
            scored = [
                (r, e)
                for r, e in scored
                if (r < cursor_key[0]) or (r == cursor_key[0] and e.page_id < cursor_key[1])
            ]

        truncated = len(scored) > query.limit
        scored = scored[: query.limit]
        items = [
            SearchHit(
                page_id=e.page_id,
                slug=e.slug,
                title=e.title,
                short_summary=e.short_summary,
                operator=e.operator,
                aircraft_type=e.aircraft_type,
                country=e.country,
                event_date=e.event_date,
                fatalities_total=e.fatalities_total,
                confidence_band=e.confidence_band,
                last_published_at=e.last_published_at,
                rank=r,
            )
            for r, e in scored
        ]
        next_rank: float | None = None
        next_id: UUID | None = None
        if truncated and items:
            next_rank = items[-1].rank
            next_id = items[-1].page_id
        return SearchResult(
            items=items,
            next_cursor_rank=next_rank,
            next_cursor_id=next_id,
            limit=query.limit,
        )

    async def rebuild_all_from(self, entries: list[SearchIndexEntry]) -> int:
        self._s.entries.clear()
        for e in entries:
            await self.upsert(e)
        return len(entries)


def _matches_filters(entry: SearchIndexEntry, query: SearchQuery) -> bool:
    """Apply the non-text filter predicates."""
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


def _score(entry: SearchIndexEntry, query: SearchQuery) -> float:
    """Cheap token-overlap score; mirrors the weighting of the SQL repo.

    Used only by the fake.  Real ranking is ``ts_rank_cd`` in
    Postgres.  We agree on the weighting *order* (title > summary >
    facets) so behavioural assertions in use-case tests (a title
    match outranks a summary match) hold against both backends.
    """
    assert query.q is not None
    needles = {t for t in query.q.lower().split() if t}
    if not needles:
        return 0.0
    title_tokens = set(entry.title.lower().split())
    summary_tokens = set(entry.short_summary.lower().split()) if entry.short_summary else set()
    facet_text = " ".join(
        x for x in (entry.operator, entry.aircraft_type, entry.country) if x
    ).lower()
    facet_tokens = set(facet_text.split())
    return (
        4.0 * len(needles & title_tokens)
        + 2.0 * len(needles & summary_tokens)
        + 1.0 * len(needles & facet_tokens)
    )


# ── Tenancy fakes (Phase 5) ─────────────────────────────────────────────────
