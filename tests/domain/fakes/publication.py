"""Fake public event page repository."""

from __future__ import annotations

from uuid import UUID

from atlas.domain.interfaces.repositories import (
    PublicEventPagePage,
    PublicEventPageRepository,
)
from atlas.domain.publication.entities import (
    PublicationStatus,
    PublicEventPage,
    PublicEventPageRevision,
)
from atlas.domain.publication.exceptions import (
    PublicEventPageAlreadyExistsError,
    PublicEventPageModifiedError,
    PublicEventPageNotFoundError,
    SlugAlreadyTakenError,
)
from tests.domain.fakes._store import (
    _PublicationStore,
)


class FakePublicEventPageRepository(PublicEventPageRepository):
    """In-memory fake mirroring the SQL repository invariants.

    Enforces slug uniqueness and one-page-per-event in domain space so
    use-case tests exercise the same error paths as production.  The
    Phase 9 extension adds the same optimistic-concurrency semantics
    on ``update`` and the append-only revision audit trail.
    """

    def __init__(self, s: _PublicationStore) -> None:
        self._s = s

    async def add(self, page: PublicEventPage) -> None:
        for existing in self._s.pages.values():
            if existing.slug == page.slug:
                raise SlugAlreadyTakenError(page.slug)
            if existing.event_id == page.event_id:
                raise PublicEventPageAlreadyExistsError(page.event_id)
        # Defensive copy: callers may mutate the page after add().
        # We model the DB's row-snapshot semantics by storing what was
        # passed in at this instant.
        self._s.pages[page.id] = page.model_copy(deep=True)

    async def update(self, page: PublicEventPage, *, expected_version: int) -> None:
        stored = self._s.pages.get(page.id)
        if stored is None:
            raise PublicEventPageNotFoundError(f"Public event page {page.id} not found")
        if stored.version != expected_version:
            raise PublicEventPageModifiedError(
                expected_version=expected_version,
                actual_version=stored.version,
            )
        # Slug uniqueness must still hold across the update.  The DB
        # enforces this via the unique index; the fake mirrors it.
        for other_id, other in self._s.pages.items():
            if other_id != page.id and other.slug == page.slug:
                raise SlugAlreadyTakenError(page.slug)
        self._s.pages[page.id] = page.model_copy(deep=True)

    async def get_by_id(self, page_id: UUID) -> PublicEventPage | None:
        page = self._s.pages.get(page_id)
        return page.model_copy(deep=True) if page is not None else None

    async def get_by_slug(self, slug: str) -> PublicEventPage | None:
        for page in self._s.pages.values():
            if page.slug == slug:
                return page.model_copy(deep=True)
        return None

    async def get_by_event_id(self, event_id: UUID) -> PublicEventPage | None:
        for page in self._s.pages.values():
            if page.event_id == event_id:
                return page.model_copy(deep=True)
        return None

    async def list_published(
        self,
        *,
        limit: int,
        after_id: UUID | None = None,
    ) -> PublicEventPagePage:
        published = [
            p
            for p in self._s.pages.values()
            if p.status == PublicationStatus.PUBLISHED and p.last_published_at is not None
        ]
        # Mirror the SQL ordering: (last_published_at DESC, id DESC)
        # Both halves of the key must agree on direction so the cursor
        # predicate is a well-defined "less than" comparison.
        published.sort(key=lambda p: (p.last_published_at, p.id), reverse=True)

        if after_id is not None:
            cursor = self._s.pages.get(after_id)
            if (
                cursor is not None
                and cursor.status == PublicationStatus.PUBLISHED
                and cursor.last_published_at is not None
            ):
                cursor_key = (cursor.last_published_at, cursor.id)
                published = [p for p in published if (p.last_published_at, p.id) < cursor_key]

        page_items = published[: limit + 1]
        if len(page_items) > limit:
            page_items = page_items[:limit]
            next_cursor: UUID | None = page_items[-1].id
        else:
            next_cursor = None
        # Defensive copy so callers can mutate without poisoning the
        # store, mirroring how the SQL repo returns fresh objects.
        return PublicEventPagePage(
            items=[p.model_copy(deep=True) for p in page_items],
            next_cursor=next_cursor,
        )

    async def list_editorial(
        self,
        *,
        statuses: frozenset[PublicationStatus] | None = None,
        limit: int,
        after_id: UUID | None = None,
    ) -> PublicEventPagePage:
        if statuses is None:
            active = frozenset(s for s in PublicationStatus if s != PublicationStatus.RETRACTED)
        else:
            active = statuses

        rows = [p for p in self._s.pages.values() if p.status in active]
        # (updated_at DESC, id DESC).  Same SQL ordering shape.
        rows.sort(key=lambda p: (p.updated_at, p.id), reverse=True)

        if after_id is not None:
            cursor = self._s.pages.get(after_id)
            if cursor is not None and cursor.status in active:
                cursor_key = (cursor.updated_at, cursor.id)
                rows = [r for r in rows if (r.updated_at, r.id) < cursor_key]

        page_items = rows[: limit + 1]
        if len(page_items) > limit:
            page_items = page_items[:limit]
            next_cursor: UUID | None = page_items[-1].id
        else:
            next_cursor = None
        return PublicEventPagePage(
            items=[p.model_copy(deep=True) for p in page_items],
            next_cursor=next_cursor,
        )

    async def add_revision(self, revision: PublicEventPageRevision) -> None:
        self._s.revisions.append(revision.model_copy(deep=True))

    async def list_revisions(self, page_id: UUID) -> list[PublicEventPageRevision]:
        revs = [r for r in self._s.revisions if r.page_id == page_id]
        revs.sort(key=lambda r: (r.version_at_moment, r.id))
        return [r.model_copy(deep=True) for r in revs]
