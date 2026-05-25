"""Pydantic schemas for the Phase 7 NL search router.

Three things on the response that matter most to callers:

1. ``parsed`` — what the system understood.  Editorial-honesty.
2. ``items`` — the actual hits, in Phase 2's ``SearchHit`` shape
   for consistency.
3. ``parser_confidence`` — 0..1 fraction of significant tokens
   the parser claimed.  Low confidence is the signal to refine.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class _NlSearchModel(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)


# ── Request shapes ──────────────────────────────────────────────────────────


class NlSearchRequest(_NlSearchModel):
    query: str = Field(min_length=1, max_length=500)
    limit: int = Field(default=25, ge=1, le=100)


class SaveNlQueryRequest(_NlSearchModel):
    label: str = Field(min_length=1, max_length=200)
    raw_query: str = Field(min_length=1, max_length=500)
    frozen_filters: dict[str, Any]


# ── Response shapes ─────────────────────────────────────────────────────────


class ParsedQueryItem(_NlSearchModel):
    """The structured echo of what the parser extracted.

    Every field is optional because the parser is best-effort.
    The caller looks at this to decide whether to refine.
    """

    operator: str | None = None
    aircraft_type: str | None = None
    country: str | None = None
    event_date_from: date | None = None
    event_date_to: date | None = None
    fatalities_min: int | None = None
    fatalities_max: int | None = None
    fatal_only: bool
    non_fatal_only: bool
    hfacs_category_codes: list[str]
    shelo_factor_classes: list[str]
    free_text_remainder: str
    confidence: float


class NlSearchHitItem(_NlSearchModel):
    """Same shape as Phase 2's ``SearchHit`` but explicitly enumerated
    so the NL endpoint has its own forward-compatible schema."""

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


class NlSearchResponse(_NlSearchModel):
    parsed: ParsedQueryItem
    items: list[NlSearchHitItem]
    total_estimated: int
    log_id: UUID


class SavedNlQueryItem(_NlSearchModel):
    id: UUID
    user_id: UUID
    label: str
    raw_query: str
    frozen_filters: dict[str, Any]
    created_at: datetime


class SavedNlQueryListResponse(_NlSearchModel):
    items: list[SavedNlQueryItem]
