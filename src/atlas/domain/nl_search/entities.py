"""NL search domain entities (Phase 7).

``ParsedQuery`` is the parser's structured output — the filter shape
the orchestrator can dispatch into existing search infrastructure.

``NlQueryLog`` is the anonymised log row; no ``user_id`` column on
purpose (see migration 043 comments).

``SavedNlQuery`` is the per-user pinned-query row.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any
from uuid import UUID, uuid4

from pydantic import Field

from atlas.domain.entities import DomainModel
from atlas.domain.utils import utc_now


class ParsedQuery(DomainModel):
    """The parser's structured output.

    Every field is optional because the parser is best-effort.
    ``free_text_remainder`` carries the substring of the query that
    didn't match any structured pattern — the orchestrator passes
    this to Phase 2 FTS so we get keyword coverage on whatever the
    parser didn't recognise.

    ``confidence`` is the parser's own self-assessment: 0.0 means
    "I matched nothing" (the whole query goes to FTS); 1.0 means
    "every token mapped onto a structured filter".  Computed as
    ``matched_chars / total_chars`` over the raw query.
    """

    operator: str | None = None
    aircraft_type: str | None = None
    country: str | None = None
    event_date_from: date | None = None
    event_date_to: date | None = None
    fatalities_min: int | None = None
    fatalities_max: int | None = None
    fatal_only: bool = False
    non_fatal_only: bool = False
    hfacs_category_codes: list[str] = Field(default_factory=list)
    shelo_factor_classes: list[str] = Field(default_factory=list)
    free_text_remainder: str = ""
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)


class NlQueryLog(DomainModel):
    """One row in the anonymised query log.

    The ``query_hash`` is computed at insert time so analytics can
    group identical-query repeats without re-hashing.
    """

    id: UUID = Field(default_factory=uuid4)
    raw_query: str
    query_hash: str = Field(min_length=64, max_length=64)
    parsed_filters: dict[str, Any]
    result_count: int = Field(ge=0)
    parser_confidence: float = Field(ge=0.0, le=1.0)
    hour_bucket: datetime
    created_at: datetime = Field(default_factory=utc_now)


class SavedNlQuery(DomainModel):
    """A user's saved NL query.

    ``frozen_filters`` is the parser output at save time — re-running
    a saved query uses these exact filters, not a fresh parse, so
    behaviour stays stable across parser revisions.
    """

    id: UUID = Field(default_factory=uuid4)
    user_id: UUID
    label: str = Field(min_length=1, max_length=200)
    raw_query: str
    frozen_filters: dict[str, Any]
    created_at: datetime = Field(default_factory=utc_now)
