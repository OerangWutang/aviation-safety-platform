"""Public Aviation Safety Atlas read endpoints (Phase 1).

Slug-keyed, projection-backed accident pages.  All endpoints are
read-only and idempotent.  In this module, "public" means published
Atlas record data; it does not mean anonymous access.  The routes keep
the existing ``X-API-Key`` reader-role gate so deployments do not expose
the published corpus accidentally.

Endpoints
---------

- ``GET  /public/events`` — keyset-paginated list of PUBLISHED pages.
- ``GET  /public/events/{slug}`` — detail; 404 if DRAFT/missing, 410
  if RETRACTED.
- ``GET  /public/events/{slug}/evidence`` — public claim/source view.
- ``GET  /public/events/{slug}/timeline`` — Chronos timeline.
- ``GET  /public/events/{slug}/related`` — related events via Orion.

Slug validation
---------------

The path-level regex (mirrors :data:`SLUG_PATTERN`) ensures a malformed
slug returns 422 from FastAPI's path parser rather than hitting the
database.  This keeps query plans clean and prevents adversarial
inputs from showing up in PostgreSQL logs.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Path, Query, Response

from atlas.application.dto import CurrentUser
from atlas.application.unit_of_work import UnitOfWork
from atlas.application.use_cases.audit import GetPublicEventAudit
from atlas.application.use_cases.public_events import (
    DEFAULT_PUBLIC_LIST_LIMIT,
    MAX_PUBLIC_LIST_LIMIT,
    GetPublicEventEvidence,
    GetPublicEventPage,
    GetPublicEventRelated,
    GetPublicEventTimeline,
    ListPublicEvents,
)
from atlas.domain.enums import Role
from atlas.domain.publication.slug import SLUG_PATTERN
from atlas.presentation.api.dependencies import get_public_uow, require_role
from atlas.presentation.api.responses import offloaded_json_response
from atlas.presentation.api.schemas.audit import (
    AuditFieldRow,
    PageAuditResponse,
)
from atlas.presentation.api.schemas.public import (
    PublicEventDetailResponse,
    PublicEventEditorial,
    PublicEventListResponse,
    PublicEventSummary,
    PublicEvidenceClaimItem,
    PublicEvidenceResponse,
    PublicEvidenceSourceItem,
    PublicRelatedEventItem,
    PublicRelatedResponse,
    PublicTimelineEventItem,
    PublicTimelineResponse,
)

router = APIRouter(prefix="/public/events", tags=["public"])


# "Public" is a data-classification label, not an auth bypass.  See module
# docstring for the access rationale.
_READERS = (Role.ADMIN, Role.REVIEWER, Role.ANALYST)


# Strictest Path slug validation possible at the router boundary.  A
# bad slug never reaches the use case or the DB; the response is a
# stable 422 from FastAPI's pydantic-driven path parser.
_SLUG_PATH = Path(
    ...,
    description="Public slug for the event page.",
    pattern=SLUG_PATTERN,
    min_length=1,
    max_length=160,
)


@router.get("", response_model=PublicEventListResponse)
async def list_events(
    limit: int = Query(
        default=DEFAULT_PUBLIC_LIST_LIMIT,
        ge=1,
        le=MAX_PUBLIC_LIST_LIMIT,
        description="Maximum number of pages returned.",
    ),
    cursor: UUID | None = Query(
        default=None,
        description=(
            "Opaque keyset cursor returned by the previous response. Omit on the first call."
        ),
    ),
    uow: UnitOfWork = Depends(get_public_uow, scope="function"),
    _user: CurrentUser = Depends(require_role(*_READERS)),
) -> Response:
    result = await ListPublicEvents(uow).execute(limit=limit, after_id=cursor)
    await uow.rollback()
    payload = PublicEventListResponse(
        items=[
            PublicEventSummary(
                slug=item.slug,
                title=item.title,
                short_summary=item.short_summary,
                event_date=item.event_date,
                location=item.location,
                operator=item.operator,
                aircraft_type=item.aircraft_type,
                fatalities_total=item.fatalities_total,
                confidence=item.confidence,
                has_unresolved_conflicts=item.has_unresolved_conflicts,
                last_published_at=item.last_published_at,
            )
            for item in result.items
        ],
        limit=result.limit,
        next_cursor=result.next_cursor,
    )
    return await offloaded_json_response(payload.model_dump(mode="json"))


@router.get("/{slug}", response_model=PublicEventDetailResponse)
async def get_event(
    slug: str = _SLUG_PATH,
    uow: UnitOfWork = Depends(get_public_uow, scope="function"),
    _user: CurrentUser = Depends(require_role(*_READERS)),
) -> Response:
    detail = await GetPublicEventPage(uow).execute(slug)
    await uow.rollback()
    payload = PublicEventDetailResponse(
        slug=detail.slug,
        canonical_event_id=detail.canonical_event_id,
        editorial=PublicEventEditorial(
            title=detail.title,
            short_summary=detail.short_summary,
            narrative_markdown=detail.narrative_markdown,
        ),
        fields=detail.fields,
        completeness_score=detail.completeness_score,
        confidence=detail.confidence,
        unresolved_conflict_fields=detail.unresolved_conflict_fields,
        projection_version=detail.projection_version,
        first_published_at=detail.first_published_at,
        last_published_at=detail.last_published_at,
        last_updated_at=detail.last_updated_at,
    )
    return await offloaded_json_response(payload.model_dump(mode="json"))


@router.get("/{slug}/evidence", response_model=PublicEvidenceResponse)
async def get_event_evidence(
    slug: str = _SLUG_PATH,
    uow: UnitOfWork = Depends(get_public_uow, scope="function"),
    _user: CurrentUser = Depends(require_role(*_READERS)),
) -> Response:
    evidence = await GetPublicEventEvidence(uow).execute(slug)
    await uow.rollback()
    payload = PublicEvidenceResponse(
        slug=evidence.slug,
        canonical_event_id=evidence.canonical_event_id,
        claims=[
            PublicEvidenceClaimItem(
                field_name=c.field_name,
                field_value=c.field_value,
                claim_type=c.claim_type,
                source_name=c.source_name,
                source_kind=c.source_kind,
                source_reliability_tier=c.source_reliability_tier,
                is_winning=c.is_winning,
                is_superseded=c.is_superseded,
                created_at=c.created_at,
            )
            for c in evidence.claims
        ],
        sources=[
            PublicEvidenceSourceItem(
                name=s.name,
                kind=s.kind,
                reliability_tier=s.reliability_tier,
            )
            for s in evidence.sources
        ],
        claim_count=evidence.claim_count,
        truncated=evidence.truncated,
    )
    return await offloaded_json_response(payload.model_dump(mode="json"))


@router.get("/{slug}/timeline", response_model=PublicTimelineResponse)
async def get_event_timeline(
    slug: str = _SLUG_PATH,
    uow: UnitOfWork = Depends(get_public_uow, scope="function"),
    _user: CurrentUser = Depends(require_role(*_READERS)),
) -> Response:
    timeline = await GetPublicEventTimeline(uow).execute(slug)
    await uow.rollback()
    payload = PublicTimelineResponse(
        slug=timeline.slug,
        canonical_event_id=timeline.canonical_event_id,
        events=[
            PublicTimelineEventItem(
                event_type=e.event_type,
                occurred_at=e.occurred_at,
                timestamp_precision=e.timestamp_precision,
                sequence_index=e.sequence_index,
                description=e.description,
            )
            for e in timeline.events
        ],
    )
    return await offloaded_json_response(payload.model_dump(mode="json"))


@router.get("/{slug}/related", response_model=PublicRelatedResponse)
async def get_event_related(
    slug: str = _SLUG_PATH,
    uow: UnitOfWork = Depends(get_public_uow, scope="function"),
    _user: CurrentUser = Depends(require_role(*_READERS)),
) -> Response:
    related = await GetPublicEventRelated(uow).execute(slug)
    await uow.rollback()
    payload = PublicRelatedResponse(
        slug=related.slug,
        canonical_event_id=related.canonical_event_id,
        items=[
            PublicRelatedEventItem(
                slug=item.slug,
                title=item.title,
                short_summary=item.short_summary,
                last_published_at=item.last_published_at,
                relation=item.relation,
            )
            for item in related.items
        ],
    )
    return await offloaded_json_response(payload.model_dump(mode="json"))


@router.get("/{slug}/audit", response_model=PageAuditResponse)
async def get_event_audit(
    slug: str = _SLUG_PATH,
    uow: UnitOfWork = Depends(get_public_uow, scope="function"),
    _user: CurrentUser = Depends(require_role(*_READERS)),
) -> Response:
    """Page-level audit summary (Phase 11).

    Surfaces, in plain English, what the projection says, what is
    disputed, what was set by an editor, and how confident the
    underlying evidence is.  Field-level deep dives live under the
    ``/audit`` namespace and link to here for context.
    """
    audit = await GetPublicEventAudit(uow).execute(slug)
    await uow.rollback()
    payload = PageAuditResponse(
        slug=audit.slug,
        canonical_event_id=audit.canonical_event_id,
        summary=audit.summary,
        confidence=audit.confidence,
        confidence_meaning=audit.confidence_meaning,
        projection_version=audit.projection_version,
        last_updated_at=audit.last_updated_at,
        fields=[
            AuditFieldRow(
                field_name=row.field_name,
                current_value=row.current_value,
                is_disputed=row.is_disputed,
                is_manually_overridden=row.is_manually_overridden,
                confidence=row.confidence,
                plain_english=row.plain_english,
            )
            for row in audit.fields
        ],
    )
    return await offloaded_json_response(payload.model_dump(mode="json"))
