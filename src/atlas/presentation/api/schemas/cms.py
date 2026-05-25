"""Pydantic schemas for the CMS routers (Phase 10).

Public read schemas keep only what a reader needs (no editor user
ids, no revision details).  Editorial read schemas carry the
workflow state.
"""

from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class _CmsModel(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)


# ── Glossary ─────────────────────────────────────────────────────────────────


class PublicGlossaryTerm(_CmsModel):
    term: str
    display_term: str
    body_markdown: str
    last_published_at: datetime | None = None


class PublicGlossaryListResponse(_CmsModel):
    items: list[PublicGlossaryTerm]


class CreateGlossaryTermRequest(_CmsModel):
    term: str = Field(min_length=1, max_length=120)
    display_term: str = Field(min_length=1, max_length=200)
    body_markdown: str = Field(min_length=1)


class UpdateGlossaryTermRequest(_CmsModel):
    expected_version: int = Field(ge=1)
    display_term: str = Field(min_length=1, max_length=200)
    body_markdown: str = Field(min_length=1)


class EditorialGlossaryTerm(_CmsModel):
    id: UUID
    term: str
    display_term: str
    body_markdown: str
    status: str
    version: int
    first_published_at: datetime | None = None
    last_published_at: datetime | None = None
    retraction_note: str | None = None
    created_at: datetime
    updated_at: datetime


# ── Methodology ──────────────────────────────────────────────────────────────


class PublicMethodologyPage(_CmsModel):
    slug: str
    title: str
    section: str
    section_order: int
    body_markdown: str
    last_published_at: datetime | None = None


class PublicMethodologySection(_CmsModel):
    section: str
    pages: list[PublicMethodologyPage]


class PublicMethodologyListResponse(_CmsModel):
    sections: list[PublicMethodologySection]


class CreateMethodologyPageRequest(_CmsModel):
    slug: str = Field(min_length=1, max_length=160)
    title: str = Field(min_length=1, max_length=300)
    section: str = Field(min_length=1, max_length=100)
    section_order: int = Field(default=0, ge=0)
    body_markdown: str = Field(min_length=1)


class UpdateMethodologyPageRequest(_CmsModel):
    expected_version: int = Field(ge=1)
    title: str = Field(min_length=1, max_length=300)
    section: str = Field(min_length=1, max_length=100)
    section_order: int = Field(ge=0)
    body_markdown: str = Field(min_length=1)


class EditorialMethodologyPage(_CmsModel):
    id: UUID
    slug: str
    title: str
    section: str
    section_order: int
    body_markdown: str
    status: str
    version: int
    first_published_at: datetime | None = None
    last_published_at: datetime | None = None
    retraction_note: str | None = None
    created_at: datetime
    updated_at: datetime


# ── Changelog ────────────────────────────────────────────────────────────────


class PublicChangelogEntry(_CmsModel):
    slug: str
    title: str
    effective_date: date
    body_markdown: str
    last_published_at: datetime | None = None


class PublicChangelogListResponse(_CmsModel):
    items: list[PublicChangelogEntry]
    next_cursor: UUID | None = None


class CreateChangelogEntryRequest(_CmsModel):
    slug: str = Field(min_length=1, max_length=160)
    title: str = Field(min_length=1, max_length=300)
    effective_date: date
    body_markdown: str = Field(min_length=1)


class UpdateChangelogEntryRequest(_CmsModel):
    expected_version: int = Field(ge=1)
    title: str = Field(min_length=1, max_length=300)
    effective_date: date
    body_markdown: str = Field(min_length=1)


class EditorialChangelogEntry(_CmsModel):
    id: UUID
    slug: str
    title: str
    effective_date: date
    body_markdown: str
    status: str
    version: int
    first_published_at: datetime | None = None
    last_published_at: datetime | None = None
    retraction_note: str | None = None
    created_at: datetime
    updated_at: datetime


# ── Shared transition request ────────────────────────────────────────────────


class TransitionRequest(_CmsModel):
    """Request body for any workflow transition.

    ``expected_version`` is required for optimistic concurrency.
    ``retraction_note`` is required only by the retract endpoint;
    other transitions may pass it as None.
    """

    expected_version: int = Field(ge=1)
    transition_reason: str | None = Field(default=None, max_length=2000)
    retraction_note: str | None = Field(default=None, max_length=4000)
