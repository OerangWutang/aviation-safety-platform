"""Unit tests for ``atlas.domain.publication.slug``.

The slug normalizer is the only public-facing identifier shape we
publish, so the rules pinned here are part of the API contract:
breaking any of these tests is a breaking change for inbound links
and search engines.
"""

from __future__ import annotations

import pytest

from atlas.domain.publication.slug import (
    MAX_SLUG_LENGTH,
    InvalidSlugError,
    is_valid_slug,
    normalize_slug,
)


class TestNormalizeSlug:
    def test_lowercases_and_hyphenates_spaces(self) -> None:
        assert normalize_slug("Boeing 737 N12345") == "boeing-737-n12345"

    def test_idempotent_on_already_canonical_input(self) -> None:
        once = normalize_slug("Test Slug")
        twice = normalize_slug(once)
        assert once == twice == "test-slug"

    def test_strips_punctuation_to_hyphens(self) -> None:
        assert normalize_slug("Smith & Jones, Inc.") == "smith-jones-inc"

    def test_collapses_repeated_separators(self) -> None:
        assert normalize_slug("a---b___c   d") == "a-b-c-d"

    def test_strips_leading_and_trailing_hyphens(self) -> None:
        assert normalize_slug("---hello world---") == "hello-world"

    def test_drops_non_ascii_silently(self) -> None:
        # ASCII fold is by design; callers wanting transliteration can
        # do it before calling.  We don't want surprising slug shapes
        # from accented characters that look identical to ASCII.
        assert normalize_slug("Café Boeing") == "caf-boeing"

    def test_underscores_become_hyphens(self) -> None:
        assert normalize_slug("event_2023_january") == "event-2023-january"

    def test_truncates_to_max_length(self) -> None:
        very_long = "a" * (MAX_SLUG_LENGTH + 50)
        result = normalize_slug(very_long)
        assert len(result) == MAX_SLUG_LENGTH
        assert is_valid_slug(result)

    def test_truncation_does_not_leave_trailing_hyphen(self) -> None:
        # Construct an input where the byte at MAX_SLUG_LENGTH falls
        # immediately after a hyphen, so a naive truncation would
        # leave one trailing.
        head = "a" * (MAX_SLUG_LENGTH - 1)
        raw = f"{head}-bbbbbbbbb"
        result = normalize_slug(raw)
        assert not result.endswith("-")
        assert is_valid_slug(result)

    @pytest.mark.parametrize("bad", ["", "   ", "!!!", "---", "    \t  \n"])
    def test_empty_or_unusable_input_raises(self, bad: str) -> None:
        with pytest.raises(InvalidSlugError):
            normalize_slug(bad)


class TestIsValidSlug:
    @pytest.mark.parametrize(
        "slug",
        [
            "a",
            "boeing-737",
            "2024-01-event-123",
            "a-b-c-d",
        ],
    )
    def test_accepts_canonical_form(self, slug: str) -> None:
        assert is_valid_slug(slug)

    @pytest.mark.parametrize(
        "bad",
        [
            "",
            "UPPER",
            "with space",
            "trailing-",
            "-leading",
            "double--hyphen",
            "punct!",
            "underscore_no",
        ],
    )
    def test_rejects_non_canonical_form(self, bad: str) -> None:
        assert not is_valid_slug(bad)

    def test_rejects_overlong_input(self) -> None:
        assert not is_valid_slug("a" * (MAX_SLUG_LENGTH + 1))
