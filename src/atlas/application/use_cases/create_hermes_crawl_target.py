from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from atlas.application.unit_of_work import UnitOfWork
from atlas.domain.entities import HermesCrawlTarget
from atlas.domain.services.hermes_url_normalizer import validate_url


@dataclass
class CreateHermesCrawlTargetInput:
    source_id: UUID
    url: str
    label: str | None = None


class CreateHermesCrawlTarget:
    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    async def execute(self, inp: CreateHermesCrawlTargetInput) -> HermesCrawlTarget:
        source = await self._uow.hermes_sources.get(inp.source_id)
        if source is None:
            raise ValueError(f"HermesSource {inp.source_id} not found")
        normalized = validate_url(inp.url)
        target = HermesCrawlTarget(
            source_id=inp.source_id,
            url=inp.url,
            normalized_url=normalized,
            label=inp.label,
        )
        # ``add_or_get_by_normalized_url`` is a single atomic INSERT … ON CONFLICT
        # DO NOTHING operation, eliminating the TOCTOU race in the previous
        # SELECT-then-INSERT pattern.
        result, created = await self._uow.hermes_crawl_targets.add_or_get_by_normalized_url(target)
        if created:
            await self._uow.commit()
        return result
