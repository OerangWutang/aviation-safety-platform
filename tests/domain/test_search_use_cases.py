"""Use-case tests for public search and its publication lifecycle.

Two concerns covered here:

1. **Lifecycle invariant** — the search index contains exactly the
   set of PUBLISHED pages.  Driven through the Phase 9 publish /
   archive / retract / re-publish paths via the editorial use cases
   so the integration between the two phases is exercised.

2. **Use-case behaviour** — :class:`SearchPublicEvents` and
   :class:`ReindexPublicEvents` compose the repository correctly,
   filters fan out as expected, cursor pagination is stable, and
   ranking respects title > summary > facets ordering.
"""

from __future__ import annotations

from datetime import date
from uuid import uuid4

import pytest

from atlas.application.use_cases.editorial import (
    ApprovePublicEventPage,
    ArchivePublicEventPage,
    CreatePublicEventPage,
    CreatePublicEventPageInput,
    PublishPublicEventPage,
    RetractPublicEventPage,
    SubmitPublicEventPage,
    TransitionPublicEventPageInput,
)
from atlas.application.use_cases.reindex_public_events import ReindexPublicEvents
from atlas.application.use_cases.search_events import SearchPublicEvents
from atlas.domain.entities import AccidentEvent, ProjectedAccidentRecord
from atlas.domain.publication.entities import PublicationStatus
from atlas.domain.search.entities import SearchQuery
from tests.domain._fake_uow import InMemoryUnitOfWork

# ── Helpers ──────────────────────────────────────────────────────────────────


async def _seed_event_with_projection(
    uow: InMemoryUnitOfWork,
    *,
    operator: str = "ABC Airlines",
    aircraft_type: str = "Boeing 737-800",
    country: str = "United States",
    event_date: str = "2024-06-01",
    fatalities_total: int = 0,
):
    event = AccidentEvent()
    uow.store.events[event.id] = event
    uow.store.projections[event.id] = ProjectedAccidentRecord(
        event_id=event.id,
        fields={
            "operator": operator,
            "aircraft_type": aircraft_type,
            "country": country,
            "event_date": event_date,
            "fatalities_total": fatalities_total,
        },
        completeness_score=0.9,
    )
    return event.id


async def _create_and_publish(
    uow: InMemoryUnitOfWork,
    *,
    slug: str,
    title: str,
    event_id=None,
    short_summary: str | None = None,
    **projection_kwargs,
):
    if event_id is None:
        event_id = await _seed_event_with_projection(uow, **projection_kwargs)
    page = await CreatePublicEventPage(uow).execute(
        CreatePublicEventPageInput(
            event_id=event_id,
            slug=slug,
            title=title,
            short_summary=short_summary,
            editor_user_id=uuid4(),
        )
    )
    page = await SubmitPublicEventPage(uow).execute(
        TransitionPublicEventPageInput(
            page_id=page.id,
            expected_version=page.version,
            editor_user_id=uuid4(),
        )
    )
    page = await ApprovePublicEventPage(uow).execute(
        TransitionPublicEventPageInput(
            page_id=page.id,
            expected_version=page.version,
            editor_user_id=uuid4(),
        )
    )
    page = await PublishPublicEventPage(uow).execute(
        TransitionPublicEventPageInput(
            page_id=page.id,
            expected_version=page.version,
            editor_user_id=uuid4(),
        )
    )
    return page


# ── Lifecycle invariant ──────────────────────────────────────────────────────


class TestSearchIndexLifecycle:
    """The invariant: search index == PUBLISHED rows."""

    async def test_publish_inserts_into_index(self) -> None:
        uow = InMemoryUnitOfWork()
        page = await _create_and_publish(uow, slug="published-event", title="Published event")
        # The page exists in the search store.
        assert page.id in uow.store.search.entries

    async def test_archive_removes_from_index(self) -> None:
        uow = InMemoryUnitOfWork()
        page = await _create_and_publish(uow, slug="to-archive", title="X")
        assert page.id in uow.store.search.entries

        page = await ArchivePublicEventPage(uow).execute(
            TransitionPublicEventPageInput(
                page_id=page.id,
                expected_version=page.version,
                editor_user_id=uuid4(),
            )
        )
        assert page.status == PublicationStatus.ARCHIVED
        assert page.id not in uow.store.search.entries

    async def test_retract_removes_from_index(self) -> None:
        uow = InMemoryUnitOfWork()
        page = await _create_and_publish(uow, slug="to-retract", title="X")
        await RetractPublicEventPage(uow).execute(
            TransitionPublicEventPageInput(
                page_id=page.id,
                expected_version=page.version,
                editor_user_id=uuid4(),
                retraction_note="error",
            )
        )
        assert page.id not in uow.store.search.entries

    async def test_republish_from_archive_reinserts(self) -> None:
        uow = InMemoryUnitOfWork()
        page = await _create_and_publish(uow, slug="cycle", title="Cycle")
        page = await ArchivePublicEventPage(uow).execute(
            TransitionPublicEventPageInput(
                page_id=page.id,
                expected_version=page.version,
                editor_user_id=uuid4(),
            )
        )
        assert page.id not in uow.store.search.entries
        page = await PublishPublicEventPage(uow).execute(
            TransitionPublicEventPageInput(
                page_id=page.id,
                expected_version=page.version,
                editor_user_id=uuid4(),
            )
        )
        assert page.id in uow.store.search.entries

    async def test_draft_never_in_index(self) -> None:
        """DRAFTs and IN_REVIEW pages never touch the search index."""
        uow = InMemoryUnitOfWork()
        event_id = await _seed_event_with_projection(uow)
        page = await CreatePublicEventPage(uow).execute(
            CreatePublicEventPageInput(
                event_id=event_id,
                slug="never-published",
                title="X",
                editor_user_id=uuid4(),
            )
        )
        await SubmitPublicEventPage(uow).execute(
            TransitionPublicEventPageInput(
                page_id=page.id,
                expected_version=page.version,
                editor_user_id=uuid4(),
            )
        )
        assert page.id not in uow.store.search.entries


# ── SearchPublicEvents use case ──────────────────────────────────────────────


class TestSearchPublicEvents:
    async def test_text_query_finds_match(self) -> None:
        uow = InMemoryUnitOfWork()
        await _create_and_publish(
            uow,
            slug="boeing-737",
            title="Boeing 737 accident",
            aircraft_type="Boeing 737-800",
        )
        await _create_and_publish(
            uow,
            slug="airbus-a320",
            title="Airbus A320 incident",
            aircraft_type="Airbus A320",
        )
        result = await SearchPublicEvents(uow).execute(SearchQuery(q="boeing"))
        # The airbus row has no "boeing" token in any indexed field
        # so it must be absent from the result.
        assert [h.slug for h in result.items] == ["boeing-737"]

    async def test_title_match_outranks_summary_match(self) -> None:
        """Weighting contract: a title hit outranks a summary hit.

        This is the only ranking invariant the fake mirrors from the
        SQL backend; the actual ts_rank_cd score does the same on
        Postgres.
        """
        uow = InMemoryUnitOfWork()
        await _create_and_publish(
            uow,
            slug="title-hit",
            title="Hydraulic failure on takeoff",
            short_summary=None,
        )
        await _create_and_publish(
            uow,
            slug="summary-hit",
            title="Routine incident",
            short_summary="Crew reported a hydraulic warning during taxi.",
        )
        result = await SearchPublicEvents(uow).execute(SearchQuery(q="hydraulic"))
        assert [h.slug for h in result.items] == ["title-hit", "summary-hit"]

    async def test_operator_filter_narrows_results(self) -> None:
        uow = InMemoryUnitOfWork()
        await _create_and_publish(uow, slug="abc-flight", title="X", operator="ABC Airlines")
        await _create_and_publish(uow, slug="xyz-flight", title="X", operator="XYZ Airlines")
        result = await SearchPublicEvents(uow).execute(SearchQuery(operator="ABC Airlines"))
        assert [h.slug for h in result.items] == ["abc-flight"]

    async def test_date_range_filter(self) -> None:
        uow = InMemoryUnitOfWork()
        await _create_and_publish(uow, slug="early", title="X", event_date="2024-01-01")
        await _create_and_publish(uow, slug="mid", title="X", event_date="2024-06-01")
        await _create_and_publish(uow, slug="late", title="X", event_date="2024-12-01")
        result = await SearchPublicEvents(uow).execute(
            SearchQuery(
                event_date_from=date(2024, 3, 1),
                event_date_to=date(2024, 9, 1),
            )
        )
        slugs = {h.slug for h in result.items}
        assert slugs == {"mid"}

    async def test_fatalities_range_filter(self) -> None:
        uow = InMemoryUnitOfWork()
        await _create_and_publish(uow, slug="none", title="X", fatalities_total=0)
        await _create_and_publish(uow, slug="some", title="X", fatalities_total=5)
        await _create_and_publish(uow, slug="many", title="X", fatalities_total=200)
        result = await SearchPublicEvents(uow).execute(
            SearchQuery(fatalities_min=1, fatalities_max=50)
        )
        slugs = {h.slug for h in result.items}
        assert slugs == {"some"}

    async def test_empty_query_returns_published_pages(self) -> None:
        """No filters, no query — returns everything published."""
        uow = InMemoryUnitOfWork()
        for i in range(3):
            await _create_and_publish(uow, slug=f"e-{i}", title=f"E {i}")
        result = await SearchPublicEvents(uow).execute(SearchQuery())
        assert len(result.items) == 3

    async def test_cursor_pagination_stability(self) -> None:
        """Walking page by page returns each row exactly once."""
        uow = InMemoryUnitOfWork()
        for i in range(5):
            await _create_and_publish(uow, slug=f"page-{i}", title=f"Hydraulic event {i}")
        first = await SearchPublicEvents(uow).execute(SearchQuery(q="hydraulic", limit=2))
        assert first.next_cursor_id is not None
        assert first.next_cursor_rank is not None
        second = await SearchPublicEvents(uow).execute(
            SearchQuery(
                q="hydraulic",
                limit=2,
                after_rank=first.next_cursor_rank,
                after_id=first.next_cursor_id,
            )
        )
        third = await SearchPublicEvents(uow).execute(
            SearchQuery(
                q="hydraulic",
                limit=2,
                after_rank=second.next_cursor_rank,
                after_id=second.next_cursor_id,
            )
        )
        seen = (
            [h.slug for h in first.items]
            + [h.slug for h in second.items]
            + [h.slug for h in third.items]
        )
        assert sorted(seen) == sorted({f"page-{i}" for i in range(5)})
        # Last page is exhausted.
        assert third.next_cursor_id is None


# ── Admin reindex use case ───────────────────────────────────────────────────


class TestReindexPublicEvents:
    async def test_rebuilds_index_from_scratch(self) -> None:
        uow = InMemoryUnitOfWork()
        for i in range(3):
            await _create_and_publish(uow, slug=f"r-{i}", title=f"R {i}")
        # Manually wipe the index — simulating drift the reindex
        # endpoint exists to fix.
        uow.store.search.entries.clear()
        assert not uow.store.search.entries

        result = await ReindexPublicEvents(uow).execute()
        assert result.pages_reindexed == 3
        assert len(uow.store.search.entries) == 3

    async def test_only_indexes_published_pages(self) -> None:
        """A DRAFT page must not be reindexed."""
        uow = InMemoryUnitOfWork()
        await _create_and_publish(uow, slug="visible", title="X")

        # Create a DRAFT page that is never published.
        event_id = await _seed_event_with_projection(uow)
        await CreatePublicEventPage(uow).execute(
            CreatePublicEventPageInput(
                event_id=event_id,
                slug="hidden-draft",
                title="X",
                editor_user_id=uuid4(),
            )
        )

        uow.store.search.entries.clear()
        result = await ReindexPublicEvents(uow).execute()
        # Only the PUBLISHED page got reindexed.
        assert result.pages_reindexed == 1
        slugs = {e.slug for e in uow.store.search.entries.values()}
        assert slugs == {"visible"}


# ── Confidence band hits the band thresholds ────────────────────────────────


@pytest.mark.parametrize(
    ("score", "expected"),
    [
        (0.95, "high"),
        (0.85, "high"),
        (0.70, "medium"),
        (0.50, "medium"),
        (0.25, "low"),
        (0.0, "unknown"),
    ],
)
async def test_confidence_band_thresholds(score: float, expected: str) -> None:
    """The band thresholds match the public-events helper exactly.

    Pinned at this layer because the search index and the public list
    must agree on the labels they show the same user.
    """
    from atlas.application.use_cases._search_indexing import _confidence_band

    projection = ProjectedAccidentRecord(event_id=uuid4(), fields={}, completeness_score=score)
    assert _confidence_band(projection) == expected
