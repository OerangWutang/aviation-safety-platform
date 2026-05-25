"""Fake HFACS/SHELO causality repositories."""

from __future__ import annotations

from uuid import UUID

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
from tests.domain.fakes._store import (
    _CausalityStore,
)


class FakeHfacsCategoryRepository(HfacsCategoryRepository):
    def __init__(self, s: _CausalityStore) -> None:
        self._s = s

    async def list_all(self) -> list[HfacsCategory]:
        return sorted(
            (c.model_copy(deep=True) for c in self._s.hfacs_categories.values()),
            key=lambda c: (c.tier_code, c.code),
        )

    async def get(self, category_id: UUID) -> HfacsCategory | None:
        c = self._s.hfacs_categories.get(category_id)
        return c.model_copy(deep=True) if c else None

    async def get_by_code(self, code: str) -> HfacsCategory | None:
        for c in self._s.hfacs_categories.values():
            if c.code == code:
                return c.model_copy(deep=True)
        return None


class FakeHfacsSubcategoryRepository(HfacsSubcategoryRepository):
    def __init__(self, s: _CausalityStore) -> None:
        self._s = s

    async def list_for_category(self, category_id: UUID) -> list[HfacsSubcategory]:
        return sorted(
            (
                s.model_copy(deep=True)
                for s in self._s.hfacs_subcategories.values()
                if s.category_id == category_id
            ),
            key=lambda s: s.code,
        )

    async def get(self, subcategory_id: UUID) -> HfacsSubcategory | None:
        s = self._s.hfacs_subcategories.get(subcategory_id)
        return s.model_copy(deep=True) if s else None


class FakeEventHfacsAttributionRepository(EventHfacsAttributionRepository):
    def __init__(self, s: _CausalityStore, causality: _CausalityStore) -> None:
        self._s = s
        self._causality = causality

    async def list_for_event(self, event_id: UUID) -> list[EventHfacsAttribution]:
        # Sort by joined-category (tier_code, code) to match SQL.
        rows = [a for a in self._s.event_hfacs_attributions.values() if a.event_id == event_id]

        def _sort_key(a: EventHfacsAttribution):
            cat = self._causality.hfacs_categories.get(a.category_id)
            return (cat.tier_code if cat else "", cat.code if cat else "")

        return sorted((a.model_copy(deep=True) for a in rows), key=_sort_key)

    async def get(self, attribution_id: UUID) -> EventHfacsAttribution | None:
        a = self._s.event_hfacs_attributions.get(attribution_id)
        return a.model_copy(deep=True) if a else None

    async def find_natural(
        self,
        *,
        event_id: UUID,
        category_id: UUID,
        subcategory_id: UUID | None,
    ) -> EventHfacsAttribution | None:
        for a in self._s.event_hfacs_attributions.values():
            if (
                a.event_id == event_id
                and a.category_id == category_id
                and a.subcategory_id == subcategory_id
            ):
                return a.model_copy(deep=True)
        return None

    async def add(self, attribution: EventHfacsAttribution) -> None:
        # Enforce the partial-unique-index natural-key invariant so
        # SQL behaviour and fake behaviour match on duplicate inserts.
        existing = await self.find_natural(
            event_id=attribution.event_id,
            category_id=attribution.category_id,
            subcategory_id=attribution.subcategory_id,
        )
        if existing is not None:
            raise HfacsAttributionConflictError(
                f"Attribution already exists for event {attribution.event_id} "
                f"category {attribution.category_id} "
                f"subcategory {attribution.subcategory_id}"
            )
        self._s.event_hfacs_attributions[attribution.id] = attribution.model_copy(deep=True)

    async def update(
        self,
        attribution: EventHfacsAttribution,
        *,
        expected_version: int,
    ) -> None:
        current = self._s.event_hfacs_attributions.get(attribution.id)
        if current is None or current.version != expected_version:
            raise HfacsAttributionConflictError(
                f"HFACS attribution {attribution.id} either vanished "
                f"or its version moved past v{expected_version}."
            )
        self._s.event_hfacs_attributions[attribution.id] = attribution.model_copy(deep=True)

    async def delete(self, attribution_id: UUID) -> None:
        self._s.event_hfacs_attributions.pop(attribution_id, None)


class FakeSheloFactorRepository(SheloFactorRepository):
    def __init__(self, s: _CausalityStore) -> None:
        self._s = s

    async def list_for_event(self, event_id: UUID) -> list[SheloFactor]:
        return sorted(
            (
                f.model_copy(deep=True)
                for f in self._s.shelo_factors.values()
                if f.event_id == event_id
            ),
            key=lambda f: (f.factor_class, f.created_at),
        )

    async def get(self, factor_id: UUID) -> SheloFactor | None:
        f = self._s.shelo_factors.get(factor_id)
        return f.model_copy(deep=True) if f else None

    async def add(self, factor: SheloFactor) -> None:
        self._s.shelo_factors[factor.id] = factor.model_copy(deep=True)

    async def update(self, factor: SheloFactor, *, expected_version: int) -> None:
        current = self._s.shelo_factors.get(factor.id)
        if current is None or current.version != expected_version:
            raise SheloFactorConflictError(
                f"SHELO factor {factor.id} either vanished or its "
                f"version moved past v{expected_version}."
            )
        self._s.shelo_factors[factor.id] = factor.model_copy(deep=True)

    async def delete(self, factor_id: UUID) -> None:
        self._s.shelo_factors.pop(factor_id, None)
        # Cascade: also drop interactions touching this factor.
        self._s.shelo_factor_interactions = {
            iid: i
            for iid, i in self._s.shelo_factor_interactions.items()
            if i.source_factor_id != factor_id and i.target_factor_id != factor_id
        }


class FakeSheloFactorInteractionRepository(SheloFactorInteractionRepository):
    def __init__(self, s: _CausalityStore) -> None:
        self._s = s

    async def list_for_event(self, event_id: UUID) -> list[SheloFactorInteraction]:
        return sorted(
            (
                i.model_copy(deep=True)
                for i in self._s.shelo_factor_interactions.values()
                if i.event_id == event_id
            ),
            key=lambda i: i.created_at,
        )

    async def find_natural(
        self,
        *,
        event_id: UUID,
        source_factor_id: UUID,
        target_factor_id: UUID,
        interaction_kind: SheloInteractionKind,
    ) -> SheloFactorInteraction | None:
        for i in self._s.shelo_factor_interactions.values():
            if (
                i.event_id == event_id
                and i.source_factor_id == source_factor_id
                and i.target_factor_id == target_factor_id
                and i.interaction_kind == interaction_kind
            ):
                return i.model_copy(deep=True)
        return None

    async def add(self, interaction: SheloFactorInteraction) -> None:
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
                f"kind={interaction.interaction_kind})"
            )
        self._s.shelo_factor_interactions[interaction.id] = interaction.model_copy(deep=True)

    async def delete(self, interaction_id: UUID) -> None:
        self._s.shelo_factor_interactions.pop(interaction_id, None)


# ── Phase 7 fakes ───────────────────────────────────────────────────────────
