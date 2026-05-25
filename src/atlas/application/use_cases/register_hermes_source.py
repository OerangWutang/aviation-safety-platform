from __future__ import annotations

from dataclasses import dataclass

from atlas.application.unit_of_work import UnitOfWork
from atlas.domain.entities import HermesSource
from atlas.domain.enums import HermesSourceType
from atlas.domain.services.hermes_url_normalizer import validate_url


@dataclass
class RegisterHermesSourceInput:
    name: str
    source_type: HermesSourceType
    base_url: str | None = None
    reliability_tier: str | None = None


class RegisterHermesSource:
    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    async def execute(self, inp: RegisterHermesSourceInput) -> HermesSource:
        base_url = validate_url(inp.base_url) if inp.base_url else None
        source = HermesSource(
            name=inp.name.strip(),
            source_type=inp.source_type,
            base_url=base_url,
            reliability_tier=inp.reliability_tier,
        )
        # ``add_or_get_by_name`` is a single atomic INSERT … ON CONFLICT DO NOTHING
        # operation, eliminating the TOCTOU race between a SELECT (find_by_name)
        # and a subsequent INSERT that could cause an IntegrityError under concurrent
        # registration callers.
        result, created = await self._uow.hermes_sources.add_or_get_by_name(source)
        if created:
            await self._uow.commit()
        return result
