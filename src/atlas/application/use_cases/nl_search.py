"""NL search orchestrator use cases (Phase 7).

The orchestrator composes the parser with existing infrastructure:

- Parsed structured filters dispatch into Phase 2's
  ``SearchRepository`` (text + facet filters).
- The free-text remainder also goes into Phase 2's FTS query so
  keyword coverage isn't lost.
- HFACS category filters intersect the result with attribution
  rows so "supervision failures in 2022" works without a
  bespoke join.

Phase 7 doesn't introduce a new index — it routes parsed filters
through the existing surfaces.  A future Phase 7.5 can swap the
parser for an LLM call without changing the orchestrator.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from atlas.application.services.metering import MeteringService
from atlas.application.services.nl_query_parser import (
    hour_bucket_for,
    parse_nl_query,
    query_hash_for,
)
from atlas.application.unit_of_work import UnitOfWork
from atlas.domain.metering.entities import MetricKind
from atlas.domain.nl_search.entities import (
    NlQueryLog,
    ParsedQuery,
    SavedNlQuery,
)
from atlas.domain.nl_search.exceptions import SavedNlQueryNotFoundError
from atlas.domain.search.entities import SearchHit, SearchQuery
from atlas.domain.utils import utc_now

# ── Execute NL search ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class NlSearchInput:
    raw_query: str
    limit: int = 25


@dataclass(frozen=True)
class NlSearchResult:
    """Composite return: structured echo + result items + parser
    confidence so the caller can decide whether to refine."""

    parsed: ParsedQuery
    items: list[SearchHit]
    total_estimated: int
    log_id: UUID


class ExecuteNlSearch:
    """Parse the query, dispatch into search, and log the call.

    Logging happens at the end so a failed parse or downstream
    query still produces a log row — the row carries
    ``result_count=0`` and the partial parse so analysts can debug
    why a query underperformed.
    """

    def __init__(self, uow: UnitOfWork):
        self._uow = uow

    async def execute(self, input: NlSearchInput) -> NlSearchResult:
        categories = await self._uow.hfacs_categories.list_all()
        parsed = parse_nl_query(input.raw_query, hfacs_categories=categories)

        # Compose a SearchQuery from the parsed filters.  Phase 2's
        # SearchQuery takes flat facet fields (operator, aircraft_type,
        # country) directly — no separate FacetFilter type.  The
        # remainder is passed as ``q`` for FTS; if the parser claimed
        # everything we leave ``q`` as None and let the facet filters
        # drive selection.
        #
        # We also convert the parser's fatal_only / non_fatal_only
        # booleans into a fatalities range — fatal_only means
        # min=1, non_fatal_only means max=0.  The explicit
        # fatalities_min / fatalities_max from the parser take
        # precedence if both are set.
        fatalities_min = parsed.fatalities_min
        fatalities_max = parsed.fatalities_max
        if parsed.fatal_only and fatalities_min is None:
            fatalities_min = 1
        if parsed.non_fatal_only and fatalities_max is None:
            fatalities_max = 0

        search_query = SearchQuery(
            q=parsed.free_text_remainder or None,
            operator=parsed.operator,
            aircraft_type=parsed.aircraft_type,
            country=parsed.country,
            event_date_from=parsed.event_date_from,
            event_date_to=parsed.event_date_to,
            fatalities_min=fatalities_min,
            fatalities_max=fatalities_max,
            limit=input.limit,
        )
        result = await self._uow.search.search(search_query)

        # If the parser identified HFACS category codes, filter the
        # result set down to events with at least one attribution
        # to those categories.  Out-of-band intersection is the
        # right shape here because Phase 2's search index isn't
        # joined to attributions.
        items = result.items
        if parsed.hfacs_category_codes:
            items = await self._intersect_with_hfacs(items, parsed.hfacs_category_codes)

        # Log the call.
        log_entry = NlQueryLog(
            raw_query=input.raw_query,
            query_hash=query_hash_for(input.raw_query),
            parsed_filters=parsed.model_dump(mode="json"),
            result_count=len(items),
            parser_confidence=parsed.confidence,
            hour_bucket=hour_bucket_for(utc_now()),
        )
        await self._uow.nl_query_log.add(log_entry)
        # Meter: one event per NL query executed.  System-wide
        # metric — no tenant scope (NL search is public-corpus).
        await MeteringService(self._uow).record(
            metric_kind=MetricKind.NL_QUERY_EXECUTED,
            tenant_id=None,
            user_id=None,
            resource_id=log_entry.id,
        )
        await self._uow.commit()

        return NlSearchResult(
            parsed=parsed,
            items=items,
            total_estimated=len(items),
            log_id=log_entry.id,
        )

    async def _intersect_with_hfacs(
        self,
        items: list[SearchHit],
        hfacs_codes: list[str],
    ) -> list[SearchHit]:
        """Keep only items whose event has an HFACS attribution to
        at least one of the matched categories.

        ``SearchHit`` carries ``page_id``, not ``event_id``, so we
        resolve through ``public_event_pages`` once per hit.  Cheap
        because the result set is already bounded by the Phase 2
        search limit.  For deeper queries (1000s of results), this
        should be pushed down to SQL — deferred.
        """
        all_cats = await self._uow.hfacs_categories.list_all()
        wanted_cat_ids = {c.id for c in all_cats if c.code in hfacs_codes}
        kept: list[SearchHit] = []
        for item in items:
            page = await self._uow.public_event_pages.get_by_id(item.page_id)
            if page is None:
                continue
            attributions = await self._uow.event_hfacs_attributions.list_for_event(page.event_id)
            if any(a.category_id in wanted_cat_ids for a in attributions):
                kept.append(item)
        return kept


# ── Saved queries ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SaveNlQueryInput:
    user_id: UUID
    label: str
    raw_query: str
    frozen_filters: dict[str, Any]


class SaveNlQuery:
    def __init__(self, uow: UnitOfWork):
        self._uow = uow

    async def execute(self, input: SaveNlQueryInput) -> SavedNlQuery:
        saved = SavedNlQuery(
            user_id=input.user_id,
            label=input.label,
            raw_query=input.raw_query,
            frozen_filters=input.frozen_filters,
        )
        await self._uow.saved_nl_queries.add(saved)
        await self._uow.commit()
        return saved


class ListSavedNlQueries:
    def __init__(self, uow: UnitOfWork):
        self._uow = uow

    async def execute(self, user_id: UUID, *, limit: int = 50) -> list[SavedNlQuery]:
        result = await self._uow.saved_nl_queries.list_for_user(user_id, limit=limit)
        await self._uow.rollback()
        return result


class DeleteSavedNlQuery:
    def __init__(self, uow: UnitOfWork):
        self._uow = uow

    async def execute(self, *, saved_id: UUID, user_id: UUID) -> None:
        deleted = await self._uow.saved_nl_queries.delete_for_user(
            saved_id=saved_id, user_id=user_id
        )
        if not deleted:
            raise SavedNlQueryNotFoundError(
                f"Saved NL query {saved_id} not found for user {user_id}"
            )
        await self._uow.commit()


__all__ = [
    "DeleteSavedNlQuery",
    "ExecuteNlSearch",
    "ListSavedNlQueries",
    "NlSearchInput",
    "NlSearchResult",
    "SaveNlQuery",
    "SaveNlQueryInput",
]
