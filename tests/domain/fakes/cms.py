"""Fake CMS (glossary, methodology, changelog) repositories."""

from __future__ import annotations

from uuid import UUID

from atlas.domain.cms.entities import (
    ChangelogEntry,
    ChangelogEntryRevision,
    GlossaryTerm,
    GlossaryTermRevision,
    MethodologyPage,
    MethodologyPageRevision,
)
from atlas.domain.cms.exceptions import CmsContentModifiedError
from atlas.domain.interfaces.repositories import (
    ChangelogEntryPage,
    ChangelogEntryRepository,
    ChangelogEntryRevisionRepository,
    GlossaryTermPage,
    GlossaryTermRepository,
    GlossaryTermRevisionRepository,
    MethodologyPagePage,
    MethodologyPageRepository,
    MethodologyPageRevisionRepository,
)
from atlas.domain.publication.entities import (
    PublicationStatus,
)
from tests.domain.fakes._store import (
    _CmsStore,
)


def _cms_keyset_paginate(rows: list, *, limit: int, after_id: UUID | None, key_attr: str):
    """Shared keyset-pagination helper for CMS fake list_editorial.

    ``key_attr`` is the timestamp attribute used as the primary sort
    (``updated_at`` for glossary/methodology/changelog editorial
    list; the public changelog list uses ``effective_date`` and goes
    through its own path).  Tie-break on id.  All three CMS fakes
    use this helper so the SQL/fake cursor semantics are pinned.
    """
    rows = sorted(rows, key=lambda r: (getattr(r, key_attr), r.id), reverse=True)
    if after_id is not None:
        cursor = next((r for r in rows if r.id == after_id), None)
        if cursor is not None:
            cursor_key = (getattr(cursor, key_attr), cursor.id)
            rows = [r for r in rows if (getattr(r, key_attr), r.id) < cursor_key]
    over = rows[: limit + 1]
    next_cursor: UUID | None = None
    if len(over) > limit:
        over = over[:limit]
        next_cursor = over[-1].id
    return over, next_cursor


class FakeGlossaryTermRepository(GlossaryTermRepository):
    def __init__(self, s: _CmsStore) -> None:
        self._s = s

    async def get(self, term_id: UUID) -> GlossaryTerm | None:
        row = self._s.glossary_terms.get(term_id)
        return row.model_copy(deep=True) if row else None

    async def get_by_term(self, term: str) -> GlossaryTerm | None:
        for t in self._s.glossary_terms.values():
            if t.term == term:
                return t.model_copy(deep=True)
        return None

    async def add(self, term: GlossaryTerm) -> None:
        # Enforce the unique constraint on ``term`` so behaviour
        # matches the SQL repo's IntegrityError path.
        for existing in self._s.glossary_terms.values():
            if existing.term == term.term:
                raise ValueError(f"Term {term.term!r} already exists")
        self._s.glossary_terms[term.id] = term.model_copy(deep=True)

    async def update(self, term: GlossaryTerm, *, expected_version: int) -> None:
        current = self._s.glossary_terms.get(term.id)
        if current is None:
            raise CmsContentModifiedError(
                kind="glossary_term",
                entity_id=term.id,
                expected_version=expected_version,
                actual_version=-1,
            )
        if current.version != expected_version:
            raise CmsContentModifiedError(
                kind="glossary_term",
                entity_id=term.id,
                expected_version=expected_version,
                actual_version=current.version,
            )
        self._s.glossary_terms[term.id] = term.model_copy(deep=True)

    async def list_published_terms(self) -> list[GlossaryTerm]:
        return sorted(
            (
                t.model_copy(deep=True)
                for t in self._s.glossary_terms.values()
                if t.status == PublicationStatus.PUBLISHED
            ),
            key=lambda t: t.term,
        )

    async def list_editorial(
        self,
        *,
        statuses=None,
        limit: int = 50,
        after_id: UUID | None = None,
    ) -> GlossaryTermPage:
        rows = list(self._s.glossary_terms.values())
        if statuses:
            rows = [r for r in rows if r.status in statuses]
        items, next_cursor = _cms_keyset_paginate(
            rows, limit=limit, after_id=after_id, key_attr="updated_at"
        )
        return GlossaryTermPage(
            items=[r.model_copy(deep=True) for r in items],
            next_cursor=next_cursor,
        )


class FakeGlossaryTermRevisionRepository(GlossaryTermRevisionRepository):
    def __init__(self, s: _CmsStore) -> None:
        self._s = s

    async def add(self, revision: GlossaryTermRevision) -> None:
        self._s.glossary_revisions.append(revision.model_copy(deep=True))

    async def list_for_term(self, term_id: UUID) -> list[GlossaryTermRevision]:
        return sorted(
            (r.model_copy(deep=True) for r in self._s.glossary_revisions if r.term_id == term_id),
            key=lambda r: r.created_at,
        )


class FakeMethodologyPageRepository(MethodologyPageRepository):
    def __init__(self, s: _CmsStore) -> None:
        self._s = s

    async def get(self, page_id: UUID) -> MethodologyPage | None:
        row = self._s.methodology_pages.get(page_id)
        return row.model_copy(deep=True) if row else None

    async def get_by_slug(self, slug: str) -> MethodologyPage | None:
        for p in self._s.methodology_pages.values():
            if p.slug == slug:
                return p.model_copy(deep=True)
        return None

    async def add(self, page: MethodologyPage) -> None:
        for existing in self._s.methodology_pages.values():
            if existing.slug == page.slug:
                raise ValueError(f"Slug {page.slug!r} already exists")
        self._s.methodology_pages[page.id] = page.model_copy(deep=True)

    async def update(self, page: MethodologyPage, *, expected_version: int) -> None:
        current = self._s.methodology_pages.get(page.id)
        if current is None:
            raise CmsContentModifiedError(
                kind="methodology_page",
                entity_id=page.id,
                expected_version=expected_version,
                actual_version=-1,
            )
        if current.version != expected_version:
            raise CmsContentModifiedError(
                kind="methodology_page",
                entity_id=page.id,
                expected_version=expected_version,
                actual_version=current.version,
            )
        self._s.methodology_pages[page.id] = page.model_copy(deep=True)

    async def list_published_grouped_by_section(
        self,
    ) -> list[MethodologyPage]:
        return sorted(
            (
                p.model_copy(deep=True)
                for p in self._s.methodology_pages.values()
                if p.status == PublicationStatus.PUBLISHED
            ),
            key=lambda p: (p.section, p.section_order, p.title),
        )

    async def list_editorial(
        self,
        *,
        statuses=None,
        limit: int = 50,
        after_id: UUID | None = None,
    ) -> MethodologyPagePage:
        rows = list(self._s.methodology_pages.values())
        if statuses:
            rows = [r for r in rows if r.status in statuses]
        items, next_cursor = _cms_keyset_paginate(
            rows, limit=limit, after_id=after_id, key_attr="updated_at"
        )
        return MethodologyPagePage(
            items=[r.model_copy(deep=True) for r in items],
            next_cursor=next_cursor,
        )


class FakeMethodologyPageRevisionRepository(MethodologyPageRevisionRepository):
    def __init__(self, s: _CmsStore) -> None:
        self._s = s

    async def add(self, revision: MethodologyPageRevision) -> None:
        self._s.methodology_revisions.append(revision.model_copy(deep=True))

    async def list_for_page(self, page_id: UUID) -> list[MethodologyPageRevision]:
        return sorted(
            (
                r.model_copy(deep=True)
                for r in self._s.methodology_revisions
                if r.page_id == page_id
            ),
            key=lambda r: r.created_at,
        )


class FakeChangelogEntryRepository(ChangelogEntryRepository):
    def __init__(self, s: _CmsStore) -> None:
        self._s = s

    async def get(self, entry_id: UUID) -> ChangelogEntry | None:
        row = self._s.changelog_entries.get(entry_id)
        return row.model_copy(deep=True) if row else None

    async def get_by_slug(self, slug: str) -> ChangelogEntry | None:
        for e in self._s.changelog_entries.values():
            if e.slug == slug:
                return e.model_copy(deep=True)
        return None

    async def add(self, entry: ChangelogEntry) -> None:
        for existing in self._s.changelog_entries.values():
            if existing.slug == entry.slug:
                raise ValueError(f"Slug {entry.slug!r} already exists")
        self._s.changelog_entries[entry.id] = entry.model_copy(deep=True)

    async def update(self, entry: ChangelogEntry, *, expected_version: int) -> None:
        current = self._s.changelog_entries.get(entry.id)
        if current is None:
            raise CmsContentModifiedError(
                kind="changelog_entry",
                entity_id=entry.id,
                expected_version=expected_version,
                actual_version=-1,
            )
        if current.version != expected_version:
            raise CmsContentModifiedError(
                kind="changelog_entry",
                entity_id=entry.id,
                expected_version=expected_version,
                actual_version=current.version,
            )
        self._s.changelog_entries[entry.id] = entry.model_copy(deep=True)

    async def list_published(
        self,
        *,
        limit: int = 50,
        after_id: UUID | None = None,
    ) -> ChangelogEntryPage:
        rows = [
            e for e in self._s.changelog_entries.values() if e.status == PublicationStatus.PUBLISHED
        ]
        # Public list sorts by ``effective_date DESC, id DESC``.
        rows.sort(key=lambda e: (e.effective_date, e.id), reverse=True)
        if after_id is not None:
            cursor = self._s.changelog_entries.get(after_id)
            if cursor is not None:
                cursor_key = (cursor.effective_date, cursor.id)
                rows = [r for r in rows if (r.effective_date, r.id) < cursor_key]
        over = rows[: limit + 1]
        next_cursor: UUID | None = None
        if len(over) > limit:
            over = over[:limit]
            next_cursor = over[-1].id
        return ChangelogEntryPage(
            items=[r.model_copy(deep=True) for r in over],
            next_cursor=next_cursor,
        )

    async def list_editorial(
        self,
        *,
        statuses=None,
        limit: int = 50,
        after_id: UUID | None = None,
    ) -> ChangelogEntryPage:
        rows = list(self._s.changelog_entries.values())
        if statuses:
            rows = [r for r in rows if r.status in statuses]
        items, next_cursor = _cms_keyset_paginate(
            rows, limit=limit, after_id=after_id, key_attr="updated_at"
        )
        return ChangelogEntryPage(
            items=[r.model_copy(deep=True) for r in items],
            next_cursor=next_cursor,
        )


class FakeChangelogEntryRevisionRepository(ChangelogEntryRevisionRepository):
    def __init__(self, s: _CmsStore) -> None:
        self._s = s

    async def add(self, revision: ChangelogEntryRevision) -> None:
        self._s.changelog_revisions.append(revision.model_copy(deep=True))

    async def list_for_entry(self, entry_id: UUID) -> list[ChangelogEntryRevision]:
        return sorted(
            (
                r.model_copy(deep=True)
                for r in self._s.changelog_revisions
                if r.entry_id == entry_id
            ),
            key=lambda r: r.created_at,
        )
