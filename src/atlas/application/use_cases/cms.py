"""CMS content use cases (Phase 10).

Three content kinds sharing one workflow.  Rather than make the Phase
9 :class:`_TransitionUseCase` generic over an entity type and risk
breaking the 84 Phase 9 tests, I write a parallel
:class:`_CmsTransition` helper that operates on a small
:class:`_CmsContentSlot` protocol.  Both helpers descend from the
same ``PublicationStatus`` + ``validate_transition`` core, so they
cannot disagree on the state machine.

The slot protocol gives the helper enough access to:

- read and mutate the entity's ``status``, ``version``,
  ``first_published_at``, ``last_published_at``, ``retraction_note``;
- ask the repository for the row, update it with optimistic-
  concurrency, and append a revision audit row.

The result is one transition implementation reused three times
(submit/approve/publish/reject/etc. for glossary, methodology,
changelog) without polymorphic typing tangling.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from atlas.application.unit_of_work import UnitOfWork
from atlas.domain.cms.entities import (
    ChangelogEntry,
    ChangelogEntryRevision,
    GlossaryTerm,
    GlossaryTermRevision,
    MethodologyPage,
    MethodologyPageRevision,
)
from atlas.domain.cms.exceptions import (
    ChangelogEntryNotFoundError,
    ChangelogEntryNotPublishedError,
    ChangelogEntryRetractedError,
    GlossaryTermNotFoundError,
    GlossaryTermNotPublishedError,
    GlossaryTermRetractedError,
    MethodologyPageNotFoundError,
    MethodologyPageNotPublishedError,
    MethodologyPageRetractedError,
)
from atlas.domain.enums import Role
from atlas.domain.publication.entities import PublicationStatus
from atlas.domain.publication.workflow import validate_transition
from atlas.domain.utils import utc_now

# ── Generic transition shape ────────────────────────────────────────────────


@dataclass(frozen=True)
class TransitionInput:
    """Inputs shared by every CMS transition.

    Same shape as Phase 9's :class:`TransitionPublicEventPageInput`
    but kind-agnostic.  ``entity_id`` is the row id of whichever
    content kind the use case targets.
    """

    entity_id: UUID
    expected_version: int
    editor_user_id: UUID
    transition_reason: str | None = None
    retraction_note: str | None = None


class _CmsContentSlot(Protocol):
    """The minimum surface a CMS content kind must expose for the
    shared transition machinery.

    Glossary / methodology / changelog all satisfy this; the
    concrete subclasses bind it to their specific entity, repo, and
    revision-row constructor.
    """

    kind_name: str

    async def get(self, entity_id: UUID) -> Any: ...
    async def update(self, entity: Any, *, expected_version: int) -> None: ...
    async def add_revision(
        self,
        *,
        entity_id: UUID,
        from_status: PublicationStatus,
        to_status: PublicationStatus,
        version: int,
        editor_user_id: UUID,
        transition_reason: str | None,
        correction_note: str | None,
    ) -> None: ...
    def not_found(self, entity_id: UUID) -> Exception: ...


# Slot adapters keep the repository details out of the transition
# core.  Each adapter binds the protocol to a concrete repository
# trio (content + revision + not-found-error type).


class _GlossarySlot:
    kind_name = "glossary_term"

    def __init__(self, uow: UnitOfWork):
        self._uow = uow

    async def get(self, entity_id: UUID) -> Any:
        return await self._uow.glossary_terms.get(entity_id)

    async def update(self, entity: Any, *, expected_version: int) -> None:
        await self._uow.glossary_terms.update(entity, expected_version=expected_version)

    async def add_revision(
        self,
        *,
        entity_id: UUID,
        from_status: PublicationStatus | None,
        to_status: PublicationStatus,
        version: int,
        editor_user_id: UUID,
        transition_reason: str | None,
        correction_note: str | None,
    ) -> None:
        await self._uow.glossary_term_revisions.add(
            GlossaryTermRevision(
                term_id=entity_id,
                from_status=from_status,
                to_status=to_status,
                version_at_revision=version,
                editor_user_id=editor_user_id,
                transition_reason=transition_reason,
                correction_note=correction_note,
            )
        )

    def not_found(self, entity_id: UUID) -> Exception:
        return GlossaryTermNotFoundError(f"Glossary term {entity_id} not found")


class _MethodologySlot:
    kind_name = "methodology_page"

    def __init__(self, uow: UnitOfWork):
        self._uow = uow

    async def get(self, entity_id: UUID) -> Any:
        return await self._uow.methodology_pages.get(entity_id)

    async def update(self, entity: Any, *, expected_version: int) -> None:
        await self._uow.methodology_pages.update(entity, expected_version=expected_version)

    async def add_revision(
        self,
        *,
        entity_id: UUID,
        from_status: PublicationStatus | None,
        to_status: PublicationStatus,
        version: int,
        editor_user_id: UUID,
        transition_reason: str | None,
        correction_note: str | None,
    ) -> None:
        await self._uow.methodology_page_revisions.add(
            MethodologyPageRevision(
                page_id=entity_id,
                from_status=from_status,
                to_status=to_status,
                version_at_revision=version,
                editor_user_id=editor_user_id,
                transition_reason=transition_reason,
                correction_note=correction_note,
            )
        )

    def not_found(self, entity_id: UUID) -> Exception:
        return MethodologyPageNotFoundError(f"Methodology page {entity_id} not found")


class _ChangelogSlot:
    kind_name = "changelog_entry"

    def __init__(self, uow: UnitOfWork):
        self._uow = uow

    async def get(self, entity_id: UUID) -> Any:
        return await self._uow.changelog_entries.get(entity_id)

    async def update(self, entity: Any, *, expected_version: int) -> None:
        await self._uow.changelog_entries.update(entity, expected_version=expected_version)

    async def add_revision(
        self,
        *,
        entity_id: UUID,
        from_status: PublicationStatus | None,
        to_status: PublicationStatus,
        version: int,
        editor_user_id: UUID,
        transition_reason: str | None,
        correction_note: str | None,
    ) -> None:
        await self._uow.changelog_entry_revisions.add(
            ChangelogEntryRevision(
                entry_id=entity_id,
                from_status=from_status,
                to_status=to_status,
                version_at_revision=version,
                editor_user_id=editor_user_id,
                transition_reason=transition_reason,
                correction_note=correction_note,
            )
        )

    def not_found(self, entity_id: UUID) -> Exception:
        return ChangelogEntryNotFoundError(f"Changelog entry {entity_id} not found")


# ── Generic transition machinery ────────────────────────────────────────────


class _CmsTransition:
    """One transition implementation reused for every CMS kind.

    Subclasses set ``target_status``.  The publish/retract transition
    classes also override ``_apply_status_mutation`` to manage the
    publication timestamps; everything else uses the default.
    """

    target_status: PublicationStatus

    def __init__(self, uow: UnitOfWork, slot: _CmsContentSlot):
        self._uow = uow
        self._slot = slot

    def _apply_status_mutation(self, entity: Any, *, now: datetime, input: TransitionInput) -> Any:
        return entity.model_copy(
            update={
                "status": self.target_status,
                "version": entity.version + 1,
                "updated_at": now,
            }
        )

    async def execute(self, input: TransitionInput) -> Any:
        entity = await self._slot.get(input.entity_id)
        if entity is None:
            raise self._slot.not_found(input.entity_id)
        from_status = entity.status
        validate_transition(from_status, self.target_status)

        moment = utc_now()
        updated = self._apply_status_mutation(entity, now=moment, input=input)
        await self._slot.update(updated, expected_version=input.expected_version)
        await self._slot.add_revision(
            entity_id=updated.id,
            from_status=from_status,
            to_status=self.target_status,
            version=updated.version,
            editor_user_id=input.editor_user_id,
            transition_reason=input.transition_reason,
            correction_note=(
                input.retraction_note if self.target_status == PublicationStatus.RETRACTED else None
            ),
        )
        await self._uow.commit()
        return updated


class _PublishMixin:
    """Mutation hook for the publish transition.

    Sets ``last_published_at`` always; sets ``first_published_at``
    only on the first publish ever.  Same shape as Phase 9.
    """

    target_status = PublicationStatus.PUBLISHED

    def _apply_status_mutation(self, entity: Any, *, now: datetime, input: TransitionInput) -> Any:
        return entity.model_copy(
            update={
                "status": PublicationStatus.PUBLISHED,
                "version": entity.version + 1,
                "updated_at": now,
                "last_published_at": now,
                "first_published_at": entity.first_published_at or now,
            }
        )


class _RetractMixin:
    target_status = PublicationStatus.RETRACTED

    def _apply_status_mutation(self, entity: Any, *, now: datetime, input: TransitionInput) -> Any:
        return entity.model_copy(
            update={
                "status": PublicationStatus.RETRACTED,
                "version": entity.version + 1,
                "updated_at": now,
                "retraction_note": input.retraction_note,
            }
        )


# ── Per-kind transition use cases ───────────────────────────────────────────
#
# Each kind gets six transitions: submit, request-changes, approve,
# reject, publish, archive, retract, reopen.  Same as Phase 9.  The
# concrete classes are intentionally short — they bind a slot to a
# target_status.


class SubmitGlossaryTerm(_CmsTransition):
    target_status = PublicationStatus.IN_REVIEW

    def __init__(self, uow: UnitOfWork):
        super().__init__(uow, _GlossarySlot(uow))


class ApproveGlossaryTerm(_CmsTransition):
    target_status = PublicationStatus.APPROVED

    def __init__(self, uow: UnitOfWork):
        super().__init__(uow, _GlossarySlot(uow))


class RequestChangesGlossaryTerm(_CmsTransition):
    target_status = PublicationStatus.DRAFT

    def __init__(self, uow: UnitOfWork):
        super().__init__(uow, _GlossarySlot(uow))


class RejectGlossaryTerm(_CmsTransition):
    target_status = PublicationStatus.DRAFT

    def __init__(self, uow: UnitOfWork):
        super().__init__(uow, _GlossarySlot(uow))


class PublishGlossaryTerm(_PublishMixin, _CmsTransition):
    def __init__(self, uow: UnitOfWork):
        super().__init__(uow, _GlossarySlot(uow))


class ArchiveGlossaryTerm(_CmsTransition):
    target_status = PublicationStatus.ARCHIVED

    def __init__(self, uow: UnitOfWork):
        super().__init__(uow, _GlossarySlot(uow))


class ReopenGlossaryTerm(_CmsTransition):
    target_status = PublicationStatus.DRAFT

    def __init__(self, uow: UnitOfWork):
        super().__init__(uow, _GlossarySlot(uow))


class RetractGlossaryTerm(_RetractMixin, _CmsTransition):
    def __init__(self, uow: UnitOfWork):
        super().__init__(uow, _GlossarySlot(uow))


class SubmitMethodologyPage(_CmsTransition):
    target_status = PublicationStatus.IN_REVIEW

    def __init__(self, uow: UnitOfWork):
        super().__init__(uow, _MethodologySlot(uow))


class ApproveMethodologyPage(_CmsTransition):
    target_status = PublicationStatus.APPROVED

    def __init__(self, uow: UnitOfWork):
        super().__init__(uow, _MethodologySlot(uow))


class RequestChangesMethodologyPage(_CmsTransition):
    target_status = PublicationStatus.DRAFT

    def __init__(self, uow: UnitOfWork):
        super().__init__(uow, _MethodologySlot(uow))


class RejectMethodologyPage(_CmsTransition):
    target_status = PublicationStatus.DRAFT

    def __init__(self, uow: UnitOfWork):
        super().__init__(uow, _MethodologySlot(uow))


class PublishMethodologyPage(_PublishMixin, _CmsTransition):
    def __init__(self, uow: UnitOfWork):
        super().__init__(uow, _MethodologySlot(uow))


class ArchiveMethodologyPage(_CmsTransition):
    target_status = PublicationStatus.ARCHIVED

    def __init__(self, uow: UnitOfWork):
        super().__init__(uow, _MethodologySlot(uow))


class ReopenMethodologyPage(_CmsTransition):
    target_status = PublicationStatus.DRAFT

    def __init__(self, uow: UnitOfWork):
        super().__init__(uow, _MethodologySlot(uow))


class RetractMethodologyPage(_RetractMixin, _CmsTransition):
    def __init__(self, uow: UnitOfWork):
        super().__init__(uow, _MethodologySlot(uow))


class SubmitChangelogEntry(_CmsTransition):
    target_status = PublicationStatus.IN_REVIEW

    def __init__(self, uow: UnitOfWork):
        super().__init__(uow, _ChangelogSlot(uow))


class ApproveChangelogEntry(_CmsTransition):
    target_status = PublicationStatus.APPROVED

    def __init__(self, uow: UnitOfWork):
        super().__init__(uow, _ChangelogSlot(uow))


class RequestChangesChangelogEntry(_CmsTransition):
    target_status = PublicationStatus.DRAFT

    def __init__(self, uow: UnitOfWork):
        super().__init__(uow, _ChangelogSlot(uow))


class RejectChangelogEntry(_CmsTransition):
    target_status = PublicationStatus.DRAFT

    def __init__(self, uow: UnitOfWork):
        super().__init__(uow, _ChangelogSlot(uow))


class PublishChangelogEntry(_PublishMixin, _CmsTransition):
    def __init__(self, uow: UnitOfWork):
        super().__init__(uow, _ChangelogSlot(uow))


class ArchiveChangelogEntry(_CmsTransition):
    target_status = PublicationStatus.ARCHIVED

    def __init__(self, uow: UnitOfWork):
        super().__init__(uow, _ChangelogSlot(uow))


class ReopenChangelogEntry(_CmsTransition):
    target_status = PublicationStatus.DRAFT

    def __init__(self, uow: UnitOfWork):
        super().__init__(uow, _ChangelogSlot(uow))


class RetractChangelogEntry(_RetractMixin, _CmsTransition):
    def __init__(self, uow: UnitOfWork):
        super().__init__(uow, _ChangelogSlot(uow))


# ── Create / update ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class CreateGlossaryTermInput:
    term: str
    display_term: str
    body_markdown: str
    editor_user_id: UUID


class CreateGlossaryTerm:
    """Create a new glossary term in DRAFT.

    The term key is case-sensitive and slug-shaped; uniqueness is
    enforced at the repository.  The use case doesn't reformat the
    caller's term — keep the surface honest about what's stored.
    """

    def __init__(self, uow: UnitOfWork):
        self._uow = uow

    async def execute(self, input: CreateGlossaryTermInput) -> GlossaryTerm:
        term = GlossaryTerm(
            term=input.term,
            display_term=input.display_term,
            body_markdown=input.body_markdown,
        )
        await self._uow.glossary_terms.add(term)
        await self._uow.glossary_term_revisions.add(
            GlossaryTermRevision(
                term_id=term.id,
                from_status=None,
                to_status=term.status,
                version_at_revision=term.version,
                editor_user_id=input.editor_user_id,
                transition_reason="created",
                correction_note=None,
            )
        )
        await self._uow.commit()
        return term


@dataclass(frozen=True)
class UpdateGlossaryTermInput:
    term_id: UUID
    expected_version: int
    display_term: str
    body_markdown: str
    editor_user_id: UUID


class UpdateGlossaryTerm:
    def __init__(self, uow: UnitOfWork):
        self._uow = uow

    async def execute(self, input: UpdateGlossaryTermInput) -> GlossaryTerm:
        existing = await self._uow.glossary_terms.get(input.term_id)
        if existing is None:
            raise GlossaryTermNotFoundError(f"Glossary term {input.term_id} not found")
        updated = existing.model_copy(
            update={
                "display_term": input.display_term,
                "body_markdown": input.body_markdown,
                "version": existing.version + 1,
                "updated_at": utc_now(),
            }
        )
        await self._uow.glossary_terms.update(updated, expected_version=input.expected_version)
        await self._uow.commit()
        return updated


@dataclass(frozen=True)
class CreateMethodologyPageInput:
    slug: str
    title: str
    section: str
    section_order: int
    body_markdown: str
    editor_user_id: UUID


class CreateMethodologyPage:
    def __init__(self, uow: UnitOfWork):
        self._uow = uow

    async def execute(self, input: CreateMethodologyPageInput) -> MethodologyPage:
        page = MethodologyPage(
            slug=input.slug,
            title=input.title,
            section=input.section,
            section_order=input.section_order,
            body_markdown=input.body_markdown,
        )
        await self._uow.methodology_pages.add(page)
        await self._uow.methodology_page_revisions.add(
            MethodologyPageRevision(
                page_id=page.id,
                from_status=None,
                to_status=page.status,
                version_at_revision=page.version,
                editor_user_id=input.editor_user_id,
                transition_reason="created",
                correction_note=None,
            )
        )
        await self._uow.commit()
        return page


@dataclass(frozen=True)
class UpdateMethodologyPageInput:
    page_id: UUID
    expected_version: int
    title: str
    section: str
    section_order: int
    body_markdown: str
    editor_user_id: UUID


class UpdateMethodologyPage:
    def __init__(self, uow: UnitOfWork):
        self._uow = uow

    async def execute(self, input: UpdateMethodologyPageInput) -> MethodologyPage:
        existing = await self._uow.methodology_pages.get(input.page_id)
        if existing is None:
            raise MethodologyPageNotFoundError(f"Methodology page {input.page_id} not found")
        updated = existing.model_copy(
            update={
                "title": input.title,
                "section": input.section,
                "section_order": input.section_order,
                "body_markdown": input.body_markdown,
                "version": existing.version + 1,
                "updated_at": utc_now(),
            }
        )
        await self._uow.methodology_pages.update(updated, expected_version=input.expected_version)
        await self._uow.commit()
        return updated


@dataclass(frozen=True)
class CreateChangelogEntryInput:
    slug: str
    title: str
    effective_date: Any  # date
    body_markdown: str
    editor_user_id: UUID


class CreateChangelogEntry:
    def __init__(self, uow: UnitOfWork):
        self._uow = uow

    async def execute(self, input: CreateChangelogEntryInput) -> ChangelogEntry:
        entry = ChangelogEntry(
            slug=input.slug,
            title=input.title,
            effective_date=input.effective_date,
            body_markdown=input.body_markdown,
        )
        await self._uow.changelog_entries.add(entry)
        await self._uow.changelog_entry_revisions.add(
            ChangelogEntryRevision(
                entry_id=entry.id,
                from_status=None,
                to_status=entry.status,
                version_at_revision=entry.version,
                editor_user_id=input.editor_user_id,
                transition_reason="created",
                correction_note=None,
            )
        )
        await self._uow.commit()
        return entry


@dataclass(frozen=True)
class UpdateChangelogEntryInput:
    entry_id: UUID
    expected_version: int
    title: str
    effective_date: Any
    body_markdown: str
    editor_user_id: UUID


class UpdateChangelogEntry:
    def __init__(self, uow: UnitOfWork):
        self._uow = uow

    async def execute(self, input: UpdateChangelogEntryInput) -> ChangelogEntry:
        existing = await self._uow.changelog_entries.get(input.entry_id)
        if existing is None:
            raise ChangelogEntryNotFoundError(f"Changelog entry {input.entry_id} not found")
        updated = existing.model_copy(
            update={
                "title": input.title,
                "effective_date": input.effective_date,
                "body_markdown": input.body_markdown,
                "version": existing.version + 1,
                "updated_at": utc_now(),
            }
        )
        await self._uow.changelog_entries.update(updated, expected_version=input.expected_version)
        await self._uow.commit()
        return updated


# ── Public reads (slug-keyed, visibility-aware) ─────────────────────────────


@dataclass(frozen=True)
class _LoadedGlossary:
    term: GlossaryTerm


async def _load_glossary_with_visibility(uow: UnitOfWork, term_key: str) -> _LoadedGlossary:
    """Same visibility contract as Phase 1's public-event-page reads.

    - PUBLISHED → return.
    - RETRACTED → raise (410 with retraction note).
    - Anything else → raise (404, doesn't leak existence of WIP).
    """
    found = await uow.glossary_terms.get_by_term(term_key)
    if found is None:
        raise GlossaryTermNotPublishedError(term_key)
    if found.status == PublicationStatus.RETRACTED:
        raise GlossaryTermRetractedError(term_key, found.retraction_note)
    if found.status != PublicationStatus.PUBLISHED:
        raise GlossaryTermNotPublishedError(term_key)
    return _LoadedGlossary(term=found)


class GetPublicGlossaryTerm:
    def __init__(self, uow: UnitOfWork):
        self._uow = uow

    async def execute(self, term: str) -> GlossaryTerm:
        loaded = await _load_glossary_with_visibility(self._uow, term)
        await self._uow.rollback()
        return loaded.term


class ListPublicGlossary:
    """All PUBLISHED glossary terms, sorted by term.

    Unbounded list because the glossary is small (dozens to low
    hundreds of entries).  No pagination cursor in Phase 10; if the
    collection ever crosses ~200, add one.
    """

    def __init__(self, uow: UnitOfWork):
        self._uow = uow

    async def execute(self) -> list[GlossaryTerm]:
        terms = await self._uow.glossary_terms.list_published_terms()
        await self._uow.rollback()
        return terms


class GetPublicMethodologyPage:
    def __init__(self, uow: UnitOfWork):
        self._uow = uow

    async def execute(self, slug: str) -> MethodologyPage:
        page = await self._uow.methodology_pages.get_by_slug(slug)
        if page is None:
            raise MethodologyPageNotPublishedError(slug)
        if page.status == PublicationStatus.RETRACTED:
            raise MethodologyPageRetractedError(slug, page.retraction_note)
        if page.status != PublicationStatus.PUBLISHED:
            raise MethodologyPageNotPublishedError(slug)
        await self._uow.rollback()
        return page


@dataclass(frozen=True)
class MethodologySectionView:
    """One section's worth of methodology pages, pre-grouped.

    The router returns a list of these so the UI can render the
    methodology nav without grouping client-side.
    """

    section: str
    pages: list[MethodologyPage]


class ListPublicMethodology:
    """All PUBLISHED methodology pages, grouped by section."""

    def __init__(self, uow: UnitOfWork):
        self._uow = uow

    async def execute(self) -> list[MethodologySectionView]:
        flat = await self._uow.methodology_pages.list_published_grouped_by_section()
        await self._uow.rollback()
        # Already sorted by (section, section_order, title).  Group.
        out: list[MethodologySectionView] = []
        current_section: str | None = None
        current_pages: list[MethodologyPage] = []
        for page in flat:
            if page.section != current_section:
                if current_section is not None:
                    out.append(MethodologySectionView(section=current_section, pages=current_pages))
                current_section = page.section
                current_pages = []
            current_pages.append(page)
        if current_section is not None:
            out.append(MethodologySectionView(section=current_section, pages=current_pages))
        return out


class GetPublicChangelogEntry:
    def __init__(self, uow: UnitOfWork):
        self._uow = uow

    async def execute(self, slug: str) -> ChangelogEntry:
        entry = await self._uow.changelog_entries.get_by_slug(slug)
        if entry is None:
            raise ChangelogEntryNotPublishedError(slug)
        if entry.status == PublicationStatus.RETRACTED:
            raise ChangelogEntryRetractedError(slug, entry.retraction_note)
        if entry.status != PublicationStatus.PUBLISHED:
            raise ChangelogEntryNotPublishedError(slug)
        await self._uow.rollback()
        return entry


@dataclass(frozen=True)
class ChangelogListResult:
    items: list[ChangelogEntry]
    next_cursor: UUID | None


_CHANGELOG_LIST_DEFAULT_LIMIT = 25
_CHANGELOG_LIST_MAX_LIMIT = 100


class ListPublicChangelog:
    """Keyset-paginated public changelog list.

    Ordered ``(effective_date DESC, id DESC)`` — readers see the
    most recently-effective changes first, with id as a stable
    tiebreak.
    """

    def __init__(self, uow: UnitOfWork):
        self._uow = uow

    async def execute(
        self,
        *,
        limit: int = _CHANGELOG_LIST_DEFAULT_LIMIT,
        after_id: UUID | None = None,
    ) -> ChangelogListResult:
        bounded_limit = max(1, min(limit, _CHANGELOG_LIST_MAX_LIMIT))
        page = await self._uow.changelog_entries.list_published(
            limit=bounded_limit, after_id=after_id
        )
        await self._uow.rollback()
        return ChangelogListResult(items=page.items, next_cursor=page.next_cursor)


# ── Role helpers ─────────────────────────────────────────────────────────────
#
# Same role gates as Phase 9: REVIEWER+ for transitions, ADMIN-only
# for retract.  Surfaced as helpers so the router doesn't need to
# re-state them per route.

EDITORIAL_ROLES = (Role.ADMIN, Role.REVIEWER, Role.ANALYST)
TRANSITION_ROLES = (Role.ADMIN, Role.REVIEWER)
RETRACT_ROLES = (Role.ADMIN,)


__all__ = [
    "EDITORIAL_ROLES",
    "RETRACT_ROLES",
    "TRANSITION_ROLES",
    "ApproveChangelogEntry",
    "ApproveGlossaryTerm",
    "ApproveMethodologyPage",
    "ArchiveChangelogEntry",
    "ArchiveGlossaryTerm",
    "ArchiveMethodologyPage",
    "ChangelogListResult",
    "CreateChangelogEntry",
    "CreateChangelogEntryInput",
    "CreateGlossaryTerm",
    "CreateGlossaryTermInput",
    "CreateMethodologyPage",
    "CreateMethodologyPageInput",
    "GetPublicChangelogEntry",
    "GetPublicGlossaryTerm",
    "GetPublicMethodologyPage",
    "ListPublicChangelog",
    "ListPublicGlossary",
    "ListPublicMethodology",
    "MethodologySectionView",
    "PublishChangelogEntry",
    "PublishGlossaryTerm",
    "PublishMethodologyPage",
    "RejectChangelogEntry",
    "RejectGlossaryTerm",
    "RejectMethodologyPage",
    "ReopenChangelogEntry",
    "ReopenGlossaryTerm",
    "ReopenMethodologyPage",
    "RequestChangesChangelogEntry",
    "RequestChangesGlossaryTerm",
    "RequestChangesMethodologyPage",
    "RetractChangelogEntry",
    "RetractGlossaryTerm",
    "RetractMethodologyPage",
    "SubmitChangelogEntry",
    "SubmitGlossaryTerm",
    "SubmitMethodologyPage",
    "TransitionInput",
    "UpdateChangelogEntry",
    "UpdateChangelogEntryInput",
    "UpdateGlossaryTerm",
    "UpdateGlossaryTermInput",
    "UpdateMethodologyPage",
    "UpdateMethodologyPageInput",
]
