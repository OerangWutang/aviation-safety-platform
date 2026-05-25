"""Orion v0.1 Entity Intelligence API router."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query

from atlas.application.unit_of_work import UnitOfWork
from atlas.application.use_cases.extract_orion_entities_from_event import (
    ExtractOrionEntitiesFromEvent,
)
from atlas.domain.enums import OrionEntityType, Role
from atlas.presentation.api.dependencies import get_uow, require_role
from atlas.presentation.api.schemas.orion import (
    OrionEntityResponse,
    OrionEntitySearchResponse,
    OrionEventEntitiesResponse,
    OrionExtractionResponse,
    OrionIdentifierResponse,
    OrionRelationshipResponse,
    OrionReviewResponse,
)

router = APIRouter(prefix="/orion", tags=["orion"])

_READERS = (Role.ADMIN, Role.REVIEWER, Role.ANALYST)
_WRITERS = (Role.ADMIN, Role.REVIEWER)


def _entity_response(entity, identifiers=None) -> OrionEntityResponse:
    return OrionEntityResponse(
        id=entity.id,
        entity_type=entity.entity_type,
        canonical_name=entity.canonical_name,
        status=entity.status,
        confidence=entity.confidence,
        merged_into_entity_id=entity.merged_into_entity_id,
        created_at=entity.created_at,
        updated_at=entity.updated_at,
        identifiers=[
            OrionIdentifierResponse(
                id=i.id,
                entity_id=i.entity_id,
                identifier_type=i.identifier_type,
                identifier_value=i.identifier_value,
                normalized_value=i.normalized_value,
                confidence=i.confidence,
                source_claim_id=i.source_claim_id,
                created_at=i.created_at,
            )
            for i in (identifiers or [])
        ],
    )


def _relationship_response(relationship) -> OrionRelationshipResponse:
    return OrionRelationshipResponse(
        id=relationship.id,
        subject_entity_id=relationship.subject_entity_id,
        relationship_type=relationship.relationship_type,
        object_entity_id=relationship.object_entity_id,
        accident_event_id=relationship.accident_event_id,
        confidence=relationship.confidence,
        created_at=relationship.created_at,
    )


@router.post("/events/{event_id}/extract", response_model=OrionExtractionResponse)
async def extract_entities_from_event(
    event_id: UUID,
    uow: UnitOfWork = Depends(get_uow, scope="function"),
    _user=Depends(require_role(*_WRITERS)),
) -> OrionExtractionResponse:
    result = await ExtractOrionEntitiesFromEvent(uow).execute(event_id)
    return OrionExtractionResponse(
        event_id=result.event_id,
        entities_created_count=result.entities_created_count,
        entities_reused_count=result.entities_reused_count,
        relationships_created_count=result.relationships_created_count,
        entity_ids=result.entity_ids,
        relationship_ids=result.relationship_ids,
    )


@router.get("/events/{event_id}/entities", response_model=OrionEventEntitiesResponse)
async def get_event_entities(
    event_id: UUID,
    uow: UnitOfWork = Depends(get_uow, scope="function"),
    _user=Depends(require_role(*_READERS)),
) -> OrionEventEntitiesResponse:
    entities = await uow.orion_entities.list_for_event(event_id)
    relationships = await uow.orion_relationships.list_for_event(event_id)

    entity_responses: list[OrionEntityResponse] = []
    for entity in entities:
        identifiers = await uow.orion_identifiers.list_for_entity(entity.id)
        entity_responses.append(_entity_response(entity, identifiers))

    await uow.rollback()
    return OrionEventEntitiesResponse(
        event_id=event_id,
        entities=entity_responses,
        relationships=[_relationship_response(r) for r in relationships],
    )


@router.get("/entities/search", response_model=OrionEntitySearchResponse)
async def search_entities(
    q: str = Query(..., min_length=1),
    entity_type: OrionEntityType | None = Query(default=None),
    limit: int = Query(default=25, ge=1, le=100),
    uow: UnitOfWork = Depends(get_uow, scope="function"),
    _user=Depends(require_role(*_READERS)),
) -> OrionEntitySearchResponse:
    entities = await uow.orion_entities.search(q, entity_type=entity_type, limit=limit)
    await uow.rollback()
    return OrionEntitySearchResponse(
        query=q,
        entity_type=entity_type,
        results=[_entity_response(e) for e in entities],
    )


@router.get("/entities/{entity_id}", response_model=OrionEntityResponse)
async def get_entity(
    entity_id: UUID,
    uow: UnitOfWork = Depends(get_uow, scope="function"),
    _user=Depends(require_role(*_READERS)),
) -> OrionEntityResponse:
    entity = await uow.orion_entities.get(entity_id)
    if entity is None:
        raise HTTPException(status_code=404, detail="Orion entity not found")
    identifiers = await uow.orion_identifiers.list_for_entity(entity_id)
    await uow.rollback()
    return _entity_response(entity, identifiers)


@router.get("/entities/{entity_id}/relationships", response_model=list[OrionRelationshipResponse])
async def get_entity_relationships(
    entity_id: UUID,
    uow: UnitOfWork = Depends(get_uow, scope="function"),
    _user=Depends(require_role(*_READERS)),
) -> list[OrionRelationshipResponse]:
    entity = await uow.orion_entities.get(entity_id)
    if entity is None:
        raise HTTPException(status_code=404, detail="Orion entity not found")
    relationships = await uow.orion_relationships.list_for_entity(entity_id)
    await uow.rollback()
    return [_relationship_response(r) for r in relationships]


@router.get("/reviews/pending", response_model=list[OrionReviewResponse])
async def list_pending_reviews(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    uow: UnitOfWork = Depends(get_uow, scope="function"),
    _user=Depends(require_role(*_READERS)),
) -> list[OrionReviewResponse]:
    reviews = await uow.orion_reviews.list_pending(limit=limit, offset=offset)
    await uow.rollback()
    return [
        OrionReviewResponse(
            id=r.id,
            candidate_entity_id_a=r.candidate_entity_id_a,
            candidate_entity_id_b=r.candidate_entity_id_b,
            entity_type=r.entity_type,
            match_score=r.match_score,
            matched_identifiers=r.matched_identifiers,
            status=r.status,
            created_at=r.created_at,
            resolved_at=r.resolved_at,
            resolution_note=r.resolution_note,
        )
        for r in reviews
    ]
