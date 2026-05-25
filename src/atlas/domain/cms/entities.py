"""CMS content entities and revision audit rows.

All three content entities share a near-identical shape with three
notable differences:

- :class:`GlossaryTerm` keys on ``term`` (kebab-case slug) and
  carries ``display_term`` for UI rendering.
- :class:`MethodologyPage` carries ``section`` + ``section_order``
  for the methodology nav grouping.
- :class:`ChangelogEntry` carries ``effective_date`` separately
  from ``last_published_at``: a changelog entry can describe a
  change that took effect weeks before the entry was published.

The shared workflow state (``status``, ``version``,
``first_published_at``, ``last_published_at``, ``retraction_note``)
lives on every entity with the same semantics as Phase 9's
:class:`PublicEventPage`.
"""

from __future__ import annotations

from datetime import date, datetime
from uuid import UUID, uuid4

from pydantic import Field

from atlas.domain.entities import DomainModel
from atlas.domain.publication.entities import PublicationStatus
from atlas.domain.utils import utc_now

# ── Glossary ─────────────────────────────────────────────────────────────────


class GlossaryTerm(DomainModel):
    """A defined term in the public glossary.

    ``term`` is the canonical lookup key — kebab-case slug used in
    URLs and as the cross-reference target for inline glossary
    links (e.g. ``[reliability-tier]``).  ``display_term`` is the
    human form for rendering ("Reliability Tier").
    """

    id: UUID = Field(default_factory=uuid4)
    term: str = Field(min_length=1, max_length=120)
    display_term: str = Field(min_length=1, max_length=200)
    body_markdown: str
    status: PublicationStatus = PublicationStatus.DRAFT
    version: int = Field(default=1, ge=1)
    first_published_at: datetime | None = None
    last_published_at: datetime | None = None
    retraction_note: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class GlossaryTermRevision(DomainModel):
    """Immutable audit row for a glossary term transition.

    Same shape as Phase 9's :class:`PublicEventPageRevision`,
    parameterised over ``term_id`` instead of ``page_id``.
    """

    id: UUID = Field(default_factory=uuid4)
    term_id: UUID
    from_status: PublicationStatus | None = None
    to_status: PublicationStatus
    version_at_revision: int
    editor_user_id: UUID
    transition_reason: str | None = None
    correction_note: str | None = None
    created_at: datetime = Field(default_factory=utc_now)


# ── Methodology ──────────────────────────────────────────────────────────────


class MethodologyPage(DomainModel):
    """A methodology page — long-form explanation of how Atlas works.

    ``section`` groups pages into the navigation hierarchy
    ("data-sources", "confidence", "audit"); ``section_order``
    orders pages within a section.  Pages with the same
    (section, section_order) sort by title as the deterministic
    final tiebreak.
    """

    id: UUID = Field(default_factory=uuid4)
    slug: str = Field(min_length=1, max_length=160)
    title: str = Field(min_length=1, max_length=300)
    section: str = Field(min_length=1, max_length=100)
    section_order: int = Field(default=0, ge=0)
    body_markdown: str
    status: PublicationStatus = PublicationStatus.DRAFT
    version: int = Field(default=1, ge=1)
    first_published_at: datetime | None = None
    last_published_at: datetime | None = None
    retraction_note: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class MethodologyPageRevision(DomainModel):
    id: UUID = Field(default_factory=uuid4)
    page_id: UUID
    from_status: PublicationStatus | None = None
    to_status: PublicationStatus
    version_at_revision: int
    editor_user_id: UUID
    transition_reason: str | None = None
    correction_note: str | None = None
    created_at: datetime = Field(default_factory=utc_now)


# ── Changelog ────────────────────────────────────────────────────────────────


class ChangelogEntry(DomainModel):
    """A changelog entry describing a notable platform change.

    The crucial design choice here: ``effective_date`` is the
    human-meaningful date (when the change happened in the real
    world), distinct from ``last_published_at`` (when the entry was
    published to readers).  A retroactive entry written today can
    describe a change that took effect last month; both dates are
    correct and both are surfaced on the public read.
    """

    id: UUID = Field(default_factory=uuid4)
    slug: str = Field(min_length=1, max_length=160)
    title: str = Field(min_length=1, max_length=300)
    effective_date: date
    body_markdown: str
    status: PublicationStatus = PublicationStatus.DRAFT
    version: int = Field(default=1, ge=1)
    first_published_at: datetime | None = None
    last_published_at: datetime | None = None
    retraction_note: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class ChangelogEntryRevision(DomainModel):
    id: UUID = Field(default_factory=uuid4)
    entry_id: UUID
    from_status: PublicationStatus | None = None
    to_status: PublicationStatus
    version_at_revision: int
    editor_user_id: UUID
    transition_reason: str | None = None
    correction_note: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
