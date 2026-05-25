"""SQL repositories for the causality bounded context (Phase 4).

HFACS taxonomy reads are full-table scans because the taxonomy is
small (<30 rows) and stable.  Per-event attribution and SHELO
factor lists filter on the indexed ``event_id`` column.

Optimistic concurrency uses the same ``WHERE id = ? AND version = ?``
update pattern as Phases 9 and 10.  On rowcount==0 we read the
current row to disambiguate "vanished" from "stale version" and
raise :class:`HfacsAttributionConflictError` in either case — the
caller treats both as "retry from a fresh read".
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from atlas.domain.causality.entities import (
    EventHfacsAttribution,
    HfacsCategory,
    HfacsSubcategory,
    SheloFactor,
    SheloFactorInteraction,
    SheloInteractionKind,
)
from atlas.domain.causality.exceptions import (
    HfacsAttributionConflictError,
    SheloFactorConflictError,
    SheloFactorInteractionConflictError,
)
from atlas.domain.interfaces.repositories import (
    EventHfacsAttributionRepository,
    HfacsCategoryRepository,
    HfacsSubcategoryRepository,
    SheloFactorInteractionRepository,
    SheloFactorRepository,
)
from atlas.infrastructure.db.orm_models import (
    EventHfacsAttributionModel,
    HfacsCategoryModel,
    HfacsSubcategoryModel,
    SheloFactorInteractionModel,
    SheloFactorModel,
)
from atlas.infrastructure.db.repositories._helpers import (
    _domain_data,
    _to_domain,
    _to_domain_opt,
)


class SqlHfacsCategoryRepository(HfacsCategoryRepository):
    def __init__(self, session: AsyncSession):
        self._session = session

    async def list_all(self) -> list[HfacsCategory]:
        # Sort by tier then code so the public taxonomy endpoint
        # renders in a stable, expected order without a client-side
        # sort.
        result = await self._session.execute(
            select(HfacsCategoryModel).order_by(
                HfacsCategoryModel.tier_code,
                HfacsCategoryModel.code,
            )
        )
        return [_to_domain(row, HfacsCategory) for row in result.scalars()]

    async def get(self, category_id: UUID) -> HfacsCategory | None:
        obj = await self._session.get(HfacsCategoryModel, category_id)
        return _to_domain_opt(obj, HfacsCategory)

    async def get_by_code(self, code: str) -> HfacsCategory | None:
        result = await self._session.execute(
            select(HfacsCategoryModel).where(HfacsCategoryModel.code == code)
        )
        return _to_domain_opt(result.scalar_one_or_none(), HfacsCategory)


class SqlHfacsSubcategoryRepository(HfacsSubcategoryRepository):
    def __init__(self, session: AsyncSession):
        self._session = session

    async def list_for_category(self, category_id: UUID) -> list[HfacsSubcategory]:
        result = await self._session.execute(
            select(HfacsSubcategoryModel)
            .where(HfacsSubcategoryModel.category_id == category_id)
            .order_by(HfacsSubcategoryModel.code)
        )
        return [_to_domain(row, HfacsSubcategory) for row in result.scalars()]

    async def get(self, subcategory_id: UUID) -> HfacsSubcategory | None:
        obj = await self._session.get(HfacsSubcategoryModel, subcategory_id)
        return _to_domain_opt(obj, HfacsSubcategory)


class SqlEventHfacsAttributionRepository(EventHfacsAttributionRepository):
    def __init__(self, session: AsyncSession):
        self._session = session

    async def list_for_event(self, event_id: UUID) -> list[EventHfacsAttribution]:
        # Join to the category so we can order by tier_code/code at
        # the SQL layer rather than re-sorting in Python.
        result = await self._session.execute(
            select(EventHfacsAttributionModel)
            .join(
                HfacsCategoryModel,
                EventHfacsAttributionModel.category_id == HfacsCategoryModel.id,
            )
            .where(EventHfacsAttributionModel.event_id == event_id)
            .order_by(HfacsCategoryModel.tier_code, HfacsCategoryModel.code)
        )
        return [_to_domain(row, EventHfacsAttribution) for row in result.scalars()]

    async def get(self, attribution_id: UUID) -> EventHfacsAttribution | None:
        obj = await self._session.get(EventHfacsAttributionModel, attribution_id)
        return _to_domain_opt(obj, EventHfacsAttribution)

    async def find_natural(
        self,
        *,
        event_id: UUID,
        category_id: UUID,
        subcategory_id: UUID | None,
    ) -> EventHfacsAttribution | None:
        # The NULL-vs-equals dance mirrors the partial unique index
        # built in migration 042: a NULL subcategory_id is treated
        # as a "category-only" attribution and is the matched key.
        stmt = select(EventHfacsAttributionModel).where(
            EventHfacsAttributionModel.event_id == event_id,
            EventHfacsAttributionModel.category_id == category_id,
        )
        if subcategory_id is None:
            stmt = stmt.where(EventHfacsAttributionModel.subcategory_id.is_(None))
        else:
            stmt = stmt.where(EventHfacsAttributionModel.subcategory_id == subcategory_id)
        result = await self._session.execute(stmt)
        return _to_domain_opt(result.scalar_one_or_none(), EventHfacsAttribution)

    async def add(self, attribution: EventHfacsAttribution) -> None:
        # Pre-check the natural key so a duplicate surfaces as the
        # typed HfacsAttributionConflictError (-> 409) rather than a
        # raw IntegrityError from the partial unique index (-> 500).
        # This keeps the SQL repo's behaviour aligned with the fake,
        # which enforces the same invariant in-memory.  The DB index
        # remains the ultimate backstop against a race between the
        # check and the flush.
        existing = await self.find_natural(
            event_id=attribution.event_id,
            category_id=attribution.category_id,
            subcategory_id=attribution.subcategory_id,
        )
        if existing is not None:
            raise HfacsAttributionConflictError(
                f"An attribution already exists for event "
                f"{attribution.event_id}, category "
                f"{attribution.category_id}, subcategory "
                f"{attribution.subcategory_id}."
            )
        self._session.add(EventHfacsAttributionModel(**_domain_data(attribution)))
        await self._session.flush()

    async def update(
        self,
        attribution: EventHfacsAttribution,
        *,
        expected_version: int,
    ) -> None:
        data = _domain_data(attribution)
        data.pop("id", None)
        result = await self._session.execute(
            update(EventHfacsAttributionModel)
            .where(
                EventHfacsAttributionModel.id == attribution.id,
                EventHfacsAttributionModel.version == expected_version,
            )
            .values(**data)
        )
        if getattr(result, "rowcount", 0) == 0:
            raise HfacsAttributionConflictError(
                f"HFACS attribution {attribution.id} either vanished "
                f"or its version moved past v{expected_version}."
            )

    async def delete(self, attribution_id: UUID) -> None:
        obj = await self._session.get(EventHfacsAttributionModel, attribution_id)
        if obj is not None:
            await self._session.delete(obj)


class SqlSheloFactorRepository(SheloFactorRepository):
    def __init__(self, session: AsyncSession):
        self._session = session

    async def list_for_event(self, event_id: UUID) -> list[SheloFactor]:
        result = await self._session.execute(
            select(SheloFactorModel)
            .where(SheloFactorModel.event_id == event_id)
            .order_by(SheloFactorModel.factor_class, SheloFactorModel.created_at)
        )
        return [_to_domain(row, SheloFactor) for row in result.scalars()]

    async def get(self, factor_id: UUID) -> SheloFactor | None:
        obj = await self._session.get(SheloFactorModel, factor_id)
        return _to_domain_opt(obj, SheloFactor)

    async def add(self, factor: SheloFactor) -> None:
        data = _domain_data(factor)
        data["factor_class"] = (
            factor.factor_class.value
            if hasattr(factor.factor_class, "value")
            else factor.factor_class
        )
        self._session.add(SheloFactorModel(**data))
        await self._session.flush()

    async def update(self, factor: SheloFactor, *, expected_version: int) -> None:
        data = _domain_data(factor)
        data["factor_class"] = (
            factor.factor_class.value
            if hasattr(factor.factor_class, "value")
            else factor.factor_class
        )
        data.pop("id", None)
        result = await self._session.execute(
            update(SheloFactorModel)
            .where(
                SheloFactorModel.id == factor.id,
                SheloFactorModel.version == expected_version,
            )
            .values(**data)
        )
        if getattr(result, "rowcount", 0) == 0:
            raise SheloFactorConflictError(
                f"SHELO factor {factor.id} either vanished or its "
                f"version moved past v{expected_version}."
            )

    async def delete(self, factor_id: UUID) -> None:
        obj = await self._session.get(SheloFactorModel, factor_id)
        if obj is not None:
            await self._session.delete(obj)


class SqlSheloFactorInteractionRepository(SheloFactorInteractionRepository):
    def __init__(self, session: AsyncSession):
        self._session = session

    async def list_for_event(self, event_id: UUID) -> list[SheloFactorInteraction]:
        result = await self._session.execute(
            select(SheloFactorInteractionModel)
            .where(SheloFactorInteractionModel.event_id == event_id)
            .order_by(SheloFactorInteractionModel.created_at)
        )
        return [_to_domain(row, SheloFactorInteraction) for row in result.scalars()]

    async def find_natural(
        self,
        *,
        event_id: UUID,
        source_factor_id: UUID,
        target_factor_id: UUID,
        interaction_kind: SheloInteractionKind,
    ) -> SheloFactorInteraction | None:
        result = await self._session.execute(
            select(SheloFactorInteractionModel).where(
                SheloFactorInteractionModel.event_id == event_id,
                SheloFactorInteractionModel.source_factor_id == source_factor_id,
                SheloFactorInteractionModel.target_factor_id == target_factor_id,
                SheloFactorInteractionModel.interaction_kind == interaction_kind.value,
            )
        )
        return _to_domain_opt(result.scalar_one_or_none(), SheloFactorInteraction)

    async def add(self, interaction: SheloFactorInteraction) -> None:
        # Pre-check the natural key for parity with the fake and to
        # surface a typed SheloFactorInteractionConflictError (-> 409)
        # rather than a raw IntegrityError (-> 500).  The unique
        # index is the ultimate backstop against a check/flush race.
        existing = await self.find_natural(
            event_id=interaction.event_id,
            source_factor_id=interaction.source_factor_id,
            target_factor_id=interaction.target_factor_id,
            interaction_kind=interaction.interaction_kind,
        )
        if existing is not None:
            raise SheloFactorInteractionConflictError(
                f"Interaction already exists with "
                f"(source={interaction.source_factor_id}, "
                f"target={interaction.target_factor_id}, "
                f"kind={interaction.interaction_kind.value})"
            )
        data = _domain_data(interaction)
        data["interaction_kind"] = (
            interaction.interaction_kind.value
            if hasattr(interaction.interaction_kind, "value")
            else interaction.interaction_kind
        )
        self._session.add(SheloFactorInteractionModel(**data))
        await self._session.flush()

    async def delete(self, interaction_id: UUID) -> None:
        obj = await self._session.get(SheloFactorInteractionModel, interaction_id)
        if obj is not None:
            await self._session.delete(obj)
