"""Publication-layer exceptions.

Mapped to HTTP responses by the FastAPI exception handlers in
``atlas.presentation.api.app``.  See that module for the status-code
contract.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from atlas.domain.exceptions import AtlasError, DomainValidationError, NotFoundError

if TYPE_CHECKING:
    from atlas.domain.publication.entities import PublicationStatus


class PublicEventPageNotFoundError(NotFoundError):
    """Raised when a slug does not resolve to any page (including DRAFT)."""

    code = "PUBLIC_EVENT_PAGE_NOT_FOUND"


class PublicEventPageNotPublishedError(AtlasError):
    """Raised by public read paths when a page exists but is DRAFT.

    Surfaced as a 404 by the public API so DRAFT existence is not
    leaked.  See :func:`atlas.presentation.api.app._register_exception_handlers`.
    """

    code = "PUBLIC_EVENT_PAGE_NOT_PUBLISHED"


class PublicEventPageRetractedError(AtlasError):
    """Raised by public read paths when a page exists but is RETRACTED.

    Surfaced as HTTP 410 Gone (with the retraction note in the body)
    so curators of inbound links can update their references.
    """

    code = "PUBLIC_EVENT_PAGE_RETRACTED"

    def __init__(self, slug: str, retraction_note: str | None) -> None:
        self.slug = slug
        self.retraction_note = retraction_note
        super().__init__(f"Public event page {slug!r} has been retracted")


class SlugAlreadyTakenError(DomainValidationError):
    """Raised when a slug collision is detected at write time."""

    code = "SLUG_ALREADY_TAKEN"

    def __init__(self, slug: str) -> None:
        self.slug = slug
        super().__init__(f"Slug {slug!r} is already taken")


class PublicEventPageAlreadyExistsError(DomainValidationError):
    """Raised when an event_id already has a page (one-page-per-event)."""

    code = "PUBLIC_EVENT_PAGE_ALREADY_EXISTS"

    def __init__(self, event_id: UUID) -> None:
        self.event_id = event_id
        super().__init__(f"Event {event_id} already has a public page")


class InvalidPublicationTransitionError(DomainValidationError):
    """Raised when an editorial transition is not allowed.

    The state machine itself is owned by
    :mod:`atlas.domain.publication.workflow`; this exception is the
    typed signal used by use cases and the API layer.
    """

    code = "INVALID_PUBLICATION_TRANSITION"

    def __init__(
        self,
        *,
        from_status: PublicationStatus,
        to_status: PublicationStatus,
    ) -> None:
        self.from_status = from_status
        self.to_status = to_status
        super().__init__(
            f"Cannot transition public event page from {from_status.value} to {to_status.value}"
        )


class PublicEventPageModifiedError(DomainValidationError):
    """Raised on an optimistic-concurrency clash.

    The page's current ``version`` differs from the ``expected_version``
    the caller passed.  The caller should refetch and retry.

    Mapped to HTTP 409 Conflict by the API layer, mirroring the
    existing ``ConflictModifiedError`` convention.
    """

    code = "PUBLIC_EVENT_PAGE_MODIFIED"

    def __init__(self, *, expected_version: int, actual_version: int) -> None:
        self.expected_version = expected_version
        self.actual_version = actual_version
        super().__init__(
            f"Public event page was modified by another writer "
            f"(expected version {expected_version}, found {actual_version})"
        )


class EditorialFieldLockedError(DomainValidationError):
    """Raised when an editor tries to overwrite an evidence-backed field.

    Phase 9 keeps editorial overlay and structured projection
    explicitly separate.  Attempts to set a key on the page that
    overlaps a projection field (the structured fields rendered under
    ``fields`` in the public detail response) are rejected — the right
    path for changing a projected fact is a MANUAL_OVERRIDE claim,
    not an editorial edit.
    """

    code = "EDITORIAL_FIELD_LOCKED"

    def __init__(self, field_name: str) -> None:
        self.field_name = field_name
        super().__init__(
            f"Field {field_name!r} is evidence-backed and cannot be "
            f"edited via the editorial workflow; use a manual-override "
            f"claim instead."
        )
