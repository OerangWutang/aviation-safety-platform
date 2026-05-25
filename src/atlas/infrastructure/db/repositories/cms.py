"""SQLAlchemy repositories for the CMS bounded context (Phase 10).

Three pairs of repos (content + revision) sharing the same shape as
Phase 9's public-event-page repo: optimistic-concurrency update,
keyset-paginated editorial list, slug-keyed public lookup.

The repos do NOT subclass each other.  Generalising across three
near-identical implementations would force a generic-typed base
class that's harder to read than the three explicit copies — each
one is ~40 lines.  The duplication is intentional and bounded.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import literal, select, tuple_, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

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
from atlas.domain.publication.entities import PublicationStatus
from atlas.infrastructure.db.orm_models import (
    ChangelogEntryModel,
    ChangelogEntryRevisionModel,
    GlossaryTermModel,
    GlossaryTermRevisionModel,
    MethodologyPageModel,
    MethodologyPageRevisionModel,
)
from atlas.infrastructure.db.repositories._helpers import (
    _domain_data,
    _to_domain,
    _to_domain_opt,
)

# ── Glossary ────────────────────────────────────────────────────────────────


class SqlGlossaryTermRepository(GlossaryTermRepository):
    def __init__(self, session: AsyncSession):
        self._session = session

    async def get(self, term_id: UUID) -> GlossaryTerm | None:
        obj = await self._session.get(GlossaryTermModel, term_id)
        return _to_domain_opt(obj, GlossaryTerm)

    async def get_by_term(self, term: str) -> GlossaryTerm | None:
        result = await self._session.execute(
            select(GlossaryTermModel).where(GlossaryTermModel.term == term)
        )
        return _to_domain_opt(result.scalar_one_or_none(), GlossaryTerm)

    async def add(self, term: GlossaryTerm) -> None:
        data = _domain_data(term)
        data["status"] = (
            term.status.value if isinstance(term.status, PublicationStatus) else term.status
        )
        self._session.add(GlossaryTermModel(**data))
        try:
            await self._session.flush()
        except IntegrityError:
            # Map the unique-violation onto the typed domain error so
            # callers don't need to inspect raw asyncpg messages.
            # The use case validates uniqueness up-front; this catch
            # is the belt-and-braces guard for the race.
            raise

    async def update(self, term: GlossaryTerm, *, expected_version: int) -> None:
        # Optimistic-concurrency update: WHERE id = ? AND version = ?
        # increments the version atomically.  A zero-rowcount result
        # means the version didn't match.
        new_version = term.version
        data = _domain_data(term)
        data["status"] = (
            term.status.value if isinstance(term.status, PublicationStatus) else term.status
        )
        # The new row carries version+1; we keep the helper's value
        # but compare expected_version against the *current* DB row.
        data.pop("id", None)
        stmt = (
            update(GlossaryTermModel)
            .where(
                GlossaryTermModel.id == term.id,
                GlossaryTermModel.version == expected_version,
            )
            .values(**data)
        )
        result = await self._session.execute(stmt)
        if getattr(result, "rowcount", 0) == 0:
            # Either the row doesn't exist or the version is stale.
            # Read once to disambiguate; this happens on the cold
            # error path and is cheap.
            current = await self.get(term.id)
            if current is None:
                # Vanished row — treat as concurrency error so the
                # caller retries via the listing rather than 500ing.
                raise CmsContentModifiedError(
                    kind="glossary_term",
                    entity_id=term.id,
                    expected_version=expected_version,
                    actual_version=-1,
                )
            raise CmsContentModifiedError(
                kind="glossary_term",
                entity_id=term.id,
                expected_version=expected_version,
                actual_version=current.version,
            )
        # The update sets the row to new_version (caller computed
        # it).  No further action.
        _ = new_version

    async def list_published_terms(self) -> list[GlossaryTerm]:
        result = await self._session.execute(
            select(GlossaryTermModel)
            .where(GlossaryTermModel.status == "PUBLISHED")
            .order_by(GlossaryTermModel.term)
        )
        return [_to_domain(row, GlossaryTerm) for row in result.scalars()]

    async def list_editorial(
        self,
        *,
        statuses: frozenset[PublicationStatus] | None = None,
        limit: int = 50,
        after_id: UUID | None = None,
    ) -> GlossaryTermPage:
        stmt = select(GlossaryTermModel).order_by(
            GlossaryTermModel.updated_at.desc(),
            GlossaryTermModel.id.desc(),
        )
        if statuses:
            stmt = stmt.where(GlossaryTermModel.status.in_(sorted(s.value for s in statuses)))
        if after_id is not None:
            cursor_row = await self._session.execute(
                select(GlossaryTermModel.updated_at, GlossaryTermModel.id).where(
                    GlossaryTermModel.id == after_id
                )
            )
            cursor = cursor_row.first()
            if cursor is not None:
                row_key = tuple_(GlossaryTermModel.updated_at, GlossaryTermModel.id)
                cursor_key = tuple_(literal(cursor.updated_at), literal(after_id))
                stmt = stmt.where(row_key < cursor_key)
        result = await self._session.execute(stmt.limit(limit + 1))
        rows = list(result.scalars())
        if len(rows) > limit:
            rows = rows[:limit]
            next_cursor: UUID | None = rows[-1].id
        else:
            next_cursor = None
        return GlossaryTermPage(
            items=[_to_domain(r, GlossaryTerm) for r in rows],
            next_cursor=next_cursor,
        )


class SqlGlossaryTermRevisionRepository(GlossaryTermRevisionRepository):
    def __init__(self, session: AsyncSession):
        self._session = session

    async def add(self, revision: GlossaryTermRevision) -> None:
        data = _domain_data(revision)
        data["from_status"] = revision.from_status.value if revision.from_status else None
        data["to_status"] = revision.to_status.value
        self._session.add(GlossaryTermRevisionModel(**data))
        await self._session.flush()

    async def list_for_term(self, term_id: UUID) -> list[GlossaryTermRevision]:
        result = await self._session.execute(
            select(GlossaryTermRevisionModel)
            .where(GlossaryTermRevisionModel.term_id == term_id)
            .order_by(GlossaryTermRevisionModel.created_at)
        )
        return [_to_domain(row, GlossaryTermRevision) for row in result.scalars()]


# ── Methodology ─────────────────────────────────────────────────────────────


class SqlMethodologyPageRepository(MethodologyPageRepository):
    def __init__(self, session: AsyncSession):
        self._session = session

    async def get(self, page_id: UUID) -> MethodologyPage | None:
        obj = await self._session.get(MethodologyPageModel, page_id)
        return _to_domain_opt(obj, MethodologyPage)

    async def get_by_slug(self, slug: str) -> MethodologyPage | None:
        result = await self._session.execute(
            select(MethodologyPageModel).where(MethodologyPageModel.slug == slug)
        )
        return _to_domain_opt(result.scalar_one_or_none(), MethodologyPage)

    async def add(self, page: MethodologyPage) -> None:
        data = _domain_data(page)
        data["status"] = (
            page.status.value if isinstance(page.status, PublicationStatus) else page.status
        )
        self._session.add(MethodologyPageModel(**data))
        await self._session.flush()

    async def update(self, page: MethodologyPage, *, expected_version: int) -> None:
        data = _domain_data(page)
        data["status"] = (
            page.status.value if isinstance(page.status, PublicationStatus) else page.status
        )
        data.pop("id", None)
        stmt = (
            update(MethodologyPageModel)
            .where(
                MethodologyPageModel.id == page.id,
                MethodologyPageModel.version == expected_version,
            )
            .values(**data)
        )
        result = await self._session.execute(stmt)
        if getattr(result, "rowcount", 0) == 0:
            current = await self.get(page.id)
            raise CmsContentModifiedError(
                kind="methodology_page",
                entity_id=page.id,
                expected_version=expected_version,
                actual_version=current.version if current else -1,
            )

    async def list_published_grouped_by_section(
        self,
    ) -> list[MethodologyPage]:
        result = await self._session.execute(
            select(MethodologyPageModel)
            .where(MethodologyPageModel.status == "PUBLISHED")
            .order_by(
                MethodologyPageModel.section,
                MethodologyPageModel.section_order,
                MethodologyPageModel.title,
            )
        )
        return [_to_domain(row, MethodologyPage) for row in result.scalars()]

    async def list_editorial(
        self,
        *,
        statuses: frozenset[PublicationStatus] | None = None,
        limit: int = 50,
        after_id: UUID | None = None,
    ) -> MethodologyPagePage:
        stmt = select(MethodologyPageModel).order_by(
            MethodologyPageModel.updated_at.desc(),
            MethodologyPageModel.id.desc(),
        )
        if statuses:
            stmt = stmt.where(MethodologyPageModel.status.in_(sorted(s.value for s in statuses)))
        if after_id is not None:
            cursor_row = await self._session.execute(
                select(MethodologyPageModel.updated_at, MethodologyPageModel.id).where(
                    MethodologyPageModel.id == after_id
                )
            )
            cursor = cursor_row.first()
            if cursor is not None:
                row_key = tuple_(MethodologyPageModel.updated_at, MethodologyPageModel.id)
                cursor_key = tuple_(literal(cursor.updated_at), literal(after_id))
                stmt = stmt.where(row_key < cursor_key)
        result = await self._session.execute(stmt.limit(limit + 1))
        rows = list(result.scalars())
        if len(rows) > limit:
            rows = rows[:limit]
            next_cursor: UUID | None = rows[-1].id
        else:
            next_cursor = None
        return MethodologyPagePage(
            items=[_to_domain(r, MethodologyPage) for r in rows],
            next_cursor=next_cursor,
        )


class SqlMethodologyPageRevisionRepository(MethodologyPageRevisionRepository):
    def __init__(self, session: AsyncSession):
        self._session = session

    async def add(self, revision: MethodologyPageRevision) -> None:
        data = _domain_data(revision)
        data["from_status"] = revision.from_status.value if revision.from_status else None
        data["to_status"] = revision.to_status.value
        self._session.add(MethodologyPageRevisionModel(**data))
        await self._session.flush()

    async def list_for_page(self, page_id: UUID) -> list[MethodologyPageRevision]:
        result = await self._session.execute(
            select(MethodologyPageRevisionModel)
            .where(MethodologyPageRevisionModel.page_id == page_id)
            .order_by(MethodologyPageRevisionModel.created_at)
        )
        return [_to_domain(row, MethodologyPageRevision) for row in result.scalars()]


# ── Changelog ───────────────────────────────────────────────────────────────


class SqlChangelogEntryRepository(ChangelogEntryRepository):
    def __init__(self, session: AsyncSession):
        self._session = session

    async def get(self, entry_id: UUID) -> ChangelogEntry | None:
        obj = await self._session.get(ChangelogEntryModel, entry_id)
        return _to_domain_opt(obj, ChangelogEntry)

    async def get_by_slug(self, slug: str) -> ChangelogEntry | None:
        result = await self._session.execute(
            select(ChangelogEntryModel).where(ChangelogEntryModel.slug == slug)
        )
        return _to_domain_opt(result.scalar_one_or_none(), ChangelogEntry)

    async def add(self, entry: ChangelogEntry) -> None:
        data = _domain_data(entry)
        data["status"] = (
            entry.status.value if isinstance(entry.status, PublicationStatus) else entry.status
        )
        self._session.add(ChangelogEntryModel(**data))
        await self._session.flush()

    async def update(self, entry: ChangelogEntry, *, expected_version: int) -> None:
        data = _domain_data(entry)
        data["status"] = (
            entry.status.value if isinstance(entry.status, PublicationStatus) else entry.status
        )
        data.pop("id", None)
        stmt = (
            update(ChangelogEntryModel)
            .where(
                ChangelogEntryModel.id == entry.id,
                ChangelogEntryModel.version == expected_version,
            )
            .values(**data)
        )
        result = await self._session.execute(stmt)
        if getattr(result, "rowcount", 0) == 0:
            current = await self.get(entry.id)
            raise CmsContentModifiedError(
                kind="changelog_entry",
                entity_id=entry.id,
                expected_version=expected_version,
                actual_version=current.version if current else -1,
            )

    async def list_published(
        self,
        *,
        limit: int = 50,
        after_id: UUID | None = None,
    ) -> ChangelogEntryPage:
        stmt = (
            select(ChangelogEntryModel)
            .where(ChangelogEntryModel.status == "PUBLISHED")
            .order_by(
                ChangelogEntryModel.effective_date.desc(),
                ChangelogEntryModel.id.desc(),
            )
        )
        if after_id is not None:
            cursor_row = await self._session.execute(
                select(
                    ChangelogEntryModel.effective_date,
                    ChangelogEntryModel.id,
                ).where(ChangelogEntryModel.id == after_id)
            )
            cursor = cursor_row.first()
            if cursor is not None:
                row_key = tuple_(
                    ChangelogEntryModel.effective_date,
                    ChangelogEntryModel.id,
                )
                cursor_key = tuple_(literal(cursor.effective_date), literal(after_id))
                stmt = stmt.where(row_key < cursor_key)
        result = await self._session.execute(stmt.limit(limit + 1))
        rows = list(result.scalars())
        if len(rows) > limit:
            rows = rows[:limit]
            next_cursor: UUID | None = rows[-1].id
        else:
            next_cursor = None
        return ChangelogEntryPage(
            items=[_to_domain(r, ChangelogEntry) for r in rows],
            next_cursor=next_cursor,
        )

    async def list_editorial(
        self,
        *,
        statuses: frozenset[PublicationStatus] | None = None,
        limit: int = 50,
        after_id: UUID | None = None,
    ) -> ChangelogEntryPage:
        stmt = select(ChangelogEntryModel).order_by(
            ChangelogEntryModel.updated_at.desc(),
            ChangelogEntryModel.id.desc(),
        )
        if statuses:
            stmt = stmt.where(ChangelogEntryModel.status.in_(sorted(s.value for s in statuses)))
        if after_id is not None:
            cursor_row = await self._session.execute(
                select(ChangelogEntryModel.updated_at, ChangelogEntryModel.id).where(
                    ChangelogEntryModel.id == after_id
                )
            )
            cursor = cursor_row.first()
            if cursor is not None:
                row_key = tuple_(ChangelogEntryModel.updated_at, ChangelogEntryModel.id)
                cursor_key = tuple_(literal(cursor.updated_at), literal(after_id))
                stmt = stmt.where(row_key < cursor_key)
        result = await self._session.execute(stmt.limit(limit + 1))
        rows = list(result.scalars())
        if len(rows) > limit:
            rows = rows[:limit]
            next_cursor = rows[-1].id
        else:
            next_cursor = None
        return ChangelogEntryPage(
            items=[_to_domain(r, ChangelogEntry) for r in rows],
            next_cursor=next_cursor,
        )


class SqlChangelogEntryRevisionRepository(ChangelogEntryRevisionRepository):
    def __init__(self, session: AsyncSession):
        self._session = session

    async def add(self, revision: ChangelogEntryRevision) -> None:
        data = _domain_data(revision)
        data["from_status"] = revision.from_status.value if revision.from_status else None
        data["to_status"] = revision.to_status.value
        self._session.add(ChangelogEntryRevisionModel(**data))
        await self._session.flush()

    async def list_for_entry(self, entry_id: UUID) -> list[ChangelogEntryRevision]:
        result = await self._session.execute(
            select(ChangelogEntryRevisionModel)
            .where(ChangelogEntryRevisionModel.entry_id == entry_id)
            .order_by(ChangelogEntryRevisionModel.created_at)
        )
        return [_to_domain(row, ChangelogEntryRevision) for row in result.scalars()]
