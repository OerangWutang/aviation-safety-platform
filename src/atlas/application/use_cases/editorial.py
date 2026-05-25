"""Editorial workflow use cases for public event pages (Phase 9).

These are the curator-facing write paths.  Each use case:

1. Loads the current page row (no DB write if the page is gone);
2. Validates the requested transition via
   :func:`atlas.domain.publication.workflow.validate_transition`;
3. Mutates an in-memory copy of the entity, bumping ``version``;
4. Persists via the repository (which performs the optimistic-
   concurrency check against ``expected_version``);
5. Appends an immutable :class:`PublicEventPageRevision` audit row.

The unit-of-work commit boundary spans (4) and (5) so a publish that
fails to write its revision rolls back the page mutation as well.

Editorial overlay vs. evidence
------------------------------

Phase 9 keeps a *narrow* editorial surface: only the page's editorial
fields (title, short summary, narrative markdown, slug) are writable
via this layer.  Attempts to set any other key on the update payload
are rejected with :class:`EditorialFieldLockedError`.  The right path
to change a projected fact (operator, fatalities, ...) is the
existing manual-override claim flow — see ``IngestClaims`` and the
conflict-resolution endpoints.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from atlas.application.unit_of_work import UnitOfWork
from atlas.application.use_cases._map_indexing import (
    index_published_page_in_map,
    remove_page_from_map,
)
from atlas.application.use_cases._search_indexing import (
    index_published_page,
    remove_page_from_index,
)
from atlas.domain.publication.entities import (
    PublicationStatus,
    PublicEventPage,
    PublicEventPageRevision,
)
from atlas.domain.publication.exceptions import (
    EditorialFieldLockedError,
    PublicEventPageNotFoundError,
    SlugAlreadyTakenError,
)
from atlas.domain.publication.slug import normalize_slug
from atlas.domain.publication.workflow import (
    allowed_next_states,
    validate_transition,
)
from atlas.domain.utils import utc_now

logger = logging.getLogger(__name__)


# The exclusive set of fields an editor may set via the editorial
# workflow.  Anything outside this set is treated as a
# field-lock violation.  This is the Phase 9 contract; expanding it
# is an explicit decision the next time we add an editorial field.
_EDITORIAL_FIELDS: frozenset[str] = frozenset(
    {"title", "short_summary", "narrative_markdown", "slug"}
)


# ── Inputs ───────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class CreatePublicEventPageInput:
    """Payload for creating a new DRAFT page.

    Slug is normalized in the use case so callers can pass loose
    user input and get a canonical slug back.
    """

    event_id: UUID
    slug: str
    title: str
    short_summary: str | None = None
    narrative_markdown: str | None = None
    editor_user_id: UUID = field(default_factory=lambda: UUID(int=0))


@dataclass(frozen=True)
class UpdatePublicEventPageInput:
    """Payload for editing a page in place.

    Only fields in :data:`_EDITORIAL_FIELDS` are accepted.  ``None`` in
    a field means "leave unchanged"; an explicit empty string for a
    nullable text field clears it.

    ``expected_version`` is the version the caller saw when fetching
    the page.  The repository raises ``PublicEventPageModifiedError``
    if it doesn't match.
    """

    page_id: UUID
    expected_version: int
    editor_user_id: UUID
    title: str | None = None
    short_summary: str | None = None
    narrative_markdown: str | None = None
    slug: str | None = None
    correction_note: str | None = None
    transition_reason: str | None = None


@dataclass(frozen=True)
class TransitionPublicEventPageInput:
    """Payload for state-only transitions.

    Carries an ``expected_version`` for optimistic concurrency and an
    optional reason for the audit trail.
    """

    page_id: UUID
    expected_version: int
    editor_user_id: UUID
    transition_reason: str | None = None
    # Used only by ``RetractPublicEventPage``; ignored elsewhere.
    retraction_note: str | None = None


# ── Shared helpers ───────────────────────────────────────────────────────────


async def _load_page(uow: UnitOfWork, page_id: UUID) -> PublicEventPage:
    page = await uow.public_event_pages.get_by_id(page_id)
    if page is None:
        raise PublicEventPageNotFoundError(f"Public event page {page_id} not found")
    return page


def _validate_editorial_kwargs(extras: dict[str, Any]) -> None:
    """Reject any kwargs outside :data:`_EDITORIAL_FIELDS`.

    Defence in depth against callers (especially future ones) wiring
    in projection-shaped fields by accident.  The router schema also
    forbids extras, but enforcing here means non-HTTP callers (CLI,
    workers, tests) hit the same contract.
    """
    illegal = set(extras) - _EDITORIAL_FIELDS
    if illegal:
        # Surface only the first offender — the typed exception's
        # field_name is more useful than a list of strings, and one
        # is enough to fail closed.
        raise EditorialFieldLockedError(next(iter(sorted(illegal))))


async def _write_revision(
    uow: UnitOfWork,
    page: PublicEventPage,
    *,
    from_status: PublicationStatus | None,
    editor_user_id: UUID,
    transition_reason: str | None,
    correction_note: str | None = None,
) -> PublicEventPageRevision:
    revision = PublicEventPageRevision(
        page_id=page.id,
        version_at_moment=page.version,
        from_status=from_status,
        to_status=page.status,
        title=page.title,
        short_summary=page.short_summary,
        narrative_markdown=page.narrative_markdown,
        editor_user_id=editor_user_id,
        transition_reason=transition_reason,
        correction_note=correction_note,
    )
    await uow.public_event_pages.add_revision(revision)
    return revision


# ── Create ───────────────────────────────────────────────────────────────────


class CreatePublicEventPage:
    """Create a new DRAFT page.

    The slug is normalized; the entity sits in DRAFT until an editor
    explicitly submits it.  A revision row is written so the audit
    trail starts from creation.
    """

    def __init__(self, uow: UnitOfWork):
        self._uow = uow

    async def execute(self, input: CreatePublicEventPageInput) -> PublicEventPage:
        slug = normalize_slug(input.slug)
        # Reject if the canonical event already has a page — let the
        # repo raise the typed error rather than racing with another
        # request.
        page = PublicEventPage(
            event_id=input.event_id,
            slug=slug,
            title=input.title,
            short_summary=input.short_summary,
            narrative_markdown=input.narrative_markdown,
            status=PublicationStatus.DRAFT,
            version=1,
        )
        await self._uow.public_event_pages.add(page)
        await _write_revision(
            self._uow,
            page,
            from_status=None,
            editor_user_id=input.editor_user_id,
            transition_reason="created",
        )
        await self._uow.commit()
        return page


# ── Update (DRAFT-only editorial edits) ──────────────────────────────────────


class UpdatePublicEventPage:
    """Edit a DRAFT page in place.

    Only DRAFT pages are editable in Phase 9.  Pages in IN_REVIEW or
    APPROVED must be sent back to DRAFT first (``RequestChanges`` or
    ``RejectPublicEventPage``); pages in PUBLISHED must be archived
    first.  This keeps the audit trail clean: the revision list
    cleanly groups "what the editor wrote in this DRAFT session"
    instead of interleaving with state changes.
    """

    def __init__(self, uow: UnitOfWork):
        self._uow = uow

    async def execute(self, input: UpdatePublicEventPageInput) -> PublicEventPage:
        page = await _load_page(self._uow, input.page_id)

        if page.status != PublicationStatus.DRAFT:
            # Use the workflow exception type so callers can branch on
            # it.  The transition_to here is "edit while non-draft",
            # not a real status change; phrase it as "stay in current
            # state but with edits", which the workflow forbids
            # implicitly because there is no DRAFT->DRAFT entry.
            from atlas.domain.publication.exceptions import (
                InvalidPublicationTransitionError,
            )

            raise InvalidPublicationTransitionError(
                from_status=page.status,
                to_status=PublicationStatus.DRAFT,
            )

        # Collect just the fields the caller actually wants to change.
        provided: dict[str, Any] = {}
        if input.title is not None:
            provided["title"] = input.title
        if input.short_summary is not None:
            provided["short_summary"] = input.short_summary or None
        if input.narrative_markdown is not None:
            provided["narrative_markdown"] = input.narrative_markdown or None
        if input.slug is not None:
            provided["slug"] = normalize_slug(input.slug)

        _validate_editorial_kwargs(provided)

        moment = utc_now()
        updated = page.model_copy(
            update={
                **provided,
                "version": page.version + 1,
                "updated_at": moment,
            }
        )

        try:
            await self._uow.public_event_pages.update(
                updated, expected_version=input.expected_version
            )
        except SlugAlreadyTakenError:
            # Re-raise: caller maps to 409/422; we don't want to bury
            # this in the generic update flow.
            raise
        await _write_revision(
            self._uow,
            updated,
            from_status=PublicationStatus.DRAFT,
            editor_user_id=input.editor_user_id,
            transition_reason=input.transition_reason or "edited",
            correction_note=input.correction_note,
        )
        await self._uow.commit()
        return updated


# ── State transitions ────────────────────────────────────────────────────────


class _TransitionUseCase:
    """Shared scaffolding for "validate + flip state + revision + commit".

    Subclasses declare the target status and a hook to mutate the
    timestamp fields (publish/retract need to set publication
    timestamps; the rest only set status + version).

    Publication-lifecycle subclasses (publish, archive, retract) also
    override ``_post_transition_hook`` to keep the search index in
    sync.  The hook runs inside the same unit of work as the state
    change, so a failed index write rolls back the transition — the
    invariant "search index == PUBLISHED rows" stays tight.
    """

    target_status: PublicationStatus

    def __init__(self, uow: UnitOfWork):
        self._uow = uow

    def _apply_status_mutation(
        self,
        page: PublicEventPage,
        *,
        now: datetime,
        input: TransitionPublicEventPageInput,
    ) -> PublicEventPage:
        # Default: status + version + updated_at.  Subclasses override
        # for the timestamp-bearing transitions.
        return page.model_copy(
            update={
                "status": self.target_status,
                "version": page.version + 1,
                "updated_at": now,
            }
        )

    async def _post_transition_hook(self, page: PublicEventPage) -> None:
        """Hook for side effects that must run before commit.

        Default: no-op.  Publication-lifecycle subclasses override to
        upsert/delete the search index entry.  Keeping the hook async
        is important: index writes are async, and inlining them in
        ``execute`` would force every transition to be aware of the
        index even when it's a no-op.
        """
        return None

    async def execute(self, input: TransitionPublicEventPageInput) -> PublicEventPage:
        page = await _load_page(self._uow, input.page_id)
        from_status = page.status
        validate_transition(from_status, self.target_status)

        moment = utc_now()
        updated = self._apply_status_mutation(page, now=moment, input=input)

        await self._uow.public_event_pages.update(updated, expected_version=input.expected_version)
        await _write_revision(
            self._uow,
            updated,
            from_status=from_status,
            editor_user_id=input.editor_user_id,
            transition_reason=input.transition_reason,
            correction_note=(
                input.retraction_note if self.target_status == PublicationStatus.RETRACTED else None
            ),
        )
        # Run side-effect hooks inside the UoW so failures still roll
        # back the state change cleanly.
        await self._post_transition_hook(updated)
        await self._uow.commit()
        return updated


class SubmitPublicEventPage(_TransitionUseCase):
    """DRAFT -> IN_REVIEW."""

    target_status = PublicationStatus.IN_REVIEW


class RequestChanges(_TransitionUseCase):
    """IN_REVIEW -> DRAFT.

    Reviewer-driven send-back-for-changes.  The state machine treats
    this as the same transition as a reject from APPROVED but the
    typed use case keeps the audit trail clearer.
    """

    target_status = PublicationStatus.DRAFT


class ApprovePublicEventPage(_TransitionUseCase):
    """IN_REVIEW -> APPROVED."""

    target_status = PublicationStatus.APPROVED


class RejectPublicEventPage(_TransitionUseCase):
    """APPROVED -> DRAFT.

    Used when an approval is taken back before publication, e.g. the
    reviewer notices something stale and wants the editor to revisit.
    """

    target_status = PublicationStatus.DRAFT


class PublishPublicEventPage(_TransitionUseCase):
    """APPROVED -> PUBLISHED, or ARCHIVED -> PUBLISHED.

    Sets ``last_published_at`` and seeds ``first_published_at`` on the
    first publish.  Republishing an ARCHIVED page preserves the
    original ``first_published_at`` — useful for "this article was
    first published in 2023" framing.
    """

    target_status = PublicationStatus.PUBLISHED

    def _apply_status_mutation(
        self,
        page: PublicEventPage,
        *,
        now: datetime,
        input: TransitionPublicEventPageInput,
    ) -> PublicEventPage:
        return page.model_copy(
            update={
                "status": PublicationStatus.PUBLISHED,
                "first_published_at": page.first_published_at or now,
                "last_published_at": now,
                "version": page.version + 1,
                "updated_at": now,
            }
        )

    async def _post_transition_hook(self, page: PublicEventPage) -> None:
        # Pull the current projection and upsert both the search and
        # map index entries.  The hooks run inside the same UoW so a
        # failed index write rolls back the publish.
        await index_published_page(self._uow, page)
        await index_published_page_in_map(self._uow, page)


class ArchivePublicEventPage(_TransitionUseCase):
    """PUBLISHED -> ARCHIVED."""

    target_status = PublicationStatus.ARCHIVED

    async def _post_transition_hook(self, page: PublicEventPage) -> None:
        # Archived pages leave both the public search index and the
        # public map index.  Editorial callers can still find them
        # via ListEditorialPages.
        await remove_page_from_index(self._uow, page.id)
        await remove_page_from_map(self._uow, page.id)


class ReopenPublicEventPage(_TransitionUseCase):
    """ARCHIVED -> DRAFT.

    Lets editors take a previously-published page back for revision.
    Publication timestamps are preserved so the revision history
    still records when this content was first published.
    """

    target_status = PublicationStatus.DRAFT


class RetractPublicEventPage(_TransitionUseCase):
    """PUBLISHED -> RETRACTED (terminal).

    Stores the retraction note on the page row so the public 410
    response can surface it without joining to revisions.  The note is
    also captured in the revision's ``correction_note`` field for the
    audit trail.
    """

    target_status = PublicationStatus.RETRACTED

    def _apply_status_mutation(
        self,
        page: PublicEventPage,
        *,
        now: datetime,
        input: TransitionPublicEventPageInput,
    ) -> PublicEventPage:
        note = input.retraction_note
        return page.model_copy(
            update={
                "status": PublicationStatus.RETRACTED,
                "retracted_at": now,
                "retraction_note": (note or None) and note[:1000],
                "version": page.version + 1,
                "updated_at": now,
            }
        )

    async def _post_transition_hook(self, page: PublicEventPage) -> None:
        # Retracted pages must disappear from both search and map
        # immediately; the public surface routes them through the
        # 410-Gone path.
        await remove_page_from_index(self._uow, page.id)
        await remove_page_from_map(self._uow, page.id)


# ── Reads ────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class EditorialPageListItem:
    id: UUID
    slug: str
    title: str
    status: PublicationStatus
    version: int
    updated_at: datetime
    last_published_at: datetime | None
    allowed_next_statuses: list[PublicationStatus]


@dataclass(frozen=True)
class EditorialPageListResult:
    items: list[EditorialPageListItem]
    next_cursor: UUID | None
    limit: int


class ListEditorialPages:
    """List pages across editorial states (excludes RETRACTED by default).

    Mirrors the public list use case in shape but reads via
    ``list_editorial`` so the editor sees DRAFT/IN_REVIEW/APPROVED/
    PUBLISHED/ARCHIVED.  The response includes ``allowed_next_statuses``
    so the editorial UI can disable buttons without a second round-trip.
    """

    def __init__(self, uow: UnitOfWork):
        self._uow = uow

    async def execute(
        self,
        *,
        statuses: frozenset[PublicationStatus] | None = None,
        limit: int = 25,
        after_id: UUID | None = None,
    ) -> EditorialPageListResult:
        bounded_limit = max(1, min(limit, 100))
        page = await self._uow.public_event_pages.list_editorial(
            statuses=statuses, limit=bounded_limit, after_id=after_id
        )
        items = [
            EditorialPageListItem(
                id=row.id,
                slug=row.slug,
                title=row.title,
                status=row.status,
                version=row.version,
                updated_at=row.updated_at,
                last_published_at=row.last_published_at,
                allowed_next_statuses=sorted(allowed_next_states(row.status)),
            )
            for row in page.items
        ]
        return EditorialPageListResult(
            items=items, next_cursor=page.next_cursor, limit=bounded_limit
        )


class ListPageRevisions:
    """Return the full revision history for a page."""

    def __init__(self, uow: UnitOfWork):
        self._uow = uow

    async def execute(self, page_id: UUID) -> list[PublicEventPageRevision]:
        # 404 the request if the page is gone — saves the UI from
        # rendering a blank revision list with no context.
        await _load_page(self._uow, page_id)
        return await self._uow.public_event_pages.list_revisions(page_id)


__all__ = [
    "ApprovePublicEventPage",
    "ArchivePublicEventPage",
    "CreatePublicEventPage",
    "CreatePublicEventPageInput",
    "EditorialPageListItem",
    "EditorialPageListResult",
    "ListEditorialPages",
    "ListPageRevisions",
    "PublishPublicEventPage",
    "RejectPublicEventPage",
    "ReopenPublicEventPage",
    "RequestChanges",
    "RetractPublicEventPage",
    "SubmitPublicEventPage",
    "TransitionPublicEventPageInput",
    "UpdatePublicEventPage",
    "UpdatePublicEventPageInput",
]
