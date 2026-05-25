"""Slug normalization for public event pages.

The slug is the *only* stable public identifier for a public event
page.  Keeping the normalizer pure and side-effect-free makes it
trivial to unit-test and impossible to drift between write and read
paths.

Rules
-----
- ASCII only.  Non-ASCII characters are dropped (the caller can
  transliterate first if they care about i18n).
- Lowercased.
- Allowed characters: ``[a-z0-9-]``.  Everything else (whitespace,
  punctuation, underscores) collapses to a single hyphen.
- Hyphens collapsed; leading/trailing hyphens stripped.
- Length capped at :data:`MAX_SLUG_LENGTH`.  The cap matches the DB
  column width so an over-long slug fails at the public boundary, not
  the persistence boundary.
- Empty result raises :class:`InvalidSlugError` so callers cannot
  silently store a blank slug.
"""

from __future__ import annotations

import re
from typing import Final

from atlas.domain.exceptions import DomainValidationError

MAX_SLUG_LENGTH: Final[int] = 160

# Validation pattern used by route handlers and the entity validator.
# Anchored on both ends so partial matches don't slip through.
SLUG_PATTERN: Final[str] = r"^[a-z0-9]+(?:-[a-z0-9]+)*$"

_SLUG_RE: Final[re.Pattern[str]] = re.compile(SLUG_PATTERN)
_NON_SLUG_CHARS_RE: Final[re.Pattern[str]] = re.compile(r"[^a-z0-9]+")
_HYPHEN_COLLAPSE_RE: Final[re.Pattern[str]] = re.compile(r"-{2,}")


class InvalidSlugError(DomainValidationError):
    """Raised when a slug cannot be normalized into a valid form."""

    code = "INVALID_SLUG"


def normalize_slug(raw: str) -> str:
    """Return a canonical slug, or raise :class:`InvalidSlugError`.

    The function is idempotent: ``normalize_slug(normalize_slug(x)) ==
    normalize_slug(x)`` for any valid input.
    """
    if raw is None:
        # Defensive guard: the type hint forbids this, but callers
        # coming from JSON deserialization can pass through None when
        # an optional field is absent.  Surfacing the InvalidSlugError
        # is friendlier than a TypeError on the next call.
        raise InvalidSlugError("slug must not be None")
    # ASCII-fold by dropping anything outside ASCII; cheap and explicit.
    ascii_only = raw.encode("ascii", "ignore").decode("ascii")
    lowered = ascii_only.lower().strip()
    # Replace runs of disallowed characters with a single hyphen, then
    # collapse repeated hyphens.  This produces stable output for inputs
    # like "  Boeing 737 — N12345 " -> "boeing-737-n12345".
    replaced = _NON_SLUG_CHARS_RE.sub("-", lowered)
    collapsed = _HYPHEN_COLLAPSE_RE.sub("-", replaced).strip("-")
    if not collapsed:
        raise InvalidSlugError("slug normalization produced an empty value")
    if len(collapsed) > MAX_SLUG_LENGTH:
        collapsed = collapsed[:MAX_SLUG_LENGTH].rstrip("-")
        if not collapsed:
            # ``MAX_SLUG_LENGTH`` is comfortably above any plausible
            # all-hyphen prefix, so this is reachable only from an
            # adversarial caller; raising keeps the invariant clean.
            raise InvalidSlugError("slug truncated to empty value")
    return collapsed


def is_valid_slug(value: str) -> bool:
    """Return whether ``value`` is already in canonical slug form."""
    if not value or len(value) > MAX_SLUG_LENGTH:
        return False
    return _SLUG_RE.match(value) is not None
