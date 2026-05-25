"""Search domain entities and value objects.

These are pure data shapes — no backend-specific behaviour leaks
here.  The Postgres FTS backend constructs ``tsvector`` strings from
the index entry's fields at write time; OpenSearch (if we ever add
it) would map the same fields onto an analyzer pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from uuid import UUID

from pydantic import Field, model_validator

from atlas.domain.entities import DomainModel


class SearchIndexEntry(DomainModel):
    """One row of the search index.

    Mirrors the materialised columns in ``search_index_entries``.
    The ``search_vector`` column is *not* exposed here — it is opaque
    to the application layer and set via a SQL expression by the
    repository.

    ``confidence_band`` is the coarse public-facing label, pre-computed
    at index time so the search query plan doesn't need to evaluate
    the underlying ``completeness_score`` thresholds.
    """

    page_id: UUID
    slug: str
    title: str
    short_summary: str | None = None
    operator: str | None = None
    aircraft_type: str | None = None
    country: str | None = None
    event_date: date | None = None
    fatalities_total: int | None = None
    confidence_band: str
    last_published_at: datetime
    indexed_at: datetime | None = None


# Maximum number of hits returned in a single response.  Bounded
# globally so an attacker cannot force an expensive query plan by
# passing a huge limit.
MAX_SEARCH_LIMIT: int = 100
DEFAULT_SEARCH_LIMIT: int = 25

# Maximum raw query length.  Keeps the tsquery expression bounded and
# protects against pathological inputs.
MAX_QUERY_LENGTH: int = 200


@dataclass(frozen=True)
class SearchQuery:
    """Validated public search request.

    Construction validates ranges (date range, fatalities range,
    limit, query length) and normalises optional facets.  The Phase 2
    Postgres FTS backend reads these straight; future backends can do
    the same.

    Filter set
    ----------

    Phase 2 ships the deterministic-text-keyable filter subset:
    text, operator, aircraft_type, country, date range, fatalities
    range, and confidence band.  The remaining filters from the spec
    (phase of flight, occurrence category, source type, investigation
    status) are intentionally deferred — they require either Orion-
    relationship indexing or new projection fields, both of which
    belong to later phases.
    """

    q: str | None = None
    operator: str | None = None
    aircraft_type: str | None = None
    country: str | None = None
    event_date_from: date | None = None
    event_date_to: date | None = None
    fatalities_min: int | None = None
    fatalities_max: int | None = None
    confidence_bands: frozenset[str] | None = None
    limit: int = DEFAULT_SEARCH_LIMIT
    after_rank: float | None = None
    after_id: UUID | None = None

    def __post_init__(self) -> None:
        # All validation lives here so callers can't construct an
        # invalid SearchQuery.  The exception types are imported lazily
        # to keep this module's import graph minimal.
        from atlas.domain.search.exceptions import SearchQueryMalformedError

        if self.q is not None and len(self.q) > MAX_QUERY_LENGTH:
            raise SearchQueryMalformedError(f"Query text exceeds {MAX_QUERY_LENGTH} characters")
        if (
            self.event_date_from is not None
            and self.event_date_to is not None
            and self.event_date_from > self.event_date_to
        ):
            raise SearchQueryMalformedError("event_date_from must be on or before event_date_to")
        if self.fatalities_min is not None and self.fatalities_min < 0:
            raise SearchQueryMalformedError("fatalities_min must be >= 0")
        if (
            self.fatalities_min is not None
            and self.fatalities_max is not None
            and self.fatalities_min > self.fatalities_max
        ):
            raise SearchQueryMalformedError("fatalities_min must be <= fatalities_max")
        if not (1 <= self.limit <= MAX_SEARCH_LIMIT):
            raise SearchQueryMalformedError(f"limit must be in [1, {MAX_SEARCH_LIMIT}]")
        # Cursor must be consistent: both halves provided or neither.
        if (self.after_rank is None) ^ (self.after_id is None):
            raise SearchQueryMalformedError("after_rank and after_id must be provided together")
        if self.confidence_bands is not None:
            illegal = self.confidence_bands - {"high", "medium", "low", "unknown"}
            if illegal:
                raise SearchQueryMalformedError(f"Unknown confidence band(s): {sorted(illegal)}")

    @property
    def has_text_query(self) -> bool:
        """Whether a non-trivial text query was provided.

        Empty / whitespace queries fall back to the "newest published"
        ordering instead of running ``ts_rank_cd``.
        """
        return self.q is not None and bool(self.q.strip())


class SearchHit(DomainModel):
    """One row of the search result list.

    ``rank`` is the raw ``ts_rank_cd`` score.  Stable to two decimal
    places under the same query; useful in regression tests.  Hidden
    from the public response by default (the API schema sets a
    debug-only flag for exposing it).
    """

    page_id: UUID
    slug: str
    title: str
    short_summary: str | None = None
    operator: str | None = None
    aircraft_type: str | None = None
    country: str | None = None
    event_date: date | None = None
    fatalities_total: int | None = None
    confidence_band: str
    last_published_at: datetime
    rank: float = Field(default=0.0)


@dataclass(frozen=True)
class SearchResult:
    """One page of search results plus the cursor for the next page."""

    items: list[SearchHit]
    next_cursor_rank: float | None
    next_cursor_id: UUID | None
    limit: int


# Re-bind for mypy/pyflakes; these are used in docstrings only.
_ = model_validator
