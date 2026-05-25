"""Map-layer exceptions."""

from __future__ import annotations

from atlas.domain.exceptions import DomainValidationError


class MapQueryMalformedError(DomainValidationError):
    """Raised when a map query fails validation.

    Surfaced as HTTP 422 by the existing generic
    ``DomainValidationError`` handler in ``app.py``.
    """

    code = "MAP_QUERY_MALFORMED"
