"""Fake Hermes source, crawl-target, fetch-job and document repositories."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

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
from atlas.domain.interfaces.repositories import (
    HermesCrawlTargetRepository,
    HermesFetchedDocumentRepository,
    HermesFetchJobRepository,
    HermesRecoveryOutcome,
    HermesSourceChangeRepository,
    HermesSourceRepository,
)
from tests.domain.fakes._store import (
    _HermesStore,
)


class FakeHermesSourceRepository(HermesSourceRepository):
    def __init__(self, s: _HermesStore) -> None:
        self._s = s

    async def get(self, id: UUID) -> HermesSource | None:
        return self._s.sources.get(id)

    async def add(self, source: HermesSource) -> None:
        self._s.sources[source.id] = source

    async def find_by_name(self, name: str) -> HermesSource | None:
        nl = name.lower().strip()
        return next((s for s in self._s.sources.values() if s.name.lower().strip() == nl), None)

    async def add_or_get_by_name(self, source: HermesSource) -> tuple[HermesSource, bool]:
        existing = await self.find_by_name(source.name)
        if existing is not None:
            return existing, False
        self._s.sources[source.id] = source
        return source, True

    async def list_active(self, limit: int = 100, offset: int = 0) -> list[HermesSource]:
        active = [s for s in self._s.sources.values() if s.is_active]
        return active[offset : offset + limit]


class FakeHermesCrawlTargetRepository(HermesCrawlTargetRepository):
    def __init__(self, s: _HermesStore) -> None:
        self._s = s

    async def get(self, id: UUID) -> HermesCrawlTarget | None:
        return self._s.targets.get(id)

    async def add(self, target: HermesCrawlTarget) -> None:
        self._s.targets[target.id] = target

    async def save(self, target: HermesCrawlTarget) -> None:
        self._s.targets[target.id] = target

    async def find_by_normalized_url(self, normalized_url: str) -> HermesCrawlTarget | None:
        return next(
            (t for t in self._s.targets.values() if t.normalized_url == normalized_url), None
        )

    async def add_or_get_by_normalized_url(
        self, target: HermesCrawlTarget
    ) -> tuple[HermesCrawlTarget, bool]:
        existing = await self.find_by_normalized_url(target.normalized_url)
        if existing is not None:
            return existing, False
        self._s.targets[target.id] = target
        return target, True

    async def list(
        self,
        status: HermesTargetStatus | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[HermesCrawlTarget]:
        items = list(self._s.targets.values())
        if status is not None:
            items = [t for t in items if t.status == status]
        return items[offset : offset + limit]


class FakeHermesFetchJobRepository(HermesFetchJobRepository):
    def __init__(self, s: _HermesStore) -> None:
        self._s = s

    async def get(self, id: UUID) -> HermesFetchJob | None:
        return next((j for j in self._s.jobs if j.id == id), None)

    async def add(self, job: HermesFetchJob) -> None:
        self._s.jobs.append(job)

    async def save(self, job: HermesFetchJob) -> None:
        for i, j in enumerate(self._s.jobs):
            if j.id == job.id:
                self._s.jobs[i] = job
                return
        self._s.jobs.append(job)

    async def find_active_for_target(self, target_id: UUID) -> HermesFetchJob | None:
        return next(
            (
                j
                for j in self._s.jobs
                if j.target_id == target_id
                and j.status in (HermesFetchJobStatus.QUEUED, HermesFetchJobStatus.RUNNING)
            ),
            None,
        )

    async def add_or_get_active_for_target(
        self, job: HermesFetchJob
    ) -> tuple[HermesFetchJob, bool]:
        existing = await self.find_active_for_target(job.target_id)
        if existing is not None:
            return existing, False
        self._s.jobs.append(job)
        return job, True

    async def claim_running(
        self,
        job_id: UUID,
        *,
        worker_id: str,
        lease_expires_at: datetime,
    ) -> HermesFetchJob | None:
        from datetime import datetime as _dt

        now = _dt.now(UTC)
        for i, j in enumerate(self._s.jobs):
            if (
                j.id == job_id
                and j.status == HermesFetchJobStatus.QUEUED
                and (j.scheduled_at is None or j.scheduled_at <= now)
            ):
                claimed = j.model_copy(
                    update={
                        "status": HermesFetchJobStatus.RUNNING,
                        "attempt_count": j.attempt_count + 1,
                        "started_at": now,
                        "finished_at": None,
                        "error_message": None,
                        "locked_by": worker_id,
                        "locked_at": now,
                        "lease_expires_at": lease_expires_at,
                    }
                )
                self._s.jobs[i] = claimed
                return claimed
        return None

    async def claim_next_running(
        self,
        *,
        worker_id: str,
        lease_expires_at: datetime,
    ) -> HermesFetchJob | None:
        job = await self.get_next_queued()
        if job is None:
            return None
        return await self.claim_running(
            job.id, worker_id=worker_id, lease_expires_at=lease_expires_at
        )

    async def lock_claim_for_finalization(
        self,
        job_id: UUID,
        *,
        worker_id: str,
        attempt_count: int,
        now: datetime,
    ) -> HermesFetchJob | None:
        for job in self._s.jobs:
            if (
                job.id == job_id
                and job.status == HermesFetchJobStatus.RUNNING
                and job.locked_by == worker_id
                and job.attempt_count == attempt_count
                and job.lease_expires_at is not None
                and job.lease_expires_at >= now
            ):
                return job
        return None

    async def recover_stale_running(
        self, *, now: datetime, limit: int = 100
    ) -> list[HermesRecoveryOutcome]:
        outcomes: list[HermesRecoveryOutcome] = []
        for i, job in enumerate(list(self._s.jobs)):
            if len(outcomes) >= limit:
                break
            if (
                job.status == HermesFetchJobStatus.RUNNING
                and job.lease_expires_at is not None
                and job.lease_expires_at < now
            ):
                if job.attempt_count >= job.max_attempts:
                    status = HermesFetchJobStatus.FAILED
                    scheduled_at = None
                else:
                    status = HermesFetchJobStatus.QUEUED
                    scheduled_at = now
                self._s.jobs[i] = job.model_copy(
                    update={
                        "status": status,
                        "scheduled_at": scheduled_at,
                        "finished_at": now,
                        "error_message": "Recovered stale RUNNING Hermes job after lease expiry",
                        "locked_by": None,
                        "locked_at": None,
                        "lease_expires_at": None,
                    }
                )
                outcomes.append(
                    HermesRecoveryOutcome(
                        job_id=job.id,
                        target_id=job.target_id,
                        final_status=status,
                        attempt_count=job.attempt_count,
                    )
                )
        return outcomes

    async def get_next_queued(self) -> HermesFetchJob | None:
        from datetime import datetime as _dt

        now = _dt.now(UTC)
        queued = [
            j
            for j in self._s.jobs
            if j.status == HermesFetchJobStatus.QUEUED
            and (j.scheduled_at is None or j.scheduled_at <= now)
        ]
        if not queued:
            return None
        return min(queued, key=lambda j: (j.priority, j.created_at, j.id))

    async def list(
        self,
        status: HermesFetchJobStatus | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[HermesFetchJob]:
        items = list(self._s.jobs)
        if status is not None:
            items = [j for j in items if j.status == status]
        return items[offset : offset + limit]


class FakeHermesFetchedDocumentRepository(HermesFetchedDocumentRepository):
    def __init__(self, s: _HermesStore) -> None:
        self._s = s

    async def get(self, id: UUID) -> HermesFetchedDocument | None:
        return next((d for d in self._s.documents if d.id == id), None)

    async def add(self, document: HermesFetchedDocument) -> None:
        self._s.documents.append(document)

    async def find_by_target_and_hash(
        self, target_id: UUID, content_sha256: str
    ) -> HermesFetchedDocument | None:
        return next(
            (
                d
                for d in self._s.documents
                if d.target_id == target_id and d.content_sha256 == content_sha256
            ),
            None,
        )

    async def get_latest_for_target(self, target_id: UUID) -> HermesFetchedDocument | None:
        docs = [d for d in self._s.documents if d.target_id == target_id]
        if not docs:
            return None
        return max(docs, key=lambda d: d.fetched_at)

    async def list_for_target(
        self, target_id: UUID, limit: int = 50, offset: int = 0
    ) -> list[HermesFetchedDocument]:
        docs = [d for d in self._s.documents if d.target_id == target_id]
        docs.sort(key=lambda d: d.fetched_at, reverse=True)
        return docs[offset : offset + limit]


class FakeHermesSourceChangeRepository(HermesSourceChangeRepository):
    def __init__(self, s: _HermesStore) -> None:
        self._s = s

    async def get(self, id: UUID) -> HermesSourceChange | None:
        return next((c for c in self._s.changes if c.id == id), None)

    async def add(self, change: HermesSourceChange) -> None:
        self._s.changes.append(change)

    async def list_for_target(
        self, target_id: UUID, limit: int = 50, offset: int = 0
    ) -> list[HermesSourceChange]:
        items = [c for c in self._s.changes if c.target_id == target_id]
        items.sort(key=lambda c: c.detected_at, reverse=True)
        return items[offset : offset + limit]

    async def list_recent(self, limit: int = 100, offset: int = 0) -> list[HermesSourceChange]:
        items = list(self._s.changes)
        items.sort(key=lambda c: c.detected_at, reverse=True)
        return items[offset : offset + limit]
