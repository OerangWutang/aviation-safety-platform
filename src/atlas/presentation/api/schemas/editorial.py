"""Pydantic request/response schemas for the editorial workflow.

These live separately from ``schemas/public.py`` because the editorial
surface is curator-facing and may expose fields (revision-level
editor_user_id, full status, draft content) that must never appear on
the public surface.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from atlas.domain.publication.entities import PublicationStatus


class _EditorialModel(BaseModel):
    """Base model with ``extra='forbid'`` for editorial payloads.

    Strict on inbound writes so unknown fields fail fast — keeps the
    editorial surface from accreting projection-shaped keys by
    mistake.  Outbound shapes inherit ``from_attributes=True`` so
    dataclasses-from-use-cases serialize cleanly.
    """

    model_config = ConfigDict(extra="forbid", from_attributes=True)


# ── Requests ─────────────────────────────────────────────────────────────────


class CreatePublicEventPageRequest(_EditorialModel):
    """Payload to create a new DRAFT page.

    ``slug`` is normalized server-side; callers can pass loose user
    input and get a canonical slug back.
    """

    event_id: UUID
    slug: str = Field(min_length=1, max_length=300)
    title: str = Field(min_length=1, max_length=300)
    short_summary: str | None = Field(default=None, max_length=2000)
    narrative_markdown: str | None = None


class UpdatePublicEventPageRequest(_EditorialModel):
    """Payload for editorial edits (DRAFT only).

    ``expected_version`` is the value the caller saw when fetching
    the page; mismatch returns 409.  Every other field is optional;
    ``None`` means "leave unchanged".
    """

    expected_version: int = Field(ge=1)
    title: str | None = Field(default=None, min_length=1, max_length=300)
    short_summary: str | None = Field(default=None, max_length=2000)
    narrative_markdown: str | None = None
    slug: str | None = Field(default=None, min_length=1, max_length=300)
    correction_note: str | None = Field(default=None, max_length=2000)
    transition_reason: str | None = Field(default=None, max_length=500)


class TransitionRequest(_EditorialModel):
    """Shared payload for state-only transitions (submit, approve, ...)."""

    expected_version: int = Field(ge=1)
    transition_reason: str | None = Field(default=None, max_length=500)


class RetractRequest(_EditorialModel):
    """Payload for retract.  The note is rendered in the 410 response."""

    expected_version: int = Field(ge=1)
    retraction_note: str | None = Field(default=None, max_length=1000)
    transition_reason: str | None = Field(default=None, max_length=500)


# ── Responses ────────────────────────────────────────────────────────────────


class PublicEventPageResponse(_EditorialModel):
    """Full editorial view of a page (DRAFT or any other status).

    Distinct from the public detail schema because it exposes
    publication state, version, retraction note, and other curator-
    only fields.
    """

    id: UUID
    event_id: UUID
    slug: str
    title: str
    short_summary: str | None = None
    narrative_markdown: str | None = None
    status: PublicationStatus
    version: int
    first_published_at: datetime | None = None
    last_published_at: datetime | None = None
    retracted_at: datetime | None = None
    retraction_note: str | None = None
    created_at: datetime
    updated_at: datetime
    allowed_next_statuses: list[PublicationStatus] = Field(default_factory=list)


class EditorialPageSummary(_EditorialModel):
    id: UUID
    slug: str
    title: str
    status: PublicationStatus
    version: int
    updated_at: datetime
    last_published_at: datetime | None = None
    allowed_next_statuses: list[PublicationStatus]


class EditorialPageListResponse(_EditorialModel):
    items: list[EditorialPageSummary]
    limit: int
    next_cursor: UUID | None = None


class PageRevisionItem(_EditorialModel):
    id: UUID
    page_id: UUID
    version_at_moment: int
    from_status: PublicationStatus | None = None
    to_status: PublicationStatus
    title: str
    short_summary: str | None = None
    narrative_markdown: str | None = None
    editor_user_id: UUID
    transition_reason: str | None = None
    correction_note: str | None = None
    created_at: datetime


class PageRevisionsResponse(_EditorialModel):
    page_id: UUID
    revisions: list[PageRevisionItem]


# Internal helper used by the router; not part of the public schema
# surface but lives here to keep response-construction close to the
# response model.
def page_to_response(page: Any) -> dict[str, Any]:
    """Convert a ``PublicEventPage`` to a response payload dict.

    Pulled into a helper so the router doesn't repeat the same field
    list across seven write endpoints.  ``allowed_next_statuses`` is
    derived from the workflow state machine at response time.
    """
    from atlas.domain.publication.workflow import allowed_next_states

    return {
        "id": page.id,
        "event_id": page.event_id,
        "slug": page.slug,
        "title": page.title,
        "short_summary": page.short_summary,
        "narrative_markdown": page.narrative_markdown,
        "status": page.status,
        "version": page.version,
        "first_published_at": page.first_published_at,
        "last_published_at": page.last_published_at,
        "retracted_at": page.retracted_at,
        "retraction_note": page.retraction_note,
        "created_at": page.created_at,
        "updated_at": page.updated_at,
        "allowed_next_statuses": sorted(allowed_next_states(page.status)),
    }
