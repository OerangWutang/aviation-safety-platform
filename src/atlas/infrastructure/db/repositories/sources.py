"""SQLAlchemy repositories for the sources aggregate.

Carved out of the old ``repositories.py`` monolith in r9; behaviour
unchanged.  Public ``Sql*`` classes are re-exported from
``atlas.infrastructure.db.repositories`` so existing imports keep
working.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from atlas.domain.entities import (
    Source,
)
from atlas.domain.interfaces.repositories import (
    SourceRepository,
)
from atlas.infrastructure.db.orm_models import (
    SourceModel,
)
from atlas.infrastructure.db.repositories._helpers import (
    _chunked,
    _domain_data,
    _to_domain,
    _to_domain_opt,
)


class SqlSourceRepository(SourceRepository):
    def __init__(self, session: AsyncSession):
        self._session = session

    async def get(self, id: UUID) -> Source | None:
        obj = await self._session.get(SourceModel, id)
        return _to_domain_opt(obj, Source)

    async def get_by_name(self, name: str) -> Source | None:
        result = await self._session.execute(select(SourceModel).where(SourceModel.name == name))
        obj = result.scalar_one_or_none()
        return _to_domain_opt(obj, Source)

    async def get_all(self) -> list[Source]:
        result = await self._session.execute(select(SourceModel))
        return [_to_domain(obj, Source) for obj in result.scalars()]

    async def get_by_ids(self, ids: list[UUID]) -> list[Source]:
        if not ids:
            return []
        unique_ids = list(dict.fromkeys(ids))
        sources_by_id: dict[UUID, Source] = {}
        for chunk in _chunked(unique_ids):
            result = await self._session.execute(
                select(SourceModel).where(SourceModel.id.in_(chunk))
            )
            for obj in result.scalars():
                sources_by_id[obj.id] = _to_domain(obj, Source)
        return [sources_by_id[source_id] for source_id in unique_ids if source_id in sources_by_id]

    async def add(self, source: Source) -> None:
        self._session.add(SourceModel(**_domain_data(source)))

    async def update_field_mapping(
        self, source_id: UUID, field_mapping: dict[str, str]
    ) -> Source | None:
        """Persist ``field_mapping_json`` for ``source_id``.

        Returns the updated entity on success or ``None`` if the source does
        not exist.  Uses an UPDATE ... RETURNING so callers get the canonical
        post-write row in one round-trip and so a concurrent delete of the
        source surfaces immediately as ``None`` rather than as a silent no-op.
        """
        stmt = (
            update(SourceModel)
            .where(SourceModel.id == source_id)
            .values(field_mapping_json=field_mapping)
            .returning(SourceModel)
        )
        result = await self._session.execute(stmt)
        obj = result.scalar_one_or_none()
        return _to_domain_opt(obj, Source)
