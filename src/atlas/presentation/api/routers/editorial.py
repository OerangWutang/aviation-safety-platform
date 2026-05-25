"""Editorial workflow API router (Phase 9).

Curator-facing write paths layered over the publication overlay.  All
endpoints require a reviewer role except ``retract`` which requires
admin (retraction is reputationally consequential and cannot be
undone).

Endpoint summary
----------------

::

    POST   /editorial/pages                          create (DRAFT)
    GET    /editorial/pages                          list (any non-RETRACTED status)
    GET    /editorial/pages/{id}                     load
    PATCH  /editorial/pages/{id}                     edit in place (DRAFT only)
    POST   /editorial/pages/{id}/submit              DRAFT      -> IN_REVIEW
    POST   /editorial/pages/{id}/request-changes     IN_REVIEW  -> DRAFT
    POST   /editorial/pages/{id}/approve             IN_REVIEW  -> APPROVED
    POST   /editorial/pages/{id}/reject              APPROVED   -> DRAFT
    POST   /editorial/pages/{id}/publish             APPROVED   -> PUBLISHED
                                                     ARCHIVED   -> PUBLISHED
    POST   /editorial/pages/{id}/archive             PUBLISHED  -> ARCHIVED
    POST   /editorial/pages/{id}/reopen              ARCHIVED   -> DRAFT
    POST   /editorial/pages/{id}/retract             PUBLISHED  -> RETRACTED (admin)
    GET    /editorial/pages/{id}/revisions           audit trail

The router never reads from the request body to populate
``editor_user_id`` — that always comes from the authenticated
``CurrentUser``.  Client-supplied identity would defeat the audit
trail.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response

from atlas.application.dto import CurrentUser
from atlas.application.unit_of_work import UnitOfWork
from atlas.application.use_cases.editorial import (
    ApprovePublicEventPage,
    ArchivePublicEventPage,
    CreatePublicEventPage,
    CreatePublicEventPageInput,
    ListEditorialPages,
    ListPageRevisions,
    PublishPublicEventPage,
    RejectPublicEventPage,
    ReopenPublicEventPage,
    RequestChanges,
    RetractPublicEventPage,
    SubmitPublicEventPage,
    TransitionPublicEventPageInput,
    UpdatePublicEventPage,
    UpdatePublicEventPageInput,
)
from atlas.domain.enums import Role
from atlas.domain.publication.entities import PublicationStatus
from atlas.domain.publication.exceptions import PublicEventPageNotFoundError
from atlas.presentation.api.dependencies import get_uow, require_role
from atlas.presentation.api.responses import offloaded_json_response
from atlas.presentation.api.schemas.editorial import (
    CreatePublicEventPageRequest,
    EditorialPageListResponse,
    EditorialPageSummary,
    PageRevisionItem,
    PageRevisionsResponse,
    PublicEventPageResponse,
    RetractRequest,
    TransitionRequest,
    UpdatePublicEventPageRequest,
    page_to_response,
)

router = APIRouter(prefix="/editorial/pages", tags=["editorial"])


# Reviewer-or-admin can do everything except retract.
_EDITORIAL_ROLES = (Role.ADMIN, Role.REVIEWER)
# Retract is admin-only — a deliberate gating decision because
# RETRACTED is terminal and forever-visible as a 410 on the public
# surface.
_RETRACTION_ROLES = (Role.ADMIN,)


# ── Helpers ──────────────────────────────────────────────────────────────────


async def _page_response(page: object, *, status_code: int = 200) -> Response:
    return await offloaded_json_response(
        PublicEventPageResponse.model_validate(page_to_response(page)).model_dump(mode="json"),
        status_code=status_code,
    )


# ── Create / read / update ───────────────────────────────────────────────────


@router.post("", response_model=PublicEventPageResponse, status_code=201)
async def create_page(
    request: CreatePublicEventPageRequest,
    uow: UnitOfWork = Depends(get_uow, scope="function"),
    user: CurrentUser = Depends(require_role(*_EDITORIAL_ROLES)),
) -> Response:
    page = await CreatePublicEventPage(uow).execute(
        CreatePublicEventPageInput(
            event_id=request.event_id,
            slug=request.slug,
            title=request.title,
            short_summary=request.short_summary,
            narrative_markdown=request.narrative_markdown,
            editor_user_id=user.user_id,
        )
    )
    return await _page_response(page, status_code=201)


@router.get("", response_model=EditorialPageListResponse)
async def list_pages(
    statuses: list[PublicationStatus] | None = Query(
        default=None,
        description=("Filter to specific statuses.  Omit to list all non-RETRACTED rows."),
    ),
    limit: int = Query(default=25, ge=1, le=100),
    cursor: UUID | None = Query(
        default=None,
        description="Keyset cursor returned by the previous response.",
    ),
    uow: UnitOfWork = Depends(get_uow, scope="function"),
    _user: CurrentUser = Depends(require_role(*_EDITORIAL_ROLES)),
) -> Response:
    status_set = frozenset(statuses) if statuses else None
    result = await ListEditorialPages(uow).execute(
        statuses=status_set, limit=limit, after_id=cursor
    )
    await uow.rollback()
    payload = EditorialPageListResponse(
        items=[
            EditorialPageSummary(
                id=i.id,
                slug=i.slug,
                title=i.title,
                status=i.status,
                version=i.version,
                updated_at=i.updated_at,
                last_published_at=i.last_published_at,
                allowed_next_statuses=i.allowed_next_statuses,
            )
            for i in result.items
        ],
        limit=result.limit,
        next_cursor=result.next_cursor,
    )
    return await offloaded_json_response(payload.model_dump(mode="json"))


@router.get("/{page_id}", response_model=PublicEventPageResponse)
async def get_page(
    page_id: UUID,
    uow: UnitOfWork = Depends(get_uow, scope="function"),
    _user: CurrentUser = Depends(require_role(*_EDITORIAL_ROLES)),
) -> Response:
    page = await uow.public_event_pages.get_by_id(page_id)
    await uow.rollback()
    if page is None:
        raise PublicEventPageNotFoundError(f"Public event page {page_id} not found")
    return await _page_response(page)


@router.patch("/{page_id}", response_model=PublicEventPageResponse)
async def update_page(
    page_id: UUID,
    request: UpdatePublicEventPageRequest,
    uow: UnitOfWork = Depends(get_uow, scope="function"),
    user: CurrentUser = Depends(require_role(*_EDITORIAL_ROLES)),
) -> Response:
    page = await UpdatePublicEventPage(uow).execute(
        UpdatePublicEventPageInput(
            page_id=page_id,
            expected_version=request.expected_version,
            editor_user_id=user.user_id,
            title=request.title,
            short_summary=request.short_summary,
            narrative_markdown=request.narrative_markdown,
            slug=request.slug,
            correction_note=request.correction_note,
            transition_reason=request.transition_reason,
        )
    )
    return await _page_response(page)


# ── State transitions ────────────────────────────────────────────────────────


def _transition_input(
    page_id: UUID, request: TransitionRequest, user: CurrentUser
) -> TransitionPublicEventPageInput:
    return TransitionPublicEventPageInput(
        page_id=page_id,
        expected_version=request.expected_version,
        editor_user_id=user.user_id,
        transition_reason=request.transition_reason,
    )


@router.post("/{page_id}/submit", response_model=PublicEventPageResponse)
async def submit_page(
    page_id: UUID,
    request: TransitionRequest,
    uow: UnitOfWork = Depends(get_uow, scope="function"),
    user: CurrentUser = Depends(require_role(*_EDITORIAL_ROLES)),
) -> Response:
    page = await SubmitPublicEventPage(uow).execute(_transition_input(page_id, request, user))
    return await _page_response(page)


@router.post("/{page_id}/request-changes", response_model=PublicEventPageResponse)
async def request_changes_page(
    page_id: UUID,
    request: TransitionRequest,
    uow: UnitOfWork = Depends(get_uow, scope="function"),
    user: CurrentUser = Depends(require_role(*_EDITORIAL_ROLES)),
) -> Response:
    page = await RequestChanges(uow).execute(_transition_input(page_id, request, user))
    return await _page_response(page)


@router.post("/{page_id}/approve", response_model=PublicEventPageResponse)
async def approve_page(
    page_id: UUID,
    request: TransitionRequest,
    uow: UnitOfWork = Depends(get_uow, scope="function"),
    user: CurrentUser = Depends(require_role(*_EDITORIAL_ROLES)),
) -> Response:
    page = await ApprovePublicEventPage(uow).execute(_transition_input(page_id, request, user))
    return await _page_response(page)


@router.post("/{page_id}/reject", response_model=PublicEventPageResponse)
async def reject_page(
    page_id: UUID,
    request: TransitionRequest,
    uow: UnitOfWork = Depends(get_uow, scope="function"),
    user: CurrentUser = Depends(require_role(*_EDITORIAL_ROLES)),
) -> Response:
    page = await RejectPublicEventPage(uow).execute(_transition_input(page_id, request, user))
    return await _page_response(page)


@router.post("/{page_id}/publish", response_model=PublicEventPageResponse)
async def publish_page(
    page_id: UUID,
    request: TransitionRequest,
    uow: UnitOfWork = Depends(get_uow, scope="function"),
    user: CurrentUser = Depends(require_role(*_EDITORIAL_ROLES)),
) -> Response:
    page = await PublishPublicEventPage(uow).execute(_transition_input(page_id, request, user))
    return await _page_response(page)


@router.post("/{page_id}/archive", response_model=PublicEventPageResponse)
async def archive_page(
    page_id: UUID,
    request: TransitionRequest,
    uow: UnitOfWork = Depends(get_uow, scope="function"),
    user: CurrentUser = Depends(require_role(*_EDITORIAL_ROLES)),
) -> Response:
    page = await ArchivePublicEventPage(uow).execute(_transition_input(page_id, request, user))
    return await _page_response(page)


@router.post("/{page_id}/reopen", response_model=PublicEventPageResponse)
async def reopen_page(
    page_id: UUID,
    request: TransitionRequest,
    uow: UnitOfWork = Depends(get_uow, scope="function"),
    user: CurrentUser = Depends(require_role(*_EDITORIAL_ROLES)),
) -> Response:
    page = await ReopenPublicEventPage(uow).execute(_transition_input(page_id, request, user))
    return await _page_response(page)


@router.post("/{page_id}/retract", response_model=PublicEventPageResponse)
async def retract_page(
    page_id: UUID,
    request: RetractRequest,
    uow: UnitOfWork = Depends(get_uow, scope="function"),
    user: CurrentUser = Depends(require_role(*_RETRACTION_ROLES)),
) -> Response:
    page = await RetractPublicEventPage(uow).execute(
        TransitionPublicEventPageInput(
            page_id=page_id,
            expected_version=request.expected_version,
            editor_user_id=user.user_id,
            transition_reason=request.transition_reason,
            retraction_note=request.retraction_note,
        )
    )
    return await _page_response(page)


# ── Revisions ────────────────────────────────────────────────────────────────


@router.get("/{page_id}/revisions", response_model=PageRevisionsResponse)
async def list_revisions(
    page_id: UUID,
    uow: UnitOfWork = Depends(get_uow, scope="function"),
    _user: CurrentUser = Depends(require_role(*_EDITORIAL_ROLES)),
) -> Response:
    revisions = await ListPageRevisions(uow).execute(page_id)
    await uow.rollback()
    payload = PageRevisionsResponse(
        page_id=page_id,
        revisions=[
            PageRevisionItem(
                id=r.id,
                page_id=r.page_id,
                version_at_moment=r.version_at_moment,
                from_status=r.from_status,
                to_status=r.to_status,
                title=r.title,
                short_summary=r.short_summary,
                narrative_markdown=r.narrative_markdown,
                editor_user_id=r.editor_user_id,
                transition_reason=r.transition_reason,
                correction_note=r.correction_note,
                created_at=r.created_at,
            )
            for r in revisions
        ],
    )
    return await offloaded_json_response(payload.model_dump(mode="json"))
