"""SQLAlchemy/Postgres-FTS implementation of :class:`SearchRepository`.

This is the Phase 2 default backend.  The interface is small enough
that adding OpenSearch / Meilisearch / Typesense later is a single
new module — nothing in the application layer reaches for Postgres-
specific types.

Index construction
------------------

The ``search_vector`` is built at upsert time with weighted
``setweight`` calls:

- A : title — heaviest weight, matches dominate ranking.
- B : short_summary — editorial prose, important but secondary.
- C : projection facets (operator, aircraft_type, country) — these
      are evidence-backed and the most useful for filter-style
      queries that happen to contain the right token.
- D : narrative_markdown — long-form prose; matches contribute, but
      not enough to outrank a title hit.

Ranking
-------

``ts_rank_cd`` over the weighted vector with default normalization
weights.  Deterministic to two decimal places for the same query and
index state, which is what the regression tests pin.

Pagination
----------

Keyset over ``(rank DESC, page_id DESC)`` when a text query is
present.  Without a text query the rank is undefined, so the
"newest published" fallback orders by
``(last_published_at DESC, page_id DESC)`` — same cursor shape, but
rank is fixed at 0.0 so the comparison degenerates to id-only.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import bindparam, func, literal, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from atlas.domain.interfaces.repositories import SearchRepository
from atlas.domain.search.entities import (
    SearchHit,
    SearchIndexEntry,
    SearchQuery,
    SearchResult,
)
from atlas.infrastructure.db.orm_models import SearchIndexEntryModel


class SqlPostgresFtsSearchRepository(SearchRepository):
    def __init__(self, session: AsyncSession):
        self._session = session

    async def upsert(self, entry: SearchIndexEntry) -> None:
        # ``setweight`` builds the weighted tsvector at write time so
        # query-time ranking is deterministic.  ``to_tsvector('simple',
        # COALESCE(...))`` over each weighted slot keeps NULL fields
        # from poisoning the concatenation.
        title_expr = func.setweight(
            func.to_tsvector(literal("simple"), func.coalesce(bindparam("title"), literal(""))),
            literal("A"),
        )
        summary_expr = func.setweight(
            func.to_tsvector(
                literal("simple"), func.coalesce(bindparam("short_summary"), literal(""))
            ),
            literal("B"),
        )
        facets_expr = func.setweight(
            func.to_tsvector(
                literal("simple"),
                func.coalesce(bindparam("operator"), literal(""))
                .concat(literal(" "))
                .concat(func.coalesce(bindparam("aircraft_type"), literal("")))
                .concat(literal(" "))
                .concat(func.coalesce(bindparam("country"), literal(""))),
            ),
            literal("C"),
        )
        narrative_expr = func.setweight(
            func.to_tsvector(
                literal("simple"), func.coalesce(bindparam("narrative_markdown"), literal(""))
            ),
            literal("D"),
        )
        vector_expr = title_expr.concat(summary_expr).concat(facets_expr).concat(narrative_expr)

        stmt = pg_insert(SearchIndexEntryModel).values(
            page_id=entry.page_id,
            slug=entry.slug,
            title=entry.title,
            short_summary=entry.short_summary,
            operator=entry.operator,
            aircraft_type=entry.aircraft_type,
            country=entry.country,
            event_date=entry.event_date,
            fatalities_total=entry.fatalities_total,
            confidence_band=entry.confidence_band,
            last_published_at=entry.last_published_at,
            search_vector=vector_expr,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[SearchIndexEntryModel.page_id],
            set_={
                "slug": entry.slug,
                "title": entry.title,
                "short_summary": entry.short_summary,
                "operator": entry.operator,
                "aircraft_type": entry.aircraft_type,
                "country": entry.country,
                "event_date": entry.event_date,
                "fatalities_total": entry.fatalities_total,
                "confidence_band": entry.confidence_band,
                "last_published_at": entry.last_published_at,
                "search_vector": vector_expr,
                "indexed_at": func.now(),
            },
        )
        # bindparam values for the tsvector expression flow through
        # ``execute(params=)`` so SQLAlchemy resolves the duplicated
        # bind names cleanly.
        narrative = (
            # The index entry does not carry the narrative directly —
            # it lives only in ``public_event_pages`` and is included
            # by the upsert use case via the SearchIndexEntry's
            # ``short_summary`` already.  Phase 2 indexes title +
            # short_summary + projection facets only; the narrative
            # weight is reserved for the next iteration.
            None
        )
        await self._session.execute(
            stmt,
            {
                "title": entry.title,
                "short_summary": entry.short_summary,
                "operator": entry.operator,
                "aircraft_type": entry.aircraft_type,
                "country": entry.country,
                "narrative_markdown": narrative,
            },
        )

    async def delete(self, page_id: UUID) -> None:
        # No-op on missing row.  asyncpg returns rowcount=0; we don't
        # raise because callers (Archive/Retract) may run against a
        # page that was never published.
        from sqlalchemy import delete as sa_delete

        await self._session.execute(
            sa_delete(SearchIndexEntryModel).where(SearchIndexEntryModel.page_id == page_id)
        )

    async def search(self, query: SearchQuery) -> SearchResult:
        # Build the predicates incrementally so the same path serves
        # text and no-text queries.
        predicates: list[Any] = []

        # Text predicate + rank expression.
        if query.has_text_query:
            tsquery = func.plainto_tsquery(literal("simple"), bindparam("q"))
            predicates.append(SearchIndexEntryModel.search_vector.op("@@")(tsquery))
            rank_expr = func.ts_rank_cd(SearchIndexEntryModel.search_vector, tsquery).label("rank")
        else:
            # No text -> rank is the publication recency expressed as
            # an epoch float.  This keeps the (rank, page_id) cursor
            # shape uniform across text and no-text paths so callers
            # don't have to special-case pagination.
            rank_expr = func.extract("epoch", SearchIndexEntryModel.last_published_at).label("rank")

        if query.operator:
            predicates.append(SearchIndexEntryModel.operator == query.operator)
        if query.aircraft_type:
            predicates.append(SearchIndexEntryModel.aircraft_type == query.aircraft_type)
        if query.country:
            predicates.append(SearchIndexEntryModel.country == query.country)
        if query.event_date_from is not None:
            predicates.append(SearchIndexEntryModel.event_date >= query.event_date_from)
        if query.event_date_to is not None:
            predicates.append(SearchIndexEntryModel.event_date <= query.event_date_to)
        if query.fatalities_min is not None:
            predicates.append(SearchIndexEntryModel.fatalities_total >= query.fatalities_min)
        if query.fatalities_max is not None:
            predicates.append(SearchIndexEntryModel.fatalities_total <= query.fatalities_max)
        if query.confidence_bands is not None:
            predicates.append(
                SearchIndexEntryModel.confidence_band.in_(sorted(query.confidence_bands))
            )

        stmt = select(SearchIndexEntryModel, rank_expr)
        for pred in predicates:
            stmt = stmt.where(pred)

        # Ordering: (rank DESC, page_id DESC).
        #
        # We deliberately do *not* include last_published_at in the
        # sort key.  The cursor predicate compares (rank, page_id)
        # because rank already disambiguates at the page level given
        # the unique page_id tiebreaker.  Any extra sort term the
        # cursor doesn't know about would create skip/duplicate
        # hazards at page boundaries.
        stmt = stmt.order_by(
            rank_expr.desc(),
            SearchIndexEntryModel.page_id.desc(),
        )

        # Keyset cursor.  ``after_rank`` and ``after_id`` arrive
        # together (validated in SearchQuery).  The cursor predicate
        # is on (rank, page_id) — last_published_at is a tie-breaker
        # but not part of the cursor because rank already disambig-
        # uates at the page level.
        if query.after_rank is not None and query.after_id is not None:
            stmt = stmt.where(
                (rank_expr < literal(query.after_rank))
                | (
                    (rank_expr == literal(query.after_rank))
                    & (SearchIndexEntryModel.page_id < literal(query.after_id))
                )
            )

        stmt = stmt.limit(query.limit + 1)

        # Bind parameters used in the tsquery expression.
        params: dict[str, Any] = {}
        if query.has_text_query:
            # plainto_tsquery is the safe constructor — handles
            # tokenization without exposing the operator syntax to
            # untrusted input.
            params["q"] = query.q

        result = await self._session.execute(stmt, params)
        rows = list(result.all())
        truncated = len(rows) > query.limit
        if truncated:
            rows = rows[: query.limit]

        items = [_row_to_hit(row) for row in rows]
        next_rank: float | None = None
        next_id: UUID | None = None
        if truncated and items:
            last = items[-1]
            next_rank = last.rank
            next_id = last.page_id

        return SearchResult(
            items=items,
            next_cursor_rank=next_rank,
            next_cursor_id=next_id,
            limit=query.limit,
        )

    async def rebuild_all_from(self, entries: list[SearchIndexEntry]) -> int:
        # Atomic within the caller's transaction: clear, then upsert
        # every entry.  The whole operation rolls back if any single
        # upsert fails, so the index is never left half-rebuilt.
        from sqlalchemy import delete as sa_delete

        await self._session.execute(sa_delete(SearchIndexEntryModel))
        for entry in entries:
            await self.upsert(entry)
        return len(entries)


def _row_to_hit(row: Any) -> SearchHit:
    """Map a (model_instance, rank) Row to a SearchHit.

    ``rank`` arrives as the second tuple element via the labelled
    ``rank_expr`` in the select.  Defensive ``float()`` cast handles
    the Decimal-vs-float quirk asyncpg can produce on aggregate
    expressions.
    """
    model = row[0]
    rank = float(row[1] or 0.0)
    return SearchHit(
        page_id=model.page_id,
        slug=model.slug,
        title=model.title,
        short_summary=model.short_summary,
        operator=model.operator,
        aircraft_type=model.aircraft_type,
        country=model.country,
        event_date=model.event_date,
        fatalities_total=model.fatalities_total,
        confidence_band=model.confidence_band,
        last_published_at=model.last_published_at,
        rank=rank,
    )


# Re-bound so static analyzers don't strip the unused imports that
# remain useful as type-narrowing hints to readers.
_ = (date, datetime)
