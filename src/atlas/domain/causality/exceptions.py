"""Causality-layer exceptions (Phase 4).

Per-kind ``NotFound`` / ``Conflict`` / validation errors so HTTP
responses carry specific codes rather than a generic miss.
"""

from __future__ import annotations

from atlas.domain.exceptions import AtlasError, DomainValidationError, NotFoundError


class HfacsCategoryNotFoundError(NotFoundError):
    code = "HFACS_CATEGORY_NOT_FOUND"


class HfacsSubcategoryNotFoundError(NotFoundError):
    code = "HFACS_SUBCATEGORY_NOT_FOUND"


class HfacsAttributionNotFoundError(NotFoundError):
    code = "HFACS_ATTRIBUTION_NOT_FOUND"


class HfacsAttributionConflictError(AtlasError):
    """Raised on a duplicate ``(event, category, subcategory)``
    attribution attempt, or on a stale ``expected_version`` update.

    Surfaced as 409 — same shape as Phase 9's
    ``PublicEventPageModifiedError`` and Phase 10's
    ``CmsContentModifiedError`` so UIs handling concurrency on
    other surfaces inherit the same wire contract.
    """

    code = "HFACS_ATTRIBUTION_CONFLICT"


class SheloFactorNotFoundError(NotFoundError):
    code = "SHELO_FACTOR_NOT_FOUND"


class SheloFactorConflictError(AtlasError):
    code = "SHELO_FACTOR_CONFLICT"


class SheloFactorInteractionConflictError(AtlasError):
    """Raised on a duplicate ``(event, source, target, kind)``
    interaction attempt."""

    code = "SHELO_FACTOR_INTERACTION_CONFLICT"


class SheloFactorInteractionSameNodeError(DomainValidationError):
    """Raised when source == target on an interaction.  422.

    The entity-level validator catches this; the use case translates
    to this typed error so the router maps to a specific HTTP code.
    """

    code = "SHELO_FACTOR_INTERACTION_SAME_NODE"
