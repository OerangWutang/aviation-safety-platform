"""Tests for :class:`SearchQuery` validation.

The query object owns all validation so the use case and router can
treat construction as the validation boundary.
"""

from __future__ import annotations

from datetime import date
from uuid import uuid4

import pytest

from atlas.domain.search.entities import (
    DEFAULT_SEARCH_LIMIT,
    MAX_SEARCH_LIMIT,
    SearchQuery,
)
from atlas.domain.search.exceptions import SearchQueryMalformedError


class TestSearchQueryValidation:
    def test_default_construction_succeeds(self) -> None:
        q = SearchQuery()
        assert q.limit == DEFAULT_SEARCH_LIMIT
        assert q.has_text_query is False

    def test_empty_q_is_not_a_text_query(self) -> None:
        assert SearchQuery(q="").has_text_query is False
        assert SearchQuery(q="   ").has_text_query is False

    def test_overlong_q_rejected(self) -> None:
        with pytest.raises(SearchQueryMalformedError):
            SearchQuery(q="x" * 201)

    def test_inverted_date_range_rejected(self) -> None:
        with pytest.raises(SearchQueryMalformedError):
            SearchQuery(
                event_date_from=date(2024, 1, 1),
                event_date_to=date(2023, 1, 1),
            )

    def test_negative_fatalities_min_rejected(self) -> None:
        with pytest.raises(SearchQueryMalformedError):
            SearchQuery(fatalities_min=-1)

    def test_inverted_fatalities_range_rejected(self) -> None:
        with pytest.raises(SearchQueryMalformedError):
            SearchQuery(fatalities_min=10, fatalities_max=5)

    @pytest.mark.parametrize("bad_limit", [0, -5, MAX_SEARCH_LIMIT + 1, 10_000])
    def test_out_of_range_limit_rejected(self, bad_limit: int) -> None:
        with pytest.raises(SearchQueryMalformedError):
            SearchQuery(limit=bad_limit)

    def test_partial_cursor_rejected(self) -> None:
        with pytest.raises(SearchQueryMalformedError):
            SearchQuery(after_rank=1.5)
        with pytest.raises(SearchQueryMalformedError):
            SearchQuery(after_id=uuid4())

    def test_unknown_confidence_band_rejected(self) -> None:
        with pytest.raises(SearchQueryMalformedError):
            SearchQuery(confidence_bands=frozenset({"high", "garbage"}))

    def test_known_confidence_bands_accepted(self) -> None:
        q = SearchQuery(confidence_bands=frozenset({"high", "medium"}))
        assert q.confidence_bands == frozenset({"high", "medium"})
