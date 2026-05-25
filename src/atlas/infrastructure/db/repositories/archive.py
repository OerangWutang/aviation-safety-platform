"""SQLAlchemy repositories for the archive aggregate.

Carved out of the old ``repositories.py`` monolith in r9; behaviour
unchanged.  Public ``Sql*`` classes are re-exported from
``atlas.infrastructure.db.repositories`` so existing imports keep
working.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from atlas.domain.entities import (
    ArchiveManifest,
)
from atlas.domain.interfaces.repositories import (
    ArchiveManifestRepository,
)
from atlas.infrastructure.db.orm_models import (
    ArchiveManifestModel,
)
from atlas.infrastructure.db.repositories._helpers import (
    _domain_data,
)


class SqlArchiveManifestRepository(ArchiveManifestRepository):
    def __init__(self, session: AsyncSession):
        self._session = session

    async def add(self, manifest: ArchiveManifest) -> None:
        self._session.add(ArchiveManifestModel(**_domain_data(manifest)))
