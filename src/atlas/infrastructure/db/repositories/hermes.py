"""SQLAlchemy repositories for the hermes aggregate.

Carved out of the old ``repositories.py`` monolith in r9; behaviour
unchanged.  Public ``Sql*`` classes are re-exported from
``atlas.infrastructure.db.repositories`` so existing imports keep
working.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import case, func, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from atlas.domain.entities import (
    HermesCrawlTarget,
    HermesFetchedDocument,
    HermesFetchJob,
    HermesSource,
    HermesSourceChange,
)
from atlas.domain.enums import (
    HermesFetchJobStatus,
    HermesTargetStatus,
)
from atlas.domain.exceptions import ConcurrentUpsertError
from atlas.domain.interfaces.repositories import (
    HermesCrawlTargetRepository,
    HermesFetchedDocumentRepository,
    HermesFetchJobRepository,
    HermesRecoveryOutcome,
    HermesSourceChangeRepository,
    HermesSourceRepository,
)
from atlas.infrastructure.db.orm_models import (
    HermesCrawlTargetModel,
    HermesFetchedDocumentModel,
    HermesFetchJobModel,
    HermesSourceChangeModel,
    HermesSourceModel,
)
from atlas.infrastructure.db.repositories._helpers import (
    _domain_data,
    _to_domain,
)


def _hermes_source_to_domain(row: HermesSourceModel) -> HermesSource:
    return _to_domain(row, HermesSource)


def _hermes_target_to_domain(row: HermesCrawlTargetModel) -> HermesCrawlTarget:
    return _to_domain(row, HermesCrawlTarget)


def _hermes_job_to_domain(row: HermesFetchJobModel) -> HermesFetchJob:
    return _to_domain(row, HermesFetchJob)


def _hermes_doc_to_domain(row: HermesFetchedDocumentModel) -> HermesFetchedDocument:
    return _to_domain(row, HermesFetchedDocument)


def _hermes_change_to_domain(row: HermesSourceChangeModel) -> HermesSourceChange:
    return _to_domain(row, HermesSourceChange)


class SqlHermesSourceRepository(HermesSourceRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, id: UUID) -> HermesSource | None:
        row = await self._session.get(HermesSourceModel, id)
        return _hermes_source_to_domain(row) if row else None

    async def add(self, source: HermesSource) -> None:
        self._session.add(HermesSourceModel(**_domain_data(source)))

    async def find_by_name(self, name: str) -> HermesSource | None:
        result = await self._session.execute(
            select(HermesSourceModel).where(
                func.lower(HermesSourceModel.name) == name.lower().strip()
            )
        )
        row = result.scalar_one_or_none()
        return _hermes_source_to_domain(row) if row else None

    async def add_or_get_by_name(self, source: HermesSource) -> tuple[HermesSource, bool]:
        """Atomically insert a new Hermes source or return an existing one.

        Uses ``INSERT … ON CONFLICT (lower(name)) DO NOTHING RETURNING *``
        against the functional unique index ``uq_hermes_sources_name_lower``.
        On the DO-NOTHING path (a source with the same lowercased name already
        exists), we re-select the pre-existing row and return it with
        ``created=False``, making the operation fully race-safe under concurrent
        registration callers.

        ``add()`` remains for backwards-compat but callers that need
        idempotency should prefer this method.
        """
        stmt = (
            insert(HermesSourceModel)
            .values(**_domain_data(source))
            .on_conflict_do_nothing(
                index_elements=[text("lower(name)")],
            )
            .returning(HermesSourceModel)
        )
        result = await self._session.execute(stmt)
        row = result.scalar_one_or_none()
        if row is not None:
            return _hermes_source_to_domain(row), True
        # DO NOTHING: re-select the pre-existing row by normalized name.
        existing = await self.find_by_name(source.name)
        if existing is not None:
            return existing, False
        raise ConcurrentUpsertError(
            f"HermesSource upsert: ON CONFLICT (lower(name)) fired for "
            f"name={source.name!r} but re-select found no existing row."
        )

    async def list_active(self, limit: int = 100, offset: int = 0) -> list[HermesSource]:
        result = await self._session.execute(
            select(HermesSourceModel)
            .where(HermesSourceModel.is_active.is_(True))
            .order_by(HermesSourceModel.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        return [_hermes_source_to_domain(r) for r in result.scalars().all()]


class SqlHermesCrawlTargetRepository(HermesCrawlTargetRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, id: UUID) -> HermesCrawlTarget | None:
        row = await self._session.get(HermesCrawlTargetModel, id)
        return _hermes_target_to_domain(row) if row else None

    async def add(self, target: HermesCrawlTarget) -> None:
        self._session.add(HermesCrawlTargetModel(**_domain_data(target)))

    async def save(self, target: HermesCrawlTarget) -> None:
        row = await self._session.get(HermesCrawlTargetModel, target.id)
        if row is None:
            self._session.add(HermesCrawlTargetModel(**_domain_data(target)))
            return
        for k, v in _domain_data(target).items():
            setattr(row, k, v)

    async def find_by_normalized_url(self, normalized_url: str) -> HermesCrawlTarget | None:
        result = await self._session.execute(
            select(HermesCrawlTargetModel).where(
                HermesCrawlTargetModel.normalized_url == normalized_url
            )
        )
        row = result.scalar_one_or_none()
        return _hermes_target_to_domain(row) if row else None

    async def add_or_get_by_normalized_url(
        self, target: HermesCrawlTarget
    ) -> tuple[HermesCrawlTarget, bool]:
        """Atomically insert a crawl target or return an existing one.

        Uses ``INSERT … ON CONFLICT (normalized_url) DO NOTHING RETURNING *``.
        ``normalized_url`` carries a simple ``unique=True`` column constraint,
        so two concurrent ``CreateHermesCrawlTarget`` calls for the same URL
        are both safe: one inserts, the other silently does nothing and
        re-selects the pre-existing row.
        """
        stmt = (
            insert(HermesCrawlTargetModel)
            .values(**_domain_data(target))
            .on_conflict_do_nothing(
                index_elements=["normalized_url"],
            )
            .returning(HermesCrawlTargetModel)
        )
        result = await self._session.execute(stmt)
        row = result.scalar_one_or_none()
        if row is not None:
            return _hermes_target_to_domain(row), True
        existing = await self.find_by_normalized_url(target.normalized_url)
        if existing is not None:
            return existing, False
        raise ConcurrentUpsertError(
            f"HermesCrawlTarget upsert: ON CONFLICT (normalized_url) fired for "
            f"url={target.normalized_url!r} but re-select found no existing row."
        )

    async def list(
        self,
        status: HermesTargetStatus | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[HermesCrawlTarget]:
        q = select(HermesCrawlTargetModel)
        if status is not None:
            q = q.where(HermesCrawlTargetModel.status == status.value)
        q = q.order_by(HermesCrawlTargetModel.created_at.desc()).limit(limit).offset(offset)
        result = await self._session.execute(q)
        return [_hermes_target_to_domain(r) for r in result.scalars().all()]


class SqlHermesFetchJobRepository(HermesFetchJobRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, id: UUID) -> HermesFetchJob | None:
        row = await self._session.get(HermesFetchJobModel, id)
        return _hermes_job_to_domain(row) if row else None

    async def add(self, job: HermesFetchJob) -> None:
        self._session.add(HermesFetchJobModel(**_domain_data(job)))

    async def save(self, job: HermesFetchJob) -> None:
        row = await self._session.get(HermesFetchJobModel, job.id)
        if row is None:
            self._session.add(HermesFetchJobModel(**_domain_data(job)))
            return
        for k, v in _domain_data(job).items():
            setattr(row, k, v)

    async def find_active_for_target(self, target_id: UUID) -> HermesFetchJob | None:
        result = await self._session.execute(
            select(HermesFetchJobModel)
            .where(
                HermesFetchJobModel.target_id == target_id,
                HermesFetchJobModel.status.in_(["RUNNING", "QUEUED"]),
            )
            .order_by(HermesFetchJobModel.created_at.asc())
            .limit(1)
        )
        row = result.scalar_one_or_none()
        return _hermes_job_to_domain(row) if row else None

    async def add_or_get_active_for_target(
        self, job: HermesFetchJob
    ) -> tuple[HermesFetchJob, bool]:
        """Atomically enqueue a job or return the existing active one.

        Uses ``INSERT … ON CONFLICT DO NOTHING`` targeting the partial unique
        index ``uq_hermes_fetch_jobs_one_active_per_target`` (target_id WHERE
        status IN ('QUEUED','RUNNING')).  Two concurrent enqueue calls for the
        same target produce at most one row.

        On the DO-NOTHING path we re-select the existing job via
        ``find_active_for_target``.  If the conflict fired but no row is
        found (e.g. because the conflicting job was completed between the
        INSERT and the re-select), we raise to avoid returning an unpersisted
        object.
        """
        stmt = (
            insert(HermesFetchJobModel)
            .values(**_domain_data(job))
            .on_conflict_do_nothing(
                index_elements=["target_id"],
                index_where=text("status IN ('QUEUED', 'RUNNING')"),
            )
            .returning(HermesFetchJobModel)
        )
        result = await self._session.execute(stmt)
        row = result.scalar_one_or_none()
        if row is not None:
            return _hermes_job_to_domain(row), True
        # DO NOTHING: re-select the pre-existing active job.
        existing = await self.find_active_for_target(job.target_id)
        if existing is not None:
            return existing, False
        # The conflict fired but the job completed (transitioned out of
        # QUEUED/RUNNING) before our re-select.  Fail loudly so the caller
        # can retry rather than receiving a phantom unpersisted object.
        raise ConcurrentUpsertError(
            f"HermesFetchJob upsert: ON CONFLICT (one_active_per_target) fired for "
            f"target_id={job.target_id} but find_active_for_target returned None.  "
            "The conflicting job may have completed between INSERT and re-select.  Retry."
        )

    async def get_next_queued(self) -> HermesFetchJob | None:
        """Return and lock the next due QUEUED job without changing status.

        Prefer ``claim_next_running`` for workers.  This lower-level method is
        kept for compatibility with diagnostics that need to inspect the next
        due job inside a transaction.

        Like :meth:`claim_next_running`, we avoid relying on
        ``session.get(...)`` to materialise the result: when the session has
        previously touched the row, the identity map would return the cached
        instance.  Within ``get_next_queued`` that is usually fine because we
        do not change status, but the row may still differ from its cached
        copy when other transactions have updated it concurrently; the
        ``FOR UPDATE`` row lock guarantees the values we have *here* are the
        current ones.  Refresh the cached instance from the SELECT mapping
        so callers always see the row that the lock protects.
        """
        stmt = text(
            """
            WITH next_job AS (
                SELECT id
                FROM hermes_fetch_jobs
                WHERE status = 'QUEUED'
                  AND (scheduled_at IS NULL OR scheduled_at <= now())
                ORDER BY priority ASC, created_at ASC
                LIMIT 1
                FOR UPDATE SKIP LOCKED
            )
            SELECT j.*
            FROM hermes_fetch_jobs j
            JOIN next_job n ON j.id = n.id
            """
        )
        result = await self._session.execute(stmt)
        row = result.mappings().first()
        if row is None:
            return None
        cached = await self._session.get(HermesFetchJobModel, row["id"])
        if cached is not None:
            for key, value in row.items():
                if hasattr(cached, key):
                    setattr(cached, key, value)
            return _hermes_job_to_domain(cached)
        orm_row = HermesFetchJobModel(**dict(row))
        return _hermes_job_to_domain(orm_row)

    async def claim_running(
        self,
        job_id: UUID,
        *,
        worker_id: str,
        lease_expires_at: datetime,
    ) -> HermesFetchJob | None:
        """Atomically transition a due QUEUED job to RUNNING with a lease."""
        now = datetime.now(UTC)
        stmt = (
            update(HermesFetchJobModel)
            .where(
                HermesFetchJobModel.id == job_id,
                HermesFetchJobModel.status == HermesFetchJobStatus.QUEUED.value,
                (HermesFetchJobModel.scheduled_at.is_(None))
                | (HermesFetchJobModel.scheduled_at <= now),
            )
            .values(
                status=HermesFetchJobStatus.RUNNING.value,
                attempt_count=HermesFetchJobModel.attempt_count + 1,
                started_at=now,
                finished_at=None,
                error_message=None,
                locked_by=worker_id,
                locked_at=now,
                lease_expires_at=lease_expires_at,
                updated_at=now,
            )
            .returning(HermesFetchJobModel)
        )
        result = await self._session.execute(stmt)
        row = result.scalar_one_or_none()
        return _hermes_job_to_domain(row) if row else None

    async def claim_next_running(
        self,
        *,
        worker_id: str,
        lease_expires_at: datetime,
    ) -> HermesFetchJob | None:
        """Atomically claim the next due queued job and transition it to RUNNING.

        The ``RETURNING j.*`` clause yields the post-update column values
        directly, so we materialise the domain object from the mapping
        rather than calling ``session.get(...)``.  Going through
        ``session.get`` is correct in the fresh-session worker path, but it
        can return the *cached* QUEUED ORM instance if the same session
        already loaded the job, hiding the lease/status update from the
        caller.  Mapping from the mapping is robust regardless of the
        session's identity-map state.
        """
        now = datetime.now(UTC)
        stmt = text(
            """
            WITH next_job AS (
                SELECT id
                FROM hermes_fetch_jobs
                WHERE status = :queued
                  AND (scheduled_at IS NULL OR scheduled_at <= :now)
                ORDER BY priority ASC, created_at ASC, id ASC
                LIMIT 1
                FOR UPDATE SKIP LOCKED
            )
            UPDATE hermes_fetch_jobs AS j
            SET status = :running,
                attempt_count = j.attempt_count + 1,
                started_at = :now,
                finished_at = NULL,
                error_message = NULL,
                locked_by = :worker_id,
                locked_at = :now,
                lease_expires_at = :lease_expires_at,
                updated_at = :now
            FROM next_job
            WHERE j.id = next_job.id
            RETURNING j.*
            """
        )
        result = await self._session.execute(
            stmt,
            {
                "queued": HermesFetchJobStatus.QUEUED.value,
                "running": HermesFetchJobStatus.RUNNING.value,
                "now": now,
                "worker_id": worker_id,
                "lease_expires_at": lease_expires_at,
            },
        )
        row = result.mappings().first()
        if row is None:
            return None
        # If the row is already in the identity map (e.g. previously loaded
        # as QUEUED in this session), refresh it from the returned row so
        # callers do not see stale ORM state.
        cached = await self._session.get(HermesFetchJobModel, row["id"])
        if cached is not None:
            for key, value in row.items():
                if hasattr(cached, key):
                    setattr(cached, key, value)
            return _hermes_job_to_domain(cached)
        # No cached instance: build the domain object directly from the
        # post-update row.  This avoids a second round-trip just to satisfy
        # session.get and is safe because RETURNING j.* yields a complete
        # column set.
        orm_row = HermesFetchJobModel(**dict(row))
        return _hermes_job_to_domain(orm_row)

    async def lock_claim_for_finalization(
        self,
        job_id: UUID,
        *,
        worker_id: str,
        attempt_count: int,
        now: datetime,
    ) -> HermesFetchJob | None:
        """Lock the RUNNING row only if this worker still owns its live lease.

        This is the fencing check that prevents an old/slow worker from writing
        documents and final job state after the lease has expired and another
        worker has recovered or re-claimed the job.
        """
        stmt = (
            select(HermesFetchJobModel)
            .where(
                HermesFetchJobModel.id == job_id,
                HermesFetchJobModel.status == HermesFetchJobStatus.RUNNING.value,
                HermesFetchJobModel.locked_by == worker_id,
                HermesFetchJobModel.attempt_count == attempt_count,
                HermesFetchJobModel.lease_expires_at.is_not(None),
                HermesFetchJobModel.lease_expires_at >= now,
            )
            .with_for_update()
        )
        result = await self._session.execute(stmt)
        row = result.scalar_one_or_none()
        return _hermes_job_to_domain(row) if row else None

    async def recover_stale_running(
        self,
        *,
        now: datetime,
        limit: int = 100,
    ) -> list[HermesRecoveryOutcome]:
        """Recover RUNNING jobs whose lease expired before finalization.

        Jobs with attempts remaining are returned to QUEUED and made due now.
        Jobs that have exhausted attempts are marked FAILED.  Lock metadata is
        cleared in both cases so the partial active-job uniqueness invariant no
        longer traps a target behind a dead worker claim.

        Returns one :class:`HermesRecoveryOutcome` per recovered job (via
        the ``RETURNING`` clause) so the caller can emit source-change
        audit rows for terminally failed jobs.  This is observability, not
        correctness — the previous int-returning shape was strictly less
        useful for the same atomic transaction.
        """
        ids_stmt = text(
            """
            SELECT id
            FROM hermes_fetch_jobs
            WHERE status = :running
              AND lease_expires_at IS NOT NULL
              AND lease_expires_at < :now
            ORDER BY lease_expires_at ASC, id ASC
            LIMIT :limit
            FOR UPDATE SKIP LOCKED
            """
        )
        ids_result = await self._session.execute(
            ids_stmt,
            {
                "running": HermesFetchJobStatus.RUNNING.value,
                "now": now,
                "limit": limit,
            },
        )
        ids = [row[0] for row in ids_result.fetchall()]
        if not ids:
            return []
        stmt = (
            update(HermesFetchJobModel)
            .where(HermesFetchJobModel.id.in_(ids))
            .values(
                status=case(
                    (
                        HermesFetchJobModel.attempt_count >= HermesFetchJobModel.max_attempts,
                        HermesFetchJobStatus.FAILED.value,
                    ),
                    else_=HermesFetchJobStatus.QUEUED.value,
                ),
                scheduled_at=case(
                    (
                        HermesFetchJobModel.attempt_count >= HermesFetchJobModel.max_attempts,
                        None,
                    ),
                    else_=now,
                ),
                finished_at=now,
                error_message="Recovered stale RUNNING Hermes job after lease expiry",
                locked_by=None,
                locked_at=None,
                lease_expires_at=None,
                updated_at=now,
            )
            .returning(
                HermesFetchJobModel.id,
                HermesFetchJobModel.target_id,
                HermesFetchJobModel.status,
                HermesFetchJobModel.attempt_count,
            )
        )
        result = await self._session.execute(stmt)
        return [
            HermesRecoveryOutcome(
                job_id=row.id,
                target_id=row.target_id,
                final_status=HermesFetchJobStatus(row.status),
                attempt_count=row.attempt_count,
            )
            for row in result.all()
        ]

    async def list(
        self,
        status: HermesFetchJobStatus | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[HermesFetchJob]:
        q = select(HermesFetchJobModel)
        if status is not None:
            q = q.where(HermesFetchJobModel.status == status.value)
        q = q.order_by(HermesFetchJobModel.created_at.desc()).limit(limit).offset(offset)
        result = await self._session.execute(q)
        return [_hermes_job_to_domain(r) for r in result.scalars().all()]


class SqlHermesFetchedDocumentRepository(HermesFetchedDocumentRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, id: UUID) -> HermesFetchedDocument | None:
        row = await self._session.get(HermesFetchedDocumentModel, id)
        return _hermes_doc_to_domain(row) if row else None

    async def add(self, document: HermesFetchedDocument) -> None:
        self._session.add(HermesFetchedDocumentModel(**_domain_data(document)))

    async def find_by_target_and_hash(
        self, target_id: UUID, content_sha256: str
    ) -> HermesFetchedDocument | None:
        result = await self._session.execute(
            select(HermesFetchedDocumentModel).where(
                HermesFetchedDocumentModel.target_id == target_id,
                HermesFetchedDocumentModel.content_sha256 == content_sha256,
            )
        )
        row = result.scalar_one_or_none()
        return _hermes_doc_to_domain(row) if row else None

    async def get_latest_for_target(self, target_id: UUID) -> HermesFetchedDocument | None:
        result = await self._session.execute(
            select(HermesFetchedDocumentModel)
            .where(HermesFetchedDocumentModel.target_id == target_id)
            .order_by(HermesFetchedDocumentModel.fetched_at.desc())
            .limit(1)
        )
        row = result.scalar_one_or_none()
        return _hermes_doc_to_domain(row) if row else None

    async def list_for_target(
        self, target_id: UUID, limit: int = 50, offset: int = 0
    ) -> list[HermesFetchedDocument]:
        result = await self._session.execute(
            select(HermesFetchedDocumentModel)
            .where(HermesFetchedDocumentModel.target_id == target_id)
            .order_by(HermesFetchedDocumentModel.fetched_at.desc())
            .limit(limit)
            .offset(offset)
        )
        return [_hermes_doc_to_domain(r) for r in result.scalars().all()]


class SqlHermesSourceChangeRepository(HermesSourceChangeRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, id: UUID) -> HermesSourceChange | None:
        row = await self._session.get(HermesSourceChangeModel, id)
        return _hermes_change_to_domain(row) if row else None

    async def add(self, change: HermesSourceChange) -> None:
        self._session.add(HermesSourceChangeModel(**_domain_data(change)))

    async def list_for_target(
        self, target_id: UUID, limit: int = 50, offset: int = 0
    ) -> list[HermesSourceChange]:
        result = await self._session.execute(
            select(HermesSourceChangeModel)
            .where(HermesSourceChangeModel.target_id == target_id)
            .order_by(HermesSourceChangeModel.detected_at.desc())
            .limit(limit)
            .offset(offset)
        )
        return [_hermes_change_to_domain(r) for r in result.scalars().all()]

    async def list_recent(self, limit: int = 100, offset: int = 0) -> list[HermesSourceChange]:
        result = await self._session.execute(
            select(HermesSourceChangeModel)
            .order_by(HermesSourceChangeModel.detected_at.desc())
            .limit(limit)
            .offset(offset)
        )
        return [_hermes_change_to_domain(r) for r in result.scalars().all()]
