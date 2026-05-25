"""Search-index lifecycle hooks driven by publication state changes.

Phase 2 of the public-event search work.  The single rule
enforced here:

    The search index contains exactly the set of PUBLISHED pages.

Concretely:

- ``index_published_page`` upserts the entry for a page that just
  became PUBLISHED (publish from APPROVED, re-publish from ARCHIVED).
- ``remove_page_from_index`` deletes the entry for a page that just
  left PUBLISHED (archive or retract).

Both are called from the Phase 9 publication use cases inside the
same unit of work, so a failed index write rolls back the state
transition.  This keeps the invariant tight: a PUBLISHED page row
always has an index row, and vice versa.  An async outbox-driven
indexer is left as a documented follow-up.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from atlas.application.unit_of_work import UnitOfWork
from atlas.domain.entities import ProjectedAccidentRecord
from atlas.domain.publication.entities import PublicEventPage
from atlas.domain.search.entities import SearchIndexEntry

logger = logging.getLogger(__name__)


def _confidence_band(projection: ProjectedAccidentRecord | None) -> str:
    """Map ``completeness_score`` to the public-facing band.

    Mirrors the same logic in ``public_events._confidence_label`` —
    duplicated here on purpose so the search package does not depend
    on the public-events use case module.  The bands are part of the
    public contract, so a single change to thresholds requires
    touching both call sites and the regression tests catch any
    drift.
    """
    if projection is None:
        return "unknown"
    score = projection.completeness_score
    if score >= 0.85:
        return "high"
    if score >= 0.5:
        return "medium"
    if score > 0.0:
        return "low"
    return "unknown"


def _str_or_none(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value or None
    return str(value)


def _parse_event_date(value: Any):  # type: ignore[no-untyped-def]
    """Coerce a stored ``event_date`` field to a ``date`` if possible.

    Projection fields arrive from JSONB so dates are strings.  Bad
    formats are tolerated by returning ``None`` — the search index is
    a soft consumer and shouldn't fail a publish just because a
    misformatted date snuck through claim normalization.
    """
    from datetime import date as date_type
    from datetime import datetime

    if value is None:
        return None
    if isinstance(value, date_type):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value).date()
        except ValueError:
            try:
                return date_type.fromisoformat(value)
            except ValueError:
                return None
    return None


def _coerce_int(value: Any) -> int | None:
    """Coerce a projection scalar to int; tolerate strings and floats."""
    if value is None:
        return None
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _entry_from(
    page: PublicEventPage, projection: ProjectedAccidentRecord | None
) -> SearchIndexEntry:
    """Build a SearchIndexEntry from a page + its current projection.

    The projection is allowed to be None — search will still index
    the page with title/summary only.  That's the right behaviour
    when an editor publishes a page faster than the projection
    settles; the index will be refreshed on the next publish cycle.
    """
    fields = projection.fields if projection else {}
    return SearchIndexEntry(
        page_id=page.id,
        slug=page.slug,
        title=page.title,
        short_summary=page.short_summary,
        operator=_str_or_none(fields.get("operator")),
        aircraft_type=_str_or_none(fields.get("aircraft_type")),
        country=_str_or_none(fields.get("country")),
        event_date=_parse_event_date(fields.get("event_date")),
        fatalities_total=_coerce_int(fields.get("fatalities_total")),
        confidence_band=_confidence_band(projection),
        # ``last_published_at`` is set when the entity transitions to
        # PUBLISHED; the entity validator guarantees it is non-None
        # at this point, but we fall back defensively.
        last_published_at=page.last_published_at or page.updated_at,
    )


async def index_published_page(uow: UnitOfWork, page: PublicEventPage) -> None:
    """Upsert the index entry for a page that is currently PUBLISHED.

    The caller is responsible for confirming the page is PUBLISHED
    before invoking — this helper does not gate on status because it
    is also used by the admin reindex path which iterates only
    PUBLISHED rows by construction.
    """
    projection = await uow.projections.get(page.event_id)
    await uow.search.upsert(_entry_from(page, projection))


async def remove_page_from_index(uow: UnitOfWork, page_id: UUID) -> None:
    """Delete the index entry for a page leaving PUBLISHED.

    Safe to call regardless of prior index presence — the repository
    treats a missing row as a no-op.
    """
    await uow.search.delete(page_id)
