"""SQLAlchemy repository for ``public_event_pages``.

Phase 1 added the table + read paths.  Phase 9 adds the editorial
workflow: optimistic-concurrency updates, the editorial-side list,
and the append-only revision audit table.

The repository never commits — the use case owns the transaction
boundary via the unit-of-work pattern.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import literal, select, tuple_, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

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
from atlas.infrastructure.db.orm_models import (
    PublicEventPageModel,
    PublicEventPageRevisionModel,
)
from atlas.infrastructure.db.repositories._helpers import (
    _domain_data,
    _to_domain,
    _to_domain_opt,
)


class SqlPublicEventPageRepository(PublicEventPageRepository):
    def __init__(self, session: AsyncSession):
        self._session = session

    async def add(self, page: PublicEventPage) -> None:
        self._session.add(PublicEventPageModel(**_domain_data(page)))
        try:
            await self._session.flush()
        except IntegrityError as exc:
            # Map the DB-level uniqueness invariants onto typed domain
            # errors so the global FastAPI exception handler can render
            # them as 4xx responses without leaking SQLSTATE detail.
            #
            # We re-raise the original IntegrityError if neither index
            # matches: that means an FK or CHECK constraint blew up,
            # which is a domain-shape bug, not a duplicate.
            constraint = _extract_constraint_name(exc)
            if constraint == "uq_public_event_pages_slug":
                raise SlugAlreadyTakenError(page.slug) from exc
            if constraint == "uq_public_event_pages_event_id":
                raise PublicEventPageAlreadyExistsError(page.event_id) from exc
            raise

    async def update(self, page: PublicEventPage, *, expected_version: int) -> None:
        # Optimistic concurrency: WHERE id = ? AND version = ?
        # ``rowcount`` is the canonical signal — if zero rows update,
        # the version didn't match (or the row vanished).  Fall back
        # to a follow-up select to distinguish the two so callers get
        # the right error.
        stmt = (
            update(PublicEventPageModel)
            .where(
                PublicEventPageModel.id == page.id,
                PublicEventPageModel.version == expected_version,
            )
            .values(
                slug=page.slug,
                title=page.title,
                short_summary=page.short_summary,
                narrative_markdown=page.narrative_markdown,
                status=page.status.value,
                version=page.version,
                first_published_at=page.first_published_at,
                last_published_at=page.last_published_at,
                retracted_at=page.retracted_at,
                retraction_note=page.retraction_note,
                updated_at=page.updated_at,
            )
        )
        try:
            result = await self._session.execute(stmt)
        except IntegrityError as exc:
            constraint = _extract_constraint_name(exc)
            if constraint == "uq_public_event_pages_slug":
                raise SlugAlreadyTakenError(page.slug) from exc
            raise

        if getattr(result, "rowcount", 0) == 0:
            # Disambiguate: was the row gone, or did the version
            # diverge?  Both are 4xx but the codes differ.
            actual = await self._session.execute(
                select(PublicEventPageModel.version).where(PublicEventPageModel.id == page.id)
            )
            actual_version = actual.scalar_one_or_none()
            if actual_version is None:
                raise PublicEventPageNotFoundError(f"Public event page {page.id} not found")
            raise PublicEventPageModifiedError(
                expected_version=expected_version,
                actual_version=actual_version,
            )

    async def get_by_id(self, page_id: UUID) -> PublicEventPage | None:
        obj = await self._session.get(PublicEventPageModel, page_id)
        return _to_domain_opt(obj, PublicEventPage)

    async def get_by_slug(self, slug: str) -> PublicEventPage | None:
        result = await self._session.execute(
            select(PublicEventPageModel).where(PublicEventPageModel.slug == slug)
        )
        return _to_domain_opt(result.scalar_one_or_none(), PublicEventPage)

    async def get_by_event_id(self, event_id: UUID) -> PublicEventPage | None:
        result = await self._session.execute(
            select(PublicEventPageModel).where(PublicEventPageModel.event_id == event_id)
        )
        return _to_domain_opt(result.scalar_one_or_none(), PublicEventPage)

    async def list_published(
        self,
        *,
        limit: int,
        after_id: UUID | None = None,
    ) -> PublicEventPagePage:
        """Return a keyset-paginated page of PUBLISHED rows.

        Stable order is ``(last_published_at DESC, id DESC)``.  The
        partial index ``ix_public_event_pages_published_pub_id`` is
        designed for exactly this query plan.

        Fetching ``limit + 1`` and trimming back is the standard
        idiom for keyset pagination without an explicit count: the
        sentinel row tells us whether there is at least one more row
        without a second SQL round-trip.
        """
        stmt = (
            select(PublicEventPageModel)
            .where(PublicEventPageModel.status == PublicationStatus.PUBLISHED.value)
            .order_by(
                PublicEventPageModel.last_published_at.desc(),
                PublicEventPageModel.id.desc(),
            )
        )

        if after_id is not None:
            # Resolve the cursor row's ordering key in a separate
            # primary-key lookup, then bind it as a literal.  This keeps
            # the cursor predicate as a tuple comparison the planner can
            # combine with the partial index, instead of a correlated
            # subquery.  Mirrors the existing helper pattern in
            # ``_apply_created_at_uuid_cursor``.
            cursor_row = await self._session.execute(
                select(
                    PublicEventPageModel.last_published_at,
                    PublicEventPageModel.status,
                ).where(PublicEventPageModel.id == after_id)
            )
            cursor = cursor_row.first()
            if (
                cursor is not None
                and cursor.last_published_at is not None
                and cursor.status == PublicationStatus.PUBLISHED.value
            ):
                row_key = tuple_(
                    PublicEventPageModel.last_published_at,
                    PublicEventPageModel.id,
                )
                cursor_key = tuple_(literal(cursor.last_published_at), literal(after_id))
                stmt = stmt.where(row_key < cursor_key)
            # Invalid/stale/draft cursors fall through to "no cursor",
            # matching the convention used elsewhere in the repo.

        # Over-fetch by one to detect "more rows available" cheaply.
        result = await self._session.execute(stmt.limit(limit + 1))
        rows = list(result.scalars())
        if len(rows) > limit:
            rows = rows[:limit]
            next_cursor: UUID | None = rows[-1].id
        else:
            next_cursor = None

        return PublicEventPagePage(
            items=[_to_domain(row, PublicEventPage) for row in rows],
            next_cursor=next_cursor,
        )

    async def list_editorial(
        self,
        *,
        statuses: frozenset[PublicationStatus] | None = None,
        limit: int,
        after_id: UUID | None = None,
    ) -> PublicEventPagePage:
        """Editorial-side listing across any status set.

        Stable order is ``(updated_at DESC, id DESC)``.  Backed by
        ``ix_public_event_pages_status_updated`` from migration 036.

        Default ``statuses=None`` lists every non-terminal row, i.e.
        excludes RETRACTED.  That matches the editorial UI default:
        retracted pages have their own audit view and shouldn't
        clutter the active worklist.
        """
        active_statuses: frozenset[PublicationStatus]
        if statuses is None:
            active_statuses = frozenset(
                s for s in PublicationStatus if s != PublicationStatus.RETRACTED
            )
        else:
            active_statuses = statuses

        status_values = [s.value for s in active_statuses]
        stmt = (
            select(PublicEventPageModel)
            .where(PublicEventPageModel.status.in_(status_values))
            .order_by(
                PublicEventPageModel.updated_at.desc(),
                PublicEventPageModel.id.desc(),
            )
        )

        if after_id is not None:
            cursor_row = await self._session.execute(
                select(
                    PublicEventPageModel.updated_at,
                    PublicEventPageModel.status,
                ).where(PublicEventPageModel.id == after_id)
            )
            cursor = cursor_row.first()
            if cursor is not None and cursor.status in status_values:
                row_key = tuple_(
                    PublicEventPageModel.updated_at,
                    PublicEventPageModel.id,
                )
                cursor_key = tuple_(literal(cursor.updated_at), literal(after_id))
                stmt = stmt.where(row_key < cursor_key)

        result = await self._session.execute(stmt.limit(limit + 1))
        rows = list(result.scalars())
        if len(rows) > limit:
            rows = rows[:limit]
            next_cursor: UUID | None = rows[-1].id
        else:
            next_cursor = None

        return PublicEventPagePage(
            items=[_to_domain(row, PublicEventPage) for row in rows],
            next_cursor=next_cursor,
        )

    async def add_revision(self, revision: PublicEventPageRevision) -> None:
        self._session.add(PublicEventPageRevisionModel(**_domain_data(revision)))
        await self._session.flush()

    async def list_revisions(self, page_id: UUID) -> list[PublicEventPageRevision]:
        result = await self._session.execute(
            select(PublicEventPageRevisionModel)
            .where(PublicEventPageRevisionModel.page_id == page_id)
            .order_by(
                PublicEventPageRevisionModel.version_at_moment.asc(),
                PublicEventPageRevisionModel.id.asc(),
            )
        )
        return [_to_domain(row, PublicEventPageRevision) for row in result.scalars()]


def _extract_constraint_name(exc: IntegrityError) -> str | None:
    """Pull the constraint name out of an IntegrityError if available.

    asyncpg exposes the underlying ``ConstraintNameError``-style detail
    on ``exc.orig``.  We probe a few attribute paths because asyncpg's
    public surface has changed over versions; falling through to
    ``None`` is safe — the caller re-raises the original error.
    """
    orig = getattr(exc, "orig", None)
    if orig is None:
        return None
    name = getattr(orig, "constraint_name", None)
    if isinstance(name, str):
        return name
    # asyncpg's PostgresError keeps the SQLSTATE detail under .args
    # for older releases.  Best-effort: parse the message.
    message = str(orig)
    for candidate in (
        "uq_public_event_pages_slug",
        "uq_public_event_pages_event_id",
    ):
        if candidate in message:
            return candidate
    return None
