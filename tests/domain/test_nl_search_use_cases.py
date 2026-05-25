"""Use-case tests for Phase 7 NL search.

Two layers:

1. **Parser correctness.** Direct tests of ``parse_nl_query`` over
   date phrases, fatality predicates, aircraft/operator aliases,
   HFACS category mentions, SHELO keywords, free-text remainder,
   and confidence calculation.

2. **Orchestrator behaviour.** ``ExecuteNlSearch`` composes
   parser output into ``SearchQuery``, optionally intersects with
   HFACS attributions, and logs every call.  Tests verify the
   composed filters and the log row.

3. **Saved queries.** Per-user scoping; cross-user delete returns
   the typed not-found error; frozen filters preserved verbatim
   across save/list.
"""

from __future__ import annotations

from datetime import date, datetime
from uuid import uuid4

import pytest

from atlas.application.services.nl_query_parser import (
    hour_bucket_for,
    parse_nl_query,
    query_hash_for,
)
from atlas.application.use_cases.nl_search import (
    DeleteSavedNlQuery,
    ExecuteNlSearch,
    ListSavedNlQueries,
    NlSearchInput,
    SaveNlQuery,
    SaveNlQueryInput,
)
from atlas.domain.causality.entities import (
    EventHfacsAttribution,
    HfacsCategory,
    HfacsTier,
)
from atlas.domain.entities import AccidentEvent, ProjectedAccidentRecord
from atlas.domain.nl_search.exceptions import SavedNlQueryNotFoundError
from atlas.domain.publication.entities import (
    PublicationStatus,
    PublicEventPage,
)
from atlas.domain.search.entities import SearchHit
from tests.domain._fake_uow import InMemoryUnitOfWork

# ── Parser direct tests ─────────────────────────────────────────────────────


class TestParserDates:
    def test_bare_year(self) -> None:
        p = parse_nl_query("crashes in 2023", hfacs_categories=[])
        assert p.event_date_from == date(2023, 1, 1)
        assert p.event_date_to == date(2023, 12, 31)

    def test_between_years(self) -> None:
        p = parse_nl_query("between 2015 and 2020", hfacs_categories=[])
        assert p.event_date_from == date(2015, 1, 1)
        assert p.event_date_to == date(2020, 12, 31)

    def test_before_year(self) -> None:
        p = parse_nl_query("before 2020", hfacs_categories=[])
        assert p.event_date_to == date(2019, 12, 31)
        assert p.event_date_from is None

    def test_after_year(self) -> None:
        p = parse_nl_query("after 2018", hfacs_categories=[])
        assert p.event_date_from == date(2019, 1, 1)
        assert p.event_date_to is None

    def test_month_range_with_year(self) -> None:
        p = parse_nl_query("Jan-Mar 2024", hfacs_categories=[])
        assert p.event_date_from == date(2024, 1, 1)
        assert p.event_date_to == date(2024, 3, 28)

    def test_specific_range_beats_bare_year(self) -> None:
        """When both 'between' and a bare year are present, the
        more specific range wins (it's consumed first)."""
        p = parse_nl_query("between 2015 and 2020 something 2018", hfacs_categories=[])
        assert p.event_date_from == date(2015, 1, 1)
        assert p.event_date_to == date(2020, 12, 31)


class TestParserFatalities:
    def test_more_than(self) -> None:
        p = parse_nl_query("more than 100 fatalities", hfacs_categories=[])
        assert p.fatalities_min == 101  # exclusive lower bound

    def test_fewer_than_deaths(self) -> None:
        p = parse_nl_query("fewer than 10 deaths", hfacs_categories=[])
        assert p.fatalities_max == 9  # exclusive upper bound

    def test_fatal_flag(self) -> None:
        p = parse_nl_query("fatal accidents", hfacs_categories=[])
        assert p.fatal_only is True
        assert p.non_fatal_only is False

    def test_non_fatal_flag(self) -> None:
        p = parse_nl_query("non-fatal incidents", hfacs_categories=[])
        assert p.non_fatal_only is True
        assert p.fatal_only is False


class TestParserAliases:
    def test_aircraft_alias_737(self) -> None:
        p = parse_nl_query("737 accidents", hfacs_categories=[])
        assert p.aircraft_type == "Boeing 737"

    def test_aircraft_longer_alias_wins(self) -> None:
        """'boeing 737' should be matched as the longer alias, not
        as 'boeing' (which isn't an alias) plus '737'."""
        p = parse_nl_query("boeing 737 in delta fleet", hfacs_categories=[])
        assert p.aircraft_type == "Boeing 737"
        # 'delta' alias also matched on a separate token.
        assert p.operator == "Delta Air Lines"

    def test_operator_alias(self) -> None:
        p = parse_nl_query("delta accidents", hfacs_categories=[])
        assert p.operator == "Delta Air Lines"

    def test_unknown_alias_ignored(self) -> None:
        p = parse_nl_query("unknown carrier xyz", hfacs_categories=[])
        assert p.operator is None
        assert p.aircraft_type is None


class TestParserHfacs:
    def test_category_phrase_matches(self) -> None:
        cat = HfacsCategory(
            tier_code="PRE",
            code="PRE-CRM",
            tier=HfacsTier.PRECONDITIONS,
            name="Crew Resource Management",
            description="x",
        )
        p = parse_nl_query(
            "Crew Resource Management failures",
            hfacs_categories=[cat],
        )
        assert "PRE-CRM" in p.hfacs_category_codes

    def test_category_phrase_case_insensitive(self) -> None:
        cat = HfacsCategory(
            tier_code="ACT",
            code="ACT-DE",
            tier=HfacsTier.UNSAFE_ACTS,
            name="Decision Errors",
            description="x",
        )
        p = parse_nl_query("show me decision errors", hfacs_categories=[cat])
        assert p.hfacs_category_codes == ["ACT-DE"]


class TestParserShelo:
    def test_software_keyword(self) -> None:
        p = parse_nl_query("FADEC fault", hfacs_categories=[])
        assert "SOFTWARE" in p.shelo_factor_classes

    def test_liveware_keyword(self) -> None:
        p = parse_nl_query("pilot fatigue contributed", hfacs_categories=[])
        assert "LIVEWARE" in p.shelo_factor_classes

    def test_multiple_classes(self) -> None:
        p = parse_nl_query(
            "software and hardware failed together",
            hfacs_categories=[],
        )
        classes = set(p.shelo_factor_classes)
        assert "SOFTWARE" in classes
        assert "HARDWARE" in classes


class TestParserRemainderAndConfidence:
    def test_free_text_remainder_preserved(self) -> None:
        p = parse_nl_query("approach 737 in 2023", hfacs_categories=[])
        # 'approach' isn't a structured filter; it lands in
        # remainder for FTS.
        assert "approach" in p.free_text_remainder

    def test_confidence_full_match(self) -> None:
        """A query whose every significant token is claimed gets
        confidence 1.0.  Stop words ('the', 'in') don't count
        against the parser."""
        # "737 2023" — every significant token mapped.
        p = parse_nl_query("737 2023", hfacs_categories=[])
        assert p.confidence == 1.0

    def test_confidence_zero_match(self) -> None:
        p = parse_nl_query("windshear approach incident", hfacs_categories=[])
        # 'windshear' is a SHELO keyword → matched. The other two
        # are remainder.  Confidence is matched / significant.
        # We just check it's strictly less than 1.0 and > 0.
        assert 0.0 < p.confidence < 1.0

    def test_stop_word_only_query(self) -> None:
        p = parse_nl_query("show me the", hfacs_categories=[])
        # All stop words → significant token set is empty, so the
        # parser returns confidence 0.0 (nothing to claim).
        assert p.confidence == 0.0


class TestParserHelpers:
    def test_query_hash_lowercase_stable(self) -> None:
        assert query_hash_for("Hello") == query_hash_for("hello")
        assert query_hash_for(" hello ") == query_hash_for("hello")
        # Hash is 64 hex chars.
        assert len(query_hash_for("any query")) == 64

    def test_hour_bucket_floors(self) -> None:
        from datetime import UTC

        when = datetime(2024, 6, 1, 14, 37, 29, 555, tzinfo=UTC)
        bucket = hour_bucket_for(when)
        assert bucket == datetime(2024, 6, 1, 14, 0, 0, 0, tzinfo=UTC)


# ── Orchestrator tests ──────────────────────────────────────────────────────


def _seed_event_and_page(
    uow: InMemoryUnitOfWork,
    *,
    slug: str,
    title: str = "test page",
):
    """Seed event + projection + PUBLISHED public_event_page.

    The search store is what actually answers Phase 2 queries, so
    we ALSO seed a search-index entry below in tests that exercise
    real search dispatch.
    """
    from datetime import UTC

    event = AccidentEvent()
    uow.store.events[event.id] = event
    uow.store.projections[event.id] = ProjectedAccidentRecord(
        event_id=event.id, fields={}, completeness_score=0.5
    )
    now = datetime(2024, 6, 1, tzinfo=UTC)
    page = PublicEventPage(
        event_id=event.id,
        slug=slug,
        title=title,
        status=PublicationStatus.PUBLISHED,
        first_published_at=now,
        last_published_at=now,
    )
    uow.store.publication.pages[page.id] = page
    return event, page


def _make_hit(page) -> SearchHit:
    from datetime import UTC

    return SearchHit(
        page_id=page.id,
        slug=page.slug,
        title=page.title,
        confidence_band="medium",
        last_published_at=datetime(2024, 6, 1, tzinfo=UTC),
    )


class _StubSearch:
    """Minimal SearchRepository stub used for orchestrator tests.

    The fake UoW's real Phase 2 search uses a separate index that
    the orchestrator tests don't set up; instead we patch the
    ``search`` method to a stub that records the query and returns
    a pre-canned result."""

    def __init__(self, items=None):
        self.calls = []
        self.items = items or []

    async def search(self, query):
        from atlas.domain.search.entities import SearchResult

        self.calls.append(query)
        return SearchResult(
            items=self.items,
            next_cursor_rank=None,
            next_cursor_id=None,
            limit=query.limit,
        )

    async def upsert(self, *args, **kwargs):
        pass

    async def delete(self, *args, **kwargs):
        pass


class TestExecuteNlSearch:
    async def test_parses_and_dispatches(self) -> None:
        uow = InMemoryUnitOfWork()
        _event, page = _seed_event_and_page(uow, slug="evt")
        stub = _StubSearch(items=[_make_hit(page)])
        uow.search = stub  # type: ignore[assignment]
        result = await ExecuteNlSearch(uow).execute(
            NlSearchInput(raw_query="737 fatal accidents in 2023")
        )
        # Parser results echoed.
        assert result.parsed.aircraft_type == "Boeing 737"
        assert result.parsed.fatal_only is True
        assert result.parsed.event_date_from == date(2023, 1, 1)
        # Stub got the composed query.
        q = stub.calls[0]
        assert q.aircraft_type == "Boeing 737"
        assert q.fatalities_min == 1  # from fatal_only translation
        assert q.event_date_from == date(2023, 1, 1)
        # Result items returned.
        assert len(result.items) == 1
        # Log written.
        assert len(uow.store.nl_search.query_log) == 1
        log = uow.store.nl_search.query_log[0]
        assert log.result_count == 1
        assert log.query_hash == query_hash_for("737 fatal accidents in 2023")

    async def test_fatalities_min_explicit_beats_fatal_only(self) -> None:
        """If the query specifies 'more than 100 fatalities', that
        explicit value takes precedence over the implicit
        fatal_only → min=1 translation."""
        uow = InMemoryUnitOfWork()
        stub = _StubSearch()
        uow.search = stub  # type: ignore[assignment]
        await ExecuteNlSearch(uow).execute(
            NlSearchInput(raw_query="fatal accidents with more than 100 fatalities")
        )
        q = stub.calls[0]
        assert q.fatalities_min == 101

    async def test_hfacs_intersection_filters_results(self) -> None:
        """When parsed.hfacs_category_codes is non-empty, the
        result set is intersected with events that have at least
        one matching HFACS attribution."""
        uow = InMemoryUnitOfWork()
        # Seed two events; only one will have a CRM attribution.
        e_with, p_with = _seed_event_and_page(uow, slug="with-crm")
        _e_no, p_no = _seed_event_and_page(uow, slug="no-crm")
        # Seed the HFACS taxonomy.
        cat = HfacsCategory(
            tier_code="PRE",
            code="PRE-CRM",
            tier=HfacsTier.PRECONDITIONS,
            name="Crew Resource Management",
            description="x",
        )
        uow.store.causality.hfacs_categories[cat.id] = cat
        # Attribute the CRM category to the first event only.
        attribution = EventHfacsAttribution(
            event_id=e_with.id,
            category_id=cat.id,
            confidence=0.8,
            editor_user_id=uuid4(),
        )
        uow.store.causality.event_hfacs_attributions[attribution.id] = attribution
        # Stub search returns BOTH hits; the orchestrator should
        # filter down to just the CRM-attributed one.
        stub = _StubSearch(items=[_make_hit(p_with), _make_hit(p_no)])
        uow.search = stub  # type: ignore[assignment]
        result = await ExecuteNlSearch(uow).execute(
            NlSearchInput(raw_query="Crew Resource Management failures")
        )
        assert len(result.items) == 1
        assert result.items[0].slug == "with-crm"

    async def test_log_carries_hour_bucket(self) -> None:
        uow = InMemoryUnitOfWork()
        uow.search = _StubSearch()  # type: ignore[assignment]
        await ExecuteNlSearch(uow).execute(NlSearchInput(raw_query="anything"))
        log = uow.store.nl_search.query_log[0]
        # Hour bucket should be hour-floored.
        assert log.hour_bucket.minute == 0
        assert log.hour_bucket.second == 0


# ── Saved queries ───────────────────────────────────────────────────────────


class TestSavedQueries:
    async def test_save_and_list_per_user(self) -> None:
        uow = InMemoryUnitOfWork()
        user_a = uuid4()
        user_b = uuid4()
        await SaveNlQuery(uow).execute(
            SaveNlQueryInput(
                user_id=user_a,
                label="A's query",
                raw_query="737 fatal",
                frozen_filters={"aircraft_type": "Boeing 737"},
            )
        )
        await SaveNlQuery(uow).execute(
            SaveNlQueryInput(
                user_id=user_b,
                label="B's query",
                raw_query="A330 non-fatal",
                frozen_filters={"aircraft_type": "Airbus A330"},
            )
        )
        listed_a = await ListSavedNlQueries(uow).execute(user_a)
        listed_b = await ListSavedNlQueries(uow).execute(user_b)
        assert len(listed_a) == 1
        assert listed_a[0].label == "A's query"
        assert len(listed_b) == 1
        assert listed_b[0].label == "B's query"

    async def test_frozen_filters_preserved_verbatim(self) -> None:
        uow = InMemoryUnitOfWork()
        user = uuid4()
        filters = {
            "aircraft_type": "Boeing 737",
            "hfacs_category_codes": ["PRE-CRM"],
            "fatalities_min": 1,
            "free_text_remainder": "approach",
        }
        await SaveNlQuery(uow).execute(
            SaveNlQueryInput(
                user_id=user,
                label="example",
                raw_query="737 fatal CRM approach",
                frozen_filters=filters,
            )
        )
        listed = await ListSavedNlQueries(uow).execute(user)
        assert listed[0].frozen_filters == filters

    async def test_delete_own_query(self) -> None:
        uow = InMemoryUnitOfWork()
        user = uuid4()
        saved = await SaveNlQuery(uow).execute(
            SaveNlQueryInput(
                user_id=user,
                label="x",
                raw_query="x",
                frozen_filters={},
            )
        )
        await DeleteSavedNlQuery(uow).execute(saved_id=saved.id, user_id=user)
        listed = await ListSavedNlQueries(uow).execute(user)
        assert listed == []

    async def test_cross_user_delete_returns_not_found(self) -> None:
        """Deleting another user's saved query raises the typed
        not-found error.  The router maps to 404, so the existence
        of the other user's query isn't leaked."""
        uow = InMemoryUnitOfWork()
        owner = uuid4()
        attacker = uuid4()
        saved = await SaveNlQuery(uow).execute(
            SaveNlQueryInput(
                user_id=owner,
                label="owner's",
                raw_query="x",
                frozen_filters={},
            )
        )
        with pytest.raises(SavedNlQueryNotFoundError):
            await DeleteSavedNlQuery(uow).execute(saved_id=saved.id, user_id=attacker)
        # And the row still exists.
        listed = await ListSavedNlQueries(uow).execute(owner)
        assert len(listed) == 1

    async def test_delete_unknown_returns_not_found(self) -> None:
        uow = InMemoryUnitOfWork()
        with pytest.raises(SavedNlQueryNotFoundError):
            await DeleteSavedNlQuery(uow).execute(saved_id=uuid4(), user_id=uuid4())
