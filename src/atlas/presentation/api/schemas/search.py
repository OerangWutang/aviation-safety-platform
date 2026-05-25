"""Pydantic schemas for the public search router.

These are intentionally minimal: the use case owns validation via
:class:`SearchQuery`'s ``__post_init__``.  The HTTP layer's job is
just to bind query params and serialize results.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class _SearchModel(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)


class SearchHitItem(_SearchModel):
    """Single row in a search response."""

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
    # Rank is exposed only for debugging.  Hidden behind a query
    # parameter on the public surface so production payloads stay
    # stable across ranking-algorithm tweaks.
    rank: float | None = None


class SearchResponse(_SearchModel):
    items: list[SearchHitItem]
    limit: int
    next_cursor_rank: float | None = Field(
        default=None,
        description=(
            "Pass back as ``after_rank`` to fetch the next page. "
            "``None`` when the result set is exhausted."
        ),
    )
    next_cursor_id: UUID | None = Field(
        default=None,
        description="Pass back as ``after_id`` together with ``after_rank``.",
    )


class ReindexResponse(_SearchModel):
    pages_reindexed: int
    # Phase 3: subset of pages_reindexed that also have coordinates
    # and were therefore added to the map index.  Default for
    # backward-compat with callers that haven't been updated.
    map_pages_reindexed: int = 0


def hits_to_response(result: Any, *, include_rank: bool) -> dict[str, Any]:
    """Build the public response payload from a :class:`SearchResult`.

    Centralised so the router doesn't repeat the include_rank gate.
    """
    return {
        "items": [
            {
                "slug": h.slug,
                "title": h.title,
                "short_summary": h.short_summary,
                "operator": h.operator,
                "aircraft_type": h.aircraft_type,
                "country": h.country,
                "event_date": h.event_date,
                "fatalities_total": h.fatalities_total,
                "confidence_band": h.confidence_band,
                "last_published_at": h.last_published_at,
                "rank": h.rank if include_rank else None,
            }
            for h in result.items
        ],
        "limit": result.limit,
        "next_cursor_rank": result.next_cursor_rank,
        "next_cursor_id": result.next_cursor_id,
    }
