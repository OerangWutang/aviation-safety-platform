"""Publication-layer domain entities.

``PublicEventPage`` carries publication metadata and editorial overlay
text.  ``PublicEventPageRevision`` is the immutable audit row written
every time a page transitions state or is edited.

The state machine itself lives in
``atlas.domain.publication.workflow``; this module only models the
data shapes and their per-row invariants.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import UUID, uuid4

from pydantic import Field, model_validator

from atlas.domain.entities import DomainModel
from atlas.domain.utils import utc_now


class PublicationStatus(StrEnum):
    """Lifecycle states for a public event page.

    Phase 9 introduces the full editorial state machine.  The
    transitions between these states are not encoded on this enum —
    they live in :mod:`atlas.domain.publication.workflow` so the data
    shape and the workflow rules can evolve independently.

    DRAFT
        Created by an editor but not yet under review.  Editable in
        place; not publicly visible.

    IN_REVIEW
        Submitted for review.  Editable only by the reviewer
        examining it; not publicly visible.  Reviewer may approve, or
        return to DRAFT for changes.

    APPROVED
        Approved by a reviewer but not yet visible to the public.
        Permits a publication scheduling step (Phase 9 itself
        publishes immediately on demand; scheduled publication is
        carried as a follow-up).

    PUBLISHED
        Visible to public read paths.  Requires ``last_published_at``
        to be non-null (CHECK + entity validator).

    ARCHIVED
        Previously published but soft-hidden.  Editors can reopen to
        DRAFT for revision; this is the recoverable counterpart to
        RETRACTED.

    RETRACTED
        Permanently withdrawn with a public retraction notice
        (HTTP 410).  Terminal: cannot transition out.
    """

    DRAFT = "DRAFT"
    IN_REVIEW = "IN_REVIEW"
    APPROVED = "APPROVED"
    PUBLISHED = "PUBLISHED"
    ARCHIVED = "ARCHIVED"
    RETRACTED = "RETRACTED"


class PublicEventPage(DomainModel):
    """A publication-metadata overlay on a canonical accident event.

    Invariants
    ----------
    - ``event_id`` is always the canonical (post-merge) event id.  The
      use case is responsible for resolving merge redirects before
      creating or updating a page; this entity does not look up the
      merge graph itself.
    - ``slug`` is globally unique and is the public stable identifier.
      Use ``normalize_slug`` before constructing this entity.
    - ``status`` transitions are *not* enforced on the entity itself.
      The transition map and validator live in
      :mod:`atlas.domain.publication.workflow`; use cases call it
      before mutating the row.  The validator below enforces the
      narrower invariant that PUBLISHED requires a publication
      timestamp and RETRACTED requires a retraction timestamp — those
      are write-time data invariants that must hold regardless of how
      the row got into that state.
    - ``title`` is editorial: it is a short display label, not a copy
      of projected fields.  Public response shapes pull operator /
      aircraft / location from the live projection.
    """

    id: UUID = Field(default_factory=uuid4)
    event_id: UUID
    slug: str
    title: str
    short_summary: str | None = None
    narrative_markdown: str | None = None
    status: PublicationStatus = PublicationStatus.DRAFT
    version: int = Field(default=1, ge=1)
    first_published_at: datetime | None = None
    last_published_at: datetime | None = None
    retracted_at: datetime | None = None
    retraction_note: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def check_status_timestamps(self) -> PublicEventPage:
        """Enforce status/timestamp invariants in domain space.

        The DB CHECK constraints catch the same invariants at write
        time, but mirroring them here means the in-memory fake UoW used
        by use-case tests fails fast on the same conditions a real
        Postgres would.
        """
        if self.status == PublicationStatus.PUBLISHED and self.last_published_at is None:
            raise ValueError("PUBLISHED public event page requires last_published_at")
        if self.status == PublicationStatus.RETRACTED and self.retracted_at is None:
            raise ValueError("RETRACTED public event page requires retracted_at")
        return self

    @property
    def is_publicly_visible(self) -> bool:
        """Return whether the page should appear on public read paths."""
        return self.status == PublicationStatus.PUBLISHED

    def publish(self, *, now: datetime | None = None) -> None:
        """Raw transition into PUBLISHED.

        This helper does **not** enforce state-machine rules — callers
        must validate the source state via
        :mod:`atlas.domain.publication.workflow` first.  Kept as a
        backwards-compatible no-validation primitive so Phase 1
        tooling/tests that pre-date the state machine continue to
        compose.
        """
        moment = now or utc_now()
        if self.first_published_at is None:
            self.first_published_at = moment
        self.last_published_at = moment
        self.status = PublicationStatus.PUBLISHED
        self.version += 1
        self.updated_at = moment

    def retract(self, note: str | None = None, *, now: datetime | None = None) -> None:
        """Raw transition into RETRACTED.  See :meth:`publish`."""
        moment = now or utc_now()
        self.status = PublicationStatus.RETRACTED
        self.retracted_at = moment
        # Defensive trim to the column width.  Mirrors the conflict-row
        # ``last_modified_note`` convention.
        self.retraction_note = (note or None) and note[:1000]
        self.version += 1
        self.updated_at = moment

    def archive(self, *, now: datetime | None = None) -> None:
        """Raw transition into ARCHIVED.  See :meth:`publish`."""
        moment = now or utc_now()
        self.status = PublicationStatus.ARCHIVED
        self.version += 1
        self.updated_at = moment


class PublicEventPageRevision(DomainModel):
    """Immutable audit row written for every editorial transition.

    Construction is the only public mutation: this entity has no
    helper methods that change state.  The repository surface only
    exposes ``add`` and ``find_by_page``.

    The editorial snapshot (title / short_summary / narrative_markdown)
    is captured at the moment of the transition rather than referencing
    the live page row, because the row mutates on subsequent edits and
    the revision list would otherwise show stale content.
    """

    id: UUID = Field(default_factory=uuid4)
    page_id: UUID
    version_at_moment: int = Field(ge=1)
    # NULL on the creation revision; non-NULL otherwise.
    from_status: PublicationStatus | None = None
    to_status: PublicationStatus
    title: str
    short_summary: str | None = None
    narrative_markdown: str | None = None
    editor_user_id: UUID
    transition_reason: str | None = None
    correction_note: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
