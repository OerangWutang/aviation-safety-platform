"""SQL repositories for the NL search bounded context (Phase 7).

Two write-mostly repos.  The log is append-only (no read API beyond
direct SQL for analytics); the saved-query repo offers list + get +
delete scoped to the owning user.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from atlas.domain.interfaces.repositories import (
    NlQueryLogRepository,
    SavedNlQueryRepository,
)
from atlas.domain.nl_search.entities import NlQueryLog, SavedNlQuery
from atlas.infrastructure.db.orm_models import (
    NlQueryLogModel,
    SavedNlQueryModel,
)
from atlas.infrastructure.db.repositories._helpers import (
    _domain_data,
    _to_domain,
    _to_domain_opt,
)


class SqlNlQueryLogRepository(NlQueryLogRepository):
    def __init__(self, session: AsyncSession):
        self._session = session

    async def add(self, entry: NlQueryLog) -> None:
        self._session.add(NlQueryLogModel(**_domain_data(entry)))
        await self._session.flush()


class SqlSavedNlQueryRepository(SavedNlQueryRepository):
    def __init__(self, session: AsyncSession):
        self._session = session

    async def add(self, saved: SavedNlQuery) -> None:
        self._session.add(SavedNlQueryModel(**_domain_data(saved)))
        await self._session.flush()

    async def list_for_user(self, user_id: UUID, *, limit: int = 50) -> list[SavedNlQuery]:
        result = await self._session.execute(
            select(SavedNlQueryModel)
            .where(SavedNlQueryModel.user_id == user_id)
            .order_by(SavedNlQueryModel.created_at.desc())
            .limit(limit)
        )
        return [_to_domain(row, SavedNlQuery) for row in result.scalars()]

    async def get(self, saved_id: UUID) -> SavedNlQuery | None:
        obj = await self._session.get(SavedNlQueryModel, saved_id)
        return _to_domain_opt(obj, SavedNlQuery)

    async def delete_for_user(self, *, saved_id: UUID, user_id: UUID) -> bool:
        # Tenant-scoped predicate is the same belt-and-braces
        # pattern used by Phase 5: even if the auth gate fails, the
        # WHERE clause prevents cross-user delete.
        result = await self._session.execute(
            delete(SavedNlQueryModel).where(
                SavedNlQueryModel.id == saved_id,
                SavedNlQueryModel.user_id == user_id,
            )
        )
        return getattr(result, "rowcount", 0) > 0
