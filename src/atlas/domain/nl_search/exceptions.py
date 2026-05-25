"""NL search exceptions (Phase 7)."""

from __future__ import annotations

from atlas.domain.exceptions import NotFoundError


class SavedNlQueryNotFoundError(NotFoundError):
    code = "SAVED_NL_QUERY_NOT_FOUND"
