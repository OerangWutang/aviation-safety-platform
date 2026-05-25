"""Causality router (Phase 4).

Three surfaces:

- Public reads (reader-gated) for the taxonomy and per-event
  composites.  Slug-keyed.  Visibility inherited from the parent
  ``PublicEventPage``.

- Editorial writes (reviewer+) for HFACS attributions.

- Editorial writes (reviewer+) for SHELO factors and interactions.

NOTE: The public_router endpoints use ``get_uow`` (system engine) rather than
``get_public_uow``.  Causality tables (``hfacs_categories``,
``event_hfacs_attributions``, ``shelo_factors``) are populated via editorial
writes that land in the system DB.  Whether these tables are replicated into
the public DB in the split-topology deployment is an open data-pipeline question.
Until that sync path is defined and tested, routing public causality reads
through the public engine would silently return empty results in production.
Revisit when the causality data-sync strategy is confirmed.

There's no distinct admin role for retraction here — Phase 4
doesn't introduce its own state machine.  Deleting an attribution
or factor is a regular editorial action with the same reviewer+
gate.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response

from atlas.application.dto import CurrentUser
from atlas.application.unit_of_work import UnitOfWork
from atlas.application.use_cases.causality import (
    AttachEventHfacsAttribution,
    AttachHfacsAttributionInput,
    AttachSheloFactor,
    AttachSheloFactorInput,
    AttachSheloInteraction,
    AttachSheloInteractionInput,
    DeleteEventHfacsAttribution,
    DeleteSheloFactor,
    DeleteSheloInteraction,
    GetEventHfacs,
    GetEventShelo,
    ListHfacsTaxonomy,
    UpdateEventHfacsAttribution,
    UpdateHfacsAttributionInput,
    UpdateSheloFactor,
    UpdateSheloFactorInput,
)
from atlas.domain.causality.entities import (
    SheloClass,
    SheloInteractionKind,
)
from atlas.domain.enums import Role
from atlas.presentation.api.dependencies import get_uow, require_role
from atlas.presentation.api.responses import offloaded_json_response
from atlas.presentation.api.schemas.causality import (
    AttachHfacsAttributionRequest,
    AttachSheloFactorRequest,
    AttachSheloInteractionRequest,
    EventHfacsResponse,
    EventSheloResponse,
    HfacsAttributionItem,
    HfacsCategoryItem,
    HfacsSubcategoryItem,
    HfacsTaxonomyResponse,
    SheloFactorInteractionItem,
    SheloFactorItem,
    UpdateHfacsAttributionRequest,
    UpdateSheloFactorRequest,
)

# Two routers under separate prefixes so OpenAPI groups them
# sensibly and so a future operator can disable the editorial
# surface without affecting the public one.
public_router = APIRouter(tags=["causality-public"])
editorial_router = APIRouter(tags=["causality-editorial"])


_READERS = (Role.ADMIN, Role.REVIEWER, Role.ANALYST)
_EDITORS = (Role.ADMIN, Role.REVIEWER)


# ── Enum coercion helpers ───────────────────────────────────────────────────


def _coerce_shelo_class(value: str) -> SheloClass:
    try:
        return SheloClass(value)
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "INVALID_SHELO_CLASS",
                "message": (
                    f"factor_class must be one of "
                    f"{sorted(c.value for c in SheloClass)}; got {value!r}"
                ),
            },
        ) from exc


def _coerce_interaction_kind(value: str) -> SheloInteractionKind:
    try:
        return SheloInteractionKind(value)
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "INVALID_INTERACTION_KIND",
                "message": (
                    f"interaction_kind must be one of "
                    f"{sorted(k.value for k in SheloInteractionKind)}; "
                    f"got {value!r}"
                ),
            },
        ) from exc


# ── Public: HFACS taxonomy ──────────────────────────────────────────────────


@public_router.get("/public/hfacs/taxonomy", response_model=HfacsTaxonomyResponse)
async def list_hfacs_taxonomy(
    uow: UnitOfWork = Depends(get_uow, scope="function"),
    _user: CurrentUser = Depends(require_role(*_READERS)),
) -> Response:
    view = await ListHfacsTaxonomy(uow).execute()
    payload = HfacsTaxonomyResponse(
        categories=[
            HfacsCategoryItem(
                id=cat.id,
                tier_code=cat.tier_code,
                code=cat.code,
                tier=cat.tier.value if hasattr(cat.tier, "value") else cat.tier,
                name=cat.name,
                description=cat.description,
                is_custom=cat.is_custom,
                subcategories=[
                    HfacsSubcategoryItem(
                        id=s.id,
                        code=s.code,
                        name=s.name,
                        description=s.description,
                        is_custom=s.is_custom,
                    )
                    for s in subs
                ],
            )
            for cat, subs in view.categories
        ]
    )
    return await offloaded_json_response(payload.model_dump(mode="json"))


# ── Public: per-event HFACS + SHELO ─────────────────────────────────────────


@public_router.get("/public/events/{slug}/hfacs", response_model=EventHfacsResponse)
async def get_event_hfacs(
    slug: str,
    uow: UnitOfWork = Depends(get_uow, scope="function"),
    _user: CurrentUser = Depends(require_role(*_READERS)),
) -> Response:
    view = await GetEventHfacs(uow).execute(slug=slug)
    payload = EventHfacsResponse(
        event_id=view.event_id,
        attributions=[
            HfacsAttributionItem(
                id=a.id,
                event_id=a.event_id,
                category_id=cat.id,
                category_code=cat.code,
                category_name=cat.name,
                category_tier=cat.tier.value if hasattr(cat.tier, "value") else cat.tier,
                subcategory_id=sub.id if sub else None,
                subcategory_code=sub.code if sub else None,
                subcategory_name=sub.name if sub else None,
                confidence=a.confidence,
                note=a.note,
                editor_user_id=a.editor_user_id,
                version=a.version,
                created_at=a.created_at,
                updated_at=a.updated_at,
            )
            for a, cat, sub in view.attributions
        ],
    )
    return await offloaded_json_response(payload.model_dump(mode="json"))


@public_router.get("/public/events/{slug}/shelo", response_model=EventSheloResponse)
async def get_event_shelo(
    slug: str,
    uow: UnitOfWork = Depends(get_uow, scope="function"),
    _user: CurrentUser = Depends(require_role(*_READERS)),
) -> Response:
    view = await GetEventShelo(uow).execute(slug=slug)
    payload = EventSheloResponse(
        event_id=view.event_id,
        factors=[
            SheloFactorItem(
                id=f.id,
                event_id=f.event_id,
                factor_class=f.factor_class.value
                if hasattr(f.factor_class, "value")
                else f.factor_class,
                label=f.label,
                description=f.description,
                editor_user_id=f.editor_user_id,
                version=f.version,
                created_at=f.created_at,
                updated_at=f.updated_at,
            )
            for f in view.factors
        ],
        interactions=[
            SheloFactorInteractionItem(
                id=i.id,
                event_id=i.event_id,
                source_factor_id=i.source_factor_id,
                target_factor_id=i.target_factor_id,
                interaction_kind=i.interaction_kind.value
                if hasattr(i.interaction_kind, "value")
                else i.interaction_kind,
                note=i.note,
                editor_user_id=i.editor_user_id,
                created_at=i.created_at,
            )
            for i in view.interactions
        ],
    )
    return await offloaded_json_response(payload.model_dump(mode="json"))


# ── Editorial: HFACS attributions ───────────────────────────────────────────


def _attribution_to_item(a: Any, cat: Any = None, sub: Any = None) -> HfacsAttributionItem:
    """Render an attribution for editorial responses.

    Editorial endpoints don't always have cat/sub loaded (the
    attach endpoint returns the row before reading the category
    back), so we default to the row's IDs and fill in display
    strings when present.
    """
    return HfacsAttributionItem(
        id=a.id,
        event_id=a.event_id,
        category_id=a.category_id,
        category_code=getattr(cat, "code", "") if cat else "",
        category_name=getattr(cat, "name", "") if cat else "",
        category_tier=(
            cat.tier.value if cat and hasattr(cat.tier, "value") else getattr(cat, "tier", "")
        )
        if cat
        else "",
        subcategory_id=a.subcategory_id,
        subcategory_code=getattr(sub, "code", None) if sub else None,
        subcategory_name=getattr(sub, "name", None) if sub else None,
        confidence=a.confidence,
        note=a.note,
        editor_user_id=a.editor_user_id,
        version=a.version,
        created_at=a.created_at,
        updated_at=a.updated_at,
    )


@editorial_router.post(
    "/editorial/events/{event_id}/hfacs",
    response_model=HfacsAttributionItem,
    status_code=201,
)
async def attach_event_hfacs(
    event_id: UUID,
    request: AttachHfacsAttributionRequest,
    uow: UnitOfWork = Depends(get_uow, scope="function"),
    user: CurrentUser = Depends(require_role(*_EDITORS)),
) -> Response:
    attribution = await AttachEventHfacsAttribution(uow).execute(
        AttachHfacsAttributionInput(
            event_id=event_id,
            category_id=request.category_id,
            subcategory_id=request.subcategory_id,
            confidence=request.confidence,
            note=request.note,
            editor_user_id=user.user_id,
        )
    )
    cat = await uow.hfacs_categories.get(attribution.category_id)
    sub = (
        await uow.hfacs_subcategories.get(attribution.subcategory_id)
        if attribution.subcategory_id
        else None
    )
    return await offloaded_json_response(
        _attribution_to_item(attribution, cat, sub).model_dump(mode="json"),
        status_code=201,
    )


@editorial_router.put(
    "/editorial/events/{event_id}/hfacs/{attribution_id}",
    response_model=HfacsAttributionItem,
)
async def update_event_hfacs(
    event_id: UUID,
    attribution_id: UUID,
    request: UpdateHfacsAttributionRequest,
    uow: UnitOfWork = Depends(get_uow, scope="function"),
    user: CurrentUser = Depends(require_role(*_EDITORS)),
) -> Response:
    attribution = await UpdateEventHfacsAttribution(uow).execute(
        UpdateHfacsAttributionInput(
            attribution_id=attribution_id,
            expected_version=request.expected_version,
            confidence=request.confidence,
            note=request.note,
            editor_user_id=user.user_id,
        )
    )
    cat = await uow.hfacs_categories.get(attribution.category_id)
    sub = (
        await uow.hfacs_subcategories.get(attribution.subcategory_id)
        if attribution.subcategory_id
        else None
    )
    return await offloaded_json_response(
        _attribution_to_item(attribution, cat, sub).model_dump(mode="json")
    )


@editorial_router.delete(
    "/editorial/events/{event_id}/hfacs/{attribution_id}",
    status_code=204,
)
async def delete_event_hfacs(
    event_id: UUID,
    attribution_id: UUID,
    uow: UnitOfWork = Depends(get_uow, scope="function"),
    _user: CurrentUser = Depends(require_role(*_EDITORS)),
) -> Response:
    await DeleteEventHfacsAttribution(uow).execute(attribution_id)
    return Response(status_code=204)


# ── Editorial: SHELO factors ────────────────────────────────────────────────


def _factor_to_item(f: Any) -> SheloFactorItem:
    return SheloFactorItem(
        id=f.id,
        event_id=f.event_id,
        factor_class=f.factor_class.value if hasattr(f.factor_class, "value") else f.factor_class,
        label=f.label,
        description=f.description,
        editor_user_id=f.editor_user_id,
        version=f.version,
        created_at=f.created_at,
        updated_at=f.updated_at,
    )


@editorial_router.post(
    "/editorial/events/{event_id}/shelo/factors",
    response_model=SheloFactorItem,
    status_code=201,
)
async def attach_shelo_factor(
    event_id: UUID,
    request: AttachSheloFactorRequest,
    uow: UnitOfWork = Depends(get_uow, scope="function"),
    user: CurrentUser = Depends(require_role(*_EDITORS)),
) -> Response:
    factor = await AttachSheloFactor(uow).execute(
        AttachSheloFactorInput(
            event_id=event_id,
            factor_class=_coerce_shelo_class(request.factor_class),
            label=request.label,
            description=request.description,
            editor_user_id=user.user_id,
        )
    )
    return await offloaded_json_response(
        _factor_to_item(factor).model_dump(mode="json"), status_code=201
    )


@editorial_router.put(
    "/editorial/events/{event_id}/shelo/factors/{factor_id}",
    response_model=SheloFactorItem,
)
async def update_shelo_factor(
    event_id: UUID,
    factor_id: UUID,
    request: UpdateSheloFactorRequest,
    uow: UnitOfWork = Depends(get_uow, scope="function"),
    user: CurrentUser = Depends(require_role(*_EDITORS)),
) -> Response:
    factor = await UpdateSheloFactor(uow).execute(
        UpdateSheloFactorInput(
            factor_id=factor_id,
            expected_version=request.expected_version,
            factor_class=_coerce_shelo_class(request.factor_class),
            label=request.label,
            description=request.description,
            editor_user_id=user.user_id,
        )
    )
    return await offloaded_json_response(_factor_to_item(factor).model_dump(mode="json"))


@editorial_router.delete(
    "/editorial/events/{event_id}/shelo/factors/{factor_id}",
    status_code=204,
)
async def delete_shelo_factor(
    event_id: UUID,
    factor_id: UUID,
    uow: UnitOfWork = Depends(get_uow, scope="function"),
    _user: CurrentUser = Depends(require_role(*_EDITORS)),
) -> Response:
    await DeleteSheloFactor(uow).execute(factor_id)
    return Response(status_code=204)


# ── Editorial: SHELO interactions ───────────────────────────────────────────


@editorial_router.post(
    "/editorial/events/{event_id}/shelo/interactions",
    response_model=SheloFactorInteractionItem,
    status_code=201,
)
async def attach_shelo_interaction(
    event_id: UUID,
    request: AttachSheloInteractionRequest,
    uow: UnitOfWork = Depends(get_uow, scope="function"),
    user: CurrentUser = Depends(require_role(*_EDITORS)),
) -> Response:
    interaction = await AttachSheloInteraction(uow).execute(
        AttachSheloInteractionInput(
            event_id=event_id,
            source_factor_id=request.source_factor_id,
            target_factor_id=request.target_factor_id,
            interaction_kind=_coerce_interaction_kind(request.interaction_kind),
            note=request.note,
            editor_user_id=user.user_id,
        )
    )
    payload = SheloFactorInteractionItem(
        id=interaction.id,
        event_id=interaction.event_id,
        source_factor_id=interaction.source_factor_id,
        target_factor_id=interaction.target_factor_id,
        interaction_kind=interaction.interaction_kind.value
        if hasattr(interaction.interaction_kind, "value")
        else interaction.interaction_kind,
        note=interaction.note,
        editor_user_id=interaction.editor_user_id,
        created_at=interaction.created_at,
    )
    return await offloaded_json_response(payload.model_dump(mode="json"), status_code=201)


@editorial_router.delete(
    "/editorial/events/{event_id}/shelo/interactions/{interaction_id}",
    status_code=204,
)
async def delete_shelo_interaction(
    event_id: UUID,
    interaction_id: UUID,
    uow: UnitOfWork = Depends(get_uow, scope="function"),
    _user: CurrentUser = Depends(require_role(*_EDITORS)),
) -> Response:
    await DeleteSheloInteraction(uow).execute(interaction_id)
    return Response(status_code=204)
