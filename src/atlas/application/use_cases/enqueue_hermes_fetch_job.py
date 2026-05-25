from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from atlas.application.unit_of_work import UnitOfWork
from atlas.domain.entities import HermesFetchJob
from atlas.domain.enums import HermesFetchJobStatus, HermesTargetStatus


@dataclass
class EnqueueHermesFetchJobInput:
    target_id: UUID
    priority: int = 100
    scheduled_at: datetime | None = None


class EnqueueHermesFetchJob:
    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    async def execute(self, inp: EnqueueHermesFetchJobInput) -> HermesFetchJob:
        target = await self._uow.hermes_crawl_targets.get(inp.target_id)
        if target is None:
            raise ValueError(f"HermesCrawlTarget {inp.target_id} not found")
        if target.status != HermesTargetStatus.ACTIVE:
            raise ValueError(f"Target {inp.target_id} is not ACTIVE (status={target.status})")

        job = HermesFetchJob(
            target_id=inp.target_id,
            status=HermesFetchJobStatus.QUEUED,
            priority=inp.priority,
            scheduled_at=inp.scheduled_at,
        )
        # ``add_or_get_active_for_target`` is a single atomic
        # INSERT … ON CONFLICT DO NOTHING operation, eliminating the TOCTOU
        # race in the previous SELECT-then-INSERT pattern.  Two concurrent
        # enqueue calls for the same target produce at most one QUEUED row;
        # the losing caller receives the pre-existing active job.
        result, created = await self._uow.hermes_fetch_jobs.add_or_get_active_for_target(job)
        if created:
            await self._uow.commit()
        return result
