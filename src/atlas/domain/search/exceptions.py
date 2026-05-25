"""Search-layer exceptions."""

from __future__ import annotations

from atlas.domain.exceptions import DomainValidationError


class SearchQueryMalformedError(DomainValidationError):
    """Raised when a search request fails validation.

    Surfaced as HTTP 422 by the existing generic
    ``DomainValidationError`` handler in ``app.py``.  We don't add a
    dedicated handler because the response shape is identical.
    """

    code = "SEARCH_QUERY_MALFORMED"
