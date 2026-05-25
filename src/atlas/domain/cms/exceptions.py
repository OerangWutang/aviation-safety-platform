"""CMS-layer exceptions.

Each content kind gets its own ``NotFound`` / ``NotPublished`` /
``Retracted`` triple so the response error codes are specific
enough for a UI to render different messages without parsing a
generic error.  The shared :class:`CmsContentModifiedError` handles
the optimistic-concurrency case across all three kinds — the error
shape is identical (caller saw stale version, retry with current).
"""

from __future__ import annotations

from uuid import UUID

from atlas.domain.exceptions import AtlasError, NotFoundError

# ── Glossary ────────────────────────────────────────────────────────────────


class GlossaryTermNotFoundError(NotFoundError):
    code = "GLOSSARY_TERM_NOT_FOUND"


class GlossaryTermNotPublishedError(NotFoundError):
    """A term exists in DRAFT/IN_REVIEW/APPROVED/ARCHIVED but is not
    currently visible publicly.

    Surfaced as 404 so the public surface doesn't leak the existence
    of work-in-progress terms — same contract as Phase 1's
    :class:`PublicEventPageNotPublishedError`.
    """

    code = "GLOSSARY_TERM_NOT_PUBLISHED"

    def __init__(self, term: str):
        self.term = term
        super().__init__(f"Glossary term {term!r} is not published")


class GlossaryTermRetractedError(AtlasError):
    """A term was once published but has since been retracted.

    Surfaced as 410 with the retraction note in the response details
    — same shape as Phase 9's retraction handler.
    """

    code = "GLOSSARY_TERM_RETRACTED"

    def __init__(self, term: str, retraction_note: str | None):
        self.term = term
        self.retraction_note = retraction_note
        super().__init__(f"Glossary term {term!r} has been retracted")


# ── Methodology ─────────────────────────────────────────────────────────────


class MethodologyPageNotFoundError(NotFoundError):
    code = "METHODOLOGY_PAGE_NOT_FOUND"


class MethodologyPageNotPublishedError(NotFoundError):
    code = "METHODOLOGY_PAGE_NOT_PUBLISHED"

    def __init__(self, slug: str):
        self.slug = slug
        super().__init__(f"Methodology page {slug!r} is not published")


class MethodologyPageRetractedError(AtlasError):
    code = "METHODOLOGY_PAGE_RETRACTED"

    def __init__(self, slug: str, retraction_note: str | None):
        self.slug = slug
        self.retraction_note = retraction_note
        super().__init__(f"Methodology page {slug!r} has been retracted")


# ── Changelog ───────────────────────────────────────────────────────────────


class ChangelogEntryNotFoundError(NotFoundError):
    code = "CHANGELOG_ENTRY_NOT_FOUND"


class ChangelogEntryNotPublishedError(NotFoundError):
    code = "CHANGELOG_ENTRY_NOT_PUBLISHED"

    def __init__(self, slug: str):
        self.slug = slug
        super().__init__(f"Changelog entry {slug!r} is not published")


class ChangelogEntryRetractedError(AtlasError):
    code = "CHANGELOG_ENTRY_RETRACTED"

    def __init__(self, slug: str, retraction_note: str | None):
        self.slug = slug
        self.retraction_note = retraction_note
        super().__init__(f"Changelog entry {slug!r} has been retracted")


# ── Shared concurrency error ────────────────────────────────────────────────


class CmsContentModifiedError(AtlasError):
    """Raised when an update/transition's ``expected_version`` doesn't
    match the stored version.

    Surfaced as HTTP 409 Conflict (same as Phase 9's
    :class:`PublicEventPageModifiedError`).  The error carries
    enough detail for a UI to re-fetch and retry: which kind of
    content, the entity id, and both versions.
    """

    code = "CMS_CONTENT_MODIFIED"

    def __init__(
        self,
        *,
        kind: str,
        entity_id: UUID,
        expected_version: int,
        actual_version: int,
    ):
        self.kind = kind
        self.entity_id = entity_id
        self.expected_version = expected_version
        self.actual_version = actual_version
        super().__init__(
            f"{kind} {entity_id} was modified concurrently "
            f"(expected v{expected_version}, found v{actual_version})"
        )
