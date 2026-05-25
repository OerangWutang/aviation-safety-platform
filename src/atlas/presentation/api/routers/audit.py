"""Audit explanations API router (Phase 11).

Three endpoints, all read-only, all reader-gated:

- field-level explanation
- claim-level explanation
- source-verification view

The per-page summary lives on the public router as
``GET /public/events/{slug}/audit`` to keep slug-keyed surfaces in
one place; event-id-keyed endpoints live here.
"""

from __future__ import annotations

from enum import StrEnum
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response

from atlas.application.dto import CurrentUser
from atlas.application.unit_of_work import UnitOfWork
from atlas.application.use_cases.audit import (
    GetClaimExplanation,
    GetFieldExplanation,
    GetSourceVerification,
)
from atlas.domain.enums import Role
from atlas.presentation.api.dependencies import get_uow, require_role
from atlas.presentation.api.responses import offloaded_json_response
from atlas.presentation.api.schemas.audit import (
    ClaimExplanationResponse,
    ClaimHistoryItem,
    ExpertDetail,
    FieldExplanationConflictItem,
    FieldExplanationLoserItem,
    FieldExplanationResponse,
    FieldExplanationWinnerItem,
    SourceVerificationResponse,
)

router = APIRouter(prefix="/audit", tags=["audit"])

# Reader-and-above gate.  These endpoints surface evidence that is
# already public via the projection and provenance endpoints — adding
# them to the reader role does not expose new information, it just
# makes the existing evidence chain legible to non-engineers.
_READERS = (Role.ADMIN, Role.REVIEWER, Role.ANALYST)


class _DetailMode(StrEnum):
    SUMMARY = "summary"
    EXPERT = "expert"


@router.get(
    "/events/{event_id}/fields/{field_name}/explanation",
    response_model=FieldExplanationResponse,
)
async def field_explanation(
    event_id: UUID,
    field_name: str,
    detail: _DetailMode = Query(
        default=_DetailMode.SUMMARY,
        description=(
            "Response detail level.  ``summary`` (default) returns "
            "non-technical prose.  ``expert`` adds claim ids, source "
            "reliability tiers, and timestamps."
        ),
    ),
    uow: UnitOfWork = Depends(get_uow, scope="function"),
    _user: CurrentUser = Depends(require_role(*_READERS)),
) -> Response:
    explanation = await GetFieldExplanation(uow, expert=(detail == _DetailMode.EXPERT)).execute(
        event_id=event_id, field_name=field_name
    )
    await uow.rollback()
    payload = FieldExplanationResponse(
        event_id=explanation.event_id,
        field_name=explanation.field_name,
        has_winner=explanation.has_winner,
        winner=(
            FieldExplanationWinnerItem(
                field_name=explanation.winner.field_name,
                current_value=explanation.winner.current_value,
                plain_english=explanation.winner.plain_english,
                source_name=explanation.winner.source_name,
                source_kind=explanation.winner.source_kind,
                expert=(
                    ExpertDetail(
                        claim_id=explanation.winner.expert.claim_id,
                        claim_type=explanation.winner.expert.claim_type,
                        source_reliability_tier=explanation.winner.expert.source_reliability_tier,
                        created_at=explanation.winner.expert.created_at,
                    )
                    if explanation.winner.expert is not None
                    else None
                ),
            )
            if explanation.winner is not None
            else None
        ),
        losers=[
            FieldExplanationLoserItem(
                source_name=loser.source_name,
                source_kind=loser.source_kind,
                reported_value=loser.reported_value,
                plain_english=loser.plain_english,
                expert=(
                    ExpertDetail(
                        claim_id=loser.expert.claim_id,
                        claim_type=loser.expert.claim_type,
                        source_reliability_tier=loser.expert.source_reliability_tier,
                        created_at=loser.expert.created_at,
                    )
                    if loser.expert is not None
                    else None
                ),
            )
            for loser in explanation.losers
        ],
        losers_truncated=explanation.losers_truncated,
        conflict=(
            FieldExplanationConflictItem(
                status=explanation.conflict.status,
                plain_english=explanation.conflict.plain_english,
                resolved_at=explanation.conflict.resolved_at,
            )
            if explanation.conflict is not None
            else None
        ),
    )
    return await offloaded_json_response(payload.model_dump(mode="json"))


@router.get(
    "/claims/{claim_id}/explanation",
    response_model=ClaimExplanationResponse,
)
async def claim_explanation(
    claim_id: UUID,
    uow: UnitOfWork = Depends(get_uow, scope="function"),
    _user: CurrentUser = Depends(require_role(*_READERS)),
) -> Response:
    explanation = await GetClaimExplanation(uow).execute(claim_id)
    await uow.rollback()
    payload = ClaimExplanationResponse(
        claim_id=explanation.claim_id,
        event_id=explanation.event_id,
        field_name=explanation.field_name,
        field_value=explanation.field_value,
        claim_type=explanation.claim_type,
        plain_english=explanation.plain_english,
        source_name=explanation.source_name,
        source_kind=explanation.source_kind,
        is_winning=explanation.is_winning,
        is_active=explanation.is_active,
        is_superseded=explanation.is_superseded,
        created_at=explanation.created_at,
        history=[
            ClaimHistoryItem(
                action=h.action,
                reason=h.reason,
                to_claim_type=h.to_claim_type,
                from_claim_type=h.from_claim_type,
                created_at=h.created_at,
            )
            for h in explanation.history
        ],
        history_truncated=explanation.history_truncated,
    )
    return await offloaded_json_response(payload.model_dump(mode="json"))


@router.get(
    "/sources/{snapshot_id}/verification",
    response_model=SourceVerificationResponse,
)
async def source_verification(
    snapshot_id: UUID,
    uow: UnitOfWork = Depends(get_uow, scope="function"),
    _user: CurrentUser = Depends(require_role(*_READERS)),
) -> Response:
    response = await GetSourceVerification(uow).execute(snapshot_id)
    await uow.rollback()
    payload = SourceVerificationResponse(
        snapshot_id=response.snapshot_id,
        source_name=response.source_name,
        source_kind=response.source_kind,
        source_record_id=response.source_record_id,
        raw_payload_hash=response.raw_payload_hash,
        captured_at=response.captured_at,
        recipe_version=response.recipe_version,
        recipe_steps=response.recipe_steps,
        verification_note=response.verification_note,
    )
    return await offloaded_json_response(payload.model_dump(mode="json"))
