"""SQLAlchemy repositories for the ingestion aggregate.

Carved out of the old ``repositories.py`` monolith in r9; behaviour
unchanged.  Public ``Sql*`` classes are re-exported from
``atlas.infrastructure.db.repositories`` so existing imports keep
working.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from atlas.domain.entities import (
    IngestionRun,
)
from atlas.domain.exceptions import IngestionRunSourceMismatchError
from atlas.domain.interfaces.repositories import (
    IngestionRunRepository,
)
from atlas.infrastructure.db.orm_models import (
    IngestionRunModel,
)
from atlas.infrastructure.db.repositories._helpers import (
    _domain_data,
    _to_domain_opt,
)


class SqlIngestionRunRepository(IngestionRunRepository):
    def __init__(self, session: AsyncSession):
        self._session = session

    async def add(self, run: IngestionRun) -> None:
        self._session.add(IngestionRunModel(**_domain_data(run)))

    async def get(self, id: UUID) -> IngestionRun | None:
        obj = await self._session.get(IngestionRunModel, id)
        return _to_domain_opt(obj, IngestionRun)

    async def update_status(
        self,
        id: UUID,
        status: str,
        finished_at: object | None = None,
    ) -> None:
        # The abstract method types ``finished_at`` as ``object`` so callers
        # can pass either ``datetime`` or ``None`` ergonomically. Narrow it
        # back to ``datetime`` for actual use here.
        terminal_statuses = {"finished", "completed", "failed"}
        if isinstance(finished_at, datetime):
            effective_finished_at: datetime | None = finished_at
        elif finished_at is None and status in terminal_statuses:
            effective_finished_at = datetime.now(UTC)
        else:
            effective_finished_at = None
        result = await self._session.execute(
            update(IngestionRunModel)
            .where(IngestionRunModel.id == id)
            .values(status=status, finished_at=effective_finished_at)
        )
        if getattr(result, "rowcount", 0) != 1:
            raise RuntimeError(f"Failed to update ingestion run {id} to status {status!r}")

    async def ensure_started(self, id: UUID, source_id: UUID) -> None:
        """Insert a running ingestion run or verify the existing row's source.

        ``ON CONFLICT DO NOTHING`` keeps idempotent callers safe, but a
        conflicting deterministic run id that belongs to another source must not
        be silently accepted. Always re-read the row after the insert attempt and
        validate ownership.
        """
        stmt = insert(IngestionRunModel).values(id=id, source_id=source_id, status="running")
        stmt = stmt.on_conflict_do_nothing(index_elements=["id"])
        await self._session.execute(stmt)

        existing = await self._session.get(IngestionRunModel, id)
        if existing is None:
            raise RuntimeError(f"ensure_started: row missing after upsert for run {id}")
        if existing.source_id != source_id:
            raise IngestionRunSourceMismatchError(
                run_id=id,
                expected_source_id=source_id,
                actual_source_id=existing.source_id,
            )
