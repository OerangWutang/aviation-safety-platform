"""Causality use cases (Phase 4).

Public reads inherit visibility from the parent ``PublicEventPage``:

- PUBLISHED → return.
- RETRACTED → raise (410 with retraction note via the existing
  Phase 1 ``PublicEventPageRetractedError``).
- Anything else → raise (404 via
  ``PublicEventPageNotPublishedError``).

We reuse the Phase 1 visibility exceptions because the contract is
identical: the parent page's status is authoritative, and any UI
already wired to handle "this page is not yet public" works without
modification for HFACS/SHELO too.

Editorial writes don't inherit visibility — they write to the
underlying tables regardless of the parent page's status.  An
analyst can attach HFACS attributions to a DRAFT event so the
analysis is ready by the time the page reaches PUBLISHED.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from atlas.application.services.metering import MeteringService
from atlas.application.unit_of_work import UnitOfWork
from atlas.domain.causality.entities import (
    EventHfacsAttribution,
    HfacsCategory,
    HfacsSubcategory,
    SheloClass,
    SheloFactor,
    SheloFactorInteraction,
    SheloInteractionKind,
)
from atlas.domain.causality.exceptions import (
    HfacsAttributionNotFoundError,
    HfacsCategoryNotFoundError,
    HfacsSubcategoryNotFoundError,
    SheloFactorInteractionConflictError,
    SheloFactorInteractionSameNodeError,
    SheloFactorNotFoundError,
)
from atlas.domain.metering.entities import MetricKind
from atlas.domain.publication.entities import PublicationStatus
from atlas.domain.publication.exceptions import (
    PublicEventPageNotPublishedError,
    PublicEventPageRetractedError,
)
from atlas.domain.utils import utc_now

# ── Visibility helper ───────────────────────────────────────────────────────


async def _require_published_event_for_slug(uow: UnitOfWork, slug: str) -> UUID:
    """Resolve a slug to a public event id, applying Phase 1 visibility.

    Returns the event_id.  Raises ``PublicEventPageNotPublishedError``
    on DRAFT/IN_REVIEW/APPROVED/ARCHIVED; raises
    ``PublicEventPageRetractedError`` on RETRACTED.

    Reusing the Phase 1 exception types lets the existing handlers
    in ``app.py`` surface the right HTTP codes (404 / 410) without
    further wiring.
    """
    page = await uow.public_event_pages.get_by_slug(slug)
    if page is None:
        raise PublicEventPageNotPublishedError(slug)
    if page.status == PublicationStatus.RETRACTED:
        raise PublicEventPageRetractedError(slug, page.retraction_note)
    if page.status != PublicationStatus.PUBLISHED:
        raise PublicEventPageNotPublishedError(slug)
    return page.event_id


# ── HFACS taxonomy read (no visibility gating; taxonomy is reference data) ──


@dataclass(frozen=True)
class HfacsTaxonomyView:
    """Composite view: every category + its subcategories.

    Phase 4 ships an empty subcategories table by default, so the
    subcategory lists are usually empty.  Operators populate them as
    needed for fine-grained attribution.
    """

    categories: list[tuple[HfacsCategory, list[HfacsSubcategory]]]


class ListHfacsTaxonomy:
    def __init__(self, uow: UnitOfWork):
        self._uow = uow

    async def execute(self) -> HfacsTaxonomyView:
        categories = await self._uow.hfacs_categories.list_all()
        grouped: list[tuple[HfacsCategory, list[HfacsSubcategory]]] = []
        for cat in categories:
            subs = await self._uow.hfacs_subcategories.list_for_category(cat.id)
            grouped.append((cat, subs))
        await self._uow.rollback()
        return HfacsTaxonomyView(categories=grouped)


# ── HFACS attributions: public reads ────────────────────────────────────────


@dataclass(frozen=True)
class EventHfacsView:
    """Composed read for the public event detail page.

    Attributions arrive sorted by (tier, code) thanks to the repo;
    we pass them straight through with their category and (optional)
    subcategory hydrated for rendering.
    """

    event_id: UUID
    attributions: list[tuple[EventHfacsAttribution, HfacsCategory, HfacsSubcategory | None]]


class GetEventHfacs:
    def __init__(self, uow: UnitOfWork):
        self._uow = uow

    async def execute(self, *, slug: str) -> EventHfacsView:
        event_id = await _require_published_event_for_slug(self._uow, slug)
        attributions = await self._uow.event_hfacs_attributions.list_for_event(event_id)
        # Hydrate category + subcategory once each so the router
        # doesn't N+1 fetch.
        cat_cache: dict[UUID, HfacsCategory] = {}
        sub_cache: dict[UUID, HfacsSubcategory] = {}
        hydrated: list[tuple[EventHfacsAttribution, HfacsCategory, HfacsSubcategory | None]] = []
        for a in attributions:
            cat = cat_cache.get(a.category_id)
            if cat is None:
                cat = await self._uow.hfacs_categories.get(a.category_id)
                if cat is None:
                    # A foreign-key violation should be impossible
                    # given the schema FK, but if a category was
                    # deleted manually we'd rather skip than crash.
                    continue
                cat_cache[a.category_id] = cat
            sub: HfacsSubcategory | None = None
            if a.subcategory_id is not None:
                sub = sub_cache.get(a.subcategory_id)
                if sub is None:
                    sub = await self._uow.hfacs_subcategories.get(a.subcategory_id)
                    if sub is not None:
                        sub_cache[a.subcategory_id] = sub
            hydrated.append((a, cat, sub))
        await self._uow.rollback()
        return EventHfacsView(event_id=event_id, attributions=hydrated)


# ── HFACS attributions: editorial writes ────────────────────────────────────


@dataclass(frozen=True)
class AttachHfacsAttributionInput:
    event_id: UUID
    category_id: UUID
    subcategory_id: UUID | None
    confidence: float
    note: str | None
    editor_user_id: UUID


class AttachEventHfacsAttribution:
    """Add a new HFACS attribution to an event.

    Verifies the category exists and (if given) the subcategory
    belongs to the same category — operators routinely make this
    mistake when scripting attributions, and a typed error is
    friendlier than a generic IntegrityError from a FK violation.
    """

    def __init__(self, uow: UnitOfWork):
        self._uow = uow

    async def execute(self, input: AttachHfacsAttributionInput) -> EventHfacsAttribution:
        category = await self._uow.hfacs_categories.get(input.category_id)
        if category is None:
            raise HfacsCategoryNotFoundError(f"HFACS category {input.category_id} not found")
        if input.subcategory_id is not None:
            subcategory = await self._uow.hfacs_subcategories.get(input.subcategory_id)
            if subcategory is None:
                raise HfacsSubcategoryNotFoundError(
                    f"HFACS subcategory {input.subcategory_id} not found"
                )
            if subcategory.category_id != input.category_id:
                # Cross-category subcategory: editorial bug.
                raise HfacsSubcategoryNotFoundError(
                    f"HFACS subcategory {input.subcategory_id} does "
                    f"not belong to category {input.category_id}"
                )
        attribution = EventHfacsAttribution(
            event_id=input.event_id,
            category_id=input.category_id,
            subcategory_id=input.subcategory_id,
            confidence=input.confidence,
            note=input.note,
            editor_user_id=input.editor_user_id,
        )
        # The repo's add() enforces the natural-key uniqueness; on
        # duplicate it raises HfacsAttributionConflictError.
        await self._uow.event_hfacs_attributions.add(attribution)
        # Meter: one event per HFACS attribution created.  Not
        # tenant-scoped (editorial action on the public corpus), but
        # we record the editor as the user.
        await MeteringService(self._uow).record(
            metric_kind=MetricKind.HFACS_ATTRIBUTION_CREATED,
            tenant_id=None,
            user_id=input.editor_user_id,
            resource_id=attribution.id,
        )
        await self._uow.commit()
        return attribution


@dataclass(frozen=True)
class UpdateHfacsAttributionInput:
    attribution_id: UUID
    expected_version: int
    confidence: float
    note: str | None
    editor_user_id: UUID


class UpdateEventHfacsAttribution:
    def __init__(self, uow: UnitOfWork):
        self._uow = uow

    async def execute(self, input: UpdateHfacsAttributionInput) -> EventHfacsAttribution:
        existing = await self._uow.event_hfacs_attributions.get(input.attribution_id)
        if existing is None:
            raise HfacsAttributionNotFoundError(
                f"HFACS attribution {input.attribution_id} not found"
            )
        updated = existing.model_copy(
            update={
                "confidence": input.confidence,
                "note": input.note,
                "editor_user_id": input.editor_user_id,
                "version": existing.version + 1,
                "updated_at": utc_now(),
            }
        )
        await self._uow.event_hfacs_attributions.update(
            updated, expected_version=input.expected_version
        )
        await self._uow.commit()
        return updated


class DeleteEventHfacsAttribution:
    """Idempotent delete: deleting a non-existent attribution is a
    no-op rather than 404, because the editorial UI may have stale
    state and retrying a delete shouldn't error."""

    def __init__(self, uow: UnitOfWork):
        self._uow = uow

    async def execute(self, attribution_id: UUID) -> None:
        await self._uow.event_hfacs_attributions.delete(attribution_id)
        await self._uow.commit()


# ── SHELO: public reads ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class EventSheloView:
    """Composite per-event SHELO read.

    Returns the factor nodes and the typed edges between them so a
    UI can render the small per-event graph without follow-up
    fetches.
    """

    event_id: UUID
    factors: list[SheloFactor]
    interactions: list[SheloFactorInteraction]


class GetEventShelo:
    def __init__(self, uow: UnitOfWork):
        self._uow = uow

    async def execute(self, *, slug: str) -> EventSheloView:
        event_id = await _require_published_event_for_slug(self._uow, slug)
        factors = await self._uow.shelo_factors.list_for_event(event_id)
        interactions = await self._uow.shelo_factor_interactions.list_for_event(event_id)
        await self._uow.rollback()
        return EventSheloView(
            event_id=event_id,
            factors=factors,
            interactions=interactions,
        )


# ── SHELO factors: editorial writes ─────────────────────────────────────────


@dataclass(frozen=True)
class AttachSheloFactorInput:
    event_id: UUID
    factor_class: SheloClass
    label: str
    description: str | None
    editor_user_id: UUID


class AttachSheloFactor:
    def __init__(self, uow: UnitOfWork):
        self._uow = uow

    async def execute(self, input: AttachSheloFactorInput) -> SheloFactor:
        factor = SheloFactor(
            event_id=input.event_id,
            factor_class=input.factor_class,
            label=input.label,
            description=input.description,
            editor_user_id=input.editor_user_id,
        )
        await self._uow.shelo_factors.add(factor)
        await self._uow.commit()
        return factor


@dataclass(frozen=True)
class UpdateSheloFactorInput:
    factor_id: UUID
    expected_version: int
    factor_class: SheloClass
    label: str
    description: str | None
    editor_user_id: UUID


class UpdateSheloFactor:
    def __init__(self, uow: UnitOfWork):
        self._uow = uow

    async def execute(self, input: UpdateSheloFactorInput) -> SheloFactor:
        existing = await self._uow.shelo_factors.get(input.factor_id)
        if existing is None:
            raise SheloFactorNotFoundError(f"SHELO factor {input.factor_id} not found")
        updated = existing.model_copy(
            update={
                "factor_class": input.factor_class,
                "label": input.label,
                "description": input.description,
                "editor_user_id": input.editor_user_id,
                "version": existing.version + 1,
                "updated_at": utc_now(),
            }
        )
        await self._uow.shelo_factors.update(updated, expected_version=input.expected_version)
        await self._uow.commit()
        return updated


class DeleteSheloFactor:
    """Delete a factor and cascade-drop any interactions touching it.

    Schema-level FK ``ondelete=CASCADE`` handles the cascade in SQL;
    the fake repo replicates it.  Idempotent on missing rows.
    """

    def __init__(self, uow: UnitOfWork):
        self._uow = uow

    async def execute(self, factor_id: UUID) -> None:
        await self._uow.shelo_factors.delete(factor_id)
        await self._uow.commit()


# ── SHELO interactions: editorial writes ────────────────────────────────────


@dataclass(frozen=True)
class AttachSheloInteractionInput:
    event_id: UUID
    source_factor_id: UUID
    target_factor_id: UUID
    interaction_kind: SheloInteractionKind
    note: str | None
    editor_user_id: UUID


class AttachSheloInteraction:
    """Create a typed edge between two factors on the same event.

    Validates:

    - Both factors exist and belong to ``event_id``.  A mismatch is
      surfaced as :class:`SheloFactorNotFoundError` with a hint
      message — the analyst typically gets here via copy-paste of
      a UUID from the wrong event.
    - Source and target differ.  The entity-level validator catches
      this too; we re-raise as the typed error so the router maps
      to 422.
    - Natural key uniqueness (event, source, target, kind).
    """

    def __init__(self, uow: UnitOfWork):
        self._uow = uow

    async def execute(self, input: AttachSheloInteractionInput) -> SheloFactorInteraction:
        if input.source_factor_id == input.target_factor_id:
            raise SheloFactorInteractionSameNodeError(
                "source_factor_id must differ from target_factor_id"
            )

        source = await self._uow.shelo_factors.get(input.source_factor_id)
        if source is None or source.event_id != input.event_id:
            raise SheloFactorNotFoundError(
                f"SHELO factor {input.source_factor_id} not found on event {input.event_id}"
            )
        target = await self._uow.shelo_factors.get(input.target_factor_id)
        if target is None or target.event_id != input.event_id:
            raise SheloFactorNotFoundError(
                f"SHELO factor {input.target_factor_id} not found on event {input.event_id}"
            )

        # Pre-check natural-key to surface 409 with a clear message;
        # the repo's add() would also catch this but via a generic
        # ValueError on the fake or IntegrityError on SQL.
        existing = await self._uow.shelo_factor_interactions.find_natural(
            event_id=input.event_id,
            source_factor_id=input.source_factor_id,
            target_factor_id=input.target_factor_id,
            interaction_kind=input.interaction_kind,
        )
        if existing is not None:
            raise SheloFactorInteractionConflictError(
                f"Interaction already exists for "
                f"({input.source_factor_id} -> {input.target_factor_id}, "
                f"{input.interaction_kind.value})"
            )

        interaction = SheloFactorInteraction(
            event_id=input.event_id,
            source_factor_id=input.source_factor_id,
            target_factor_id=input.target_factor_id,
            interaction_kind=input.interaction_kind,
            note=input.note,
            editor_user_id=input.editor_user_id,
        )
        await self._uow.shelo_factor_interactions.add(interaction)
        await self._uow.commit()
        return interaction


class DeleteSheloInteraction:
    def __init__(self, uow: UnitOfWork):
        self._uow = uow

    async def execute(self, interaction_id: UUID) -> None:
        await self._uow.shelo_factor_interactions.delete(interaction_id)
        await self._uow.commit()


__all__ = [
    "AttachEventHfacsAttribution",
    "AttachHfacsAttributionInput",
    "AttachSheloFactor",
    "AttachSheloFactorInput",
    "AttachSheloInteraction",
    "AttachSheloInteractionInput",
    "DeleteEventHfacsAttribution",
    "DeleteSheloFactor",
    "DeleteSheloInteraction",
    "EventHfacsView",
    "EventSheloView",
    "GetEventHfacs",
    "GetEventShelo",
    "HfacsTaxonomyView",
    "ListHfacsTaxonomy",
    "UpdateEventHfacsAttribution",
    "UpdateHfacsAttributionInput",
    "UpdateSheloFactor",
    "UpdateSheloFactorInput",
]
