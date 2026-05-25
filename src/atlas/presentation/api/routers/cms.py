"""CMS router (Phase 10).

One router for all three content kinds, organised by URL prefix
rather than by content kind class:

- Public reads under ``/public/glossary``, ``/public/methodology``,
  ``/public/changelog``.
- Editorial CRUD + transitions under ``/editorial/glossary``,
  ``/editorial/methodology``, ``/editorial/changelog``.

Three considerations decided the single-router layout:

1. **One file, one OpenAPI tag set** keeps the CMS surface visible
   in a single place; future maintainers can find every CMS route
   without grepping across three files.
2. **Per-kind use cases keep the request body schemas honest** —
   each kind's create/update body differs (glossary has ``term``,
   methodology has ``section``, changelog has ``effective_date``).
   A generic endpoint would force a discriminated-union shape that
   loses type safety.
3. **Transition routes are uniform**: same input shape across all
   three kinds, same role gate, same response.

Role gates:

- Public reads: reader+ (analyst).
- Editorial reads and create/update: reader+ (analyst).
- Workflow transitions (submit/approve/etc.): reviewer+.
- Retraction: admin-only.

Visibility on the public surface:

- PUBLISHED → 200.
- RETRACTED → 410 with retraction note.
- Anything else → 404 (no leak that work-in-progress exists).
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response

from atlas.application.dto import CurrentUser
from atlas.application.unit_of_work import UnitOfWork
from atlas.application.use_cases.cms import (
    EDITORIAL_ROLES,
    RETRACT_ROLES,
    TRANSITION_ROLES,
    ApproveChangelogEntry,
    ApproveGlossaryTerm,
    ApproveMethodologyPage,
    ArchiveChangelogEntry,
    ArchiveGlossaryTerm,
    ArchiveMethodologyPage,
    CreateChangelogEntry,
    CreateChangelogEntryInput,
    CreateGlossaryTerm,
    CreateGlossaryTermInput,
    CreateMethodologyPage,
    CreateMethodologyPageInput,
    GetPublicChangelogEntry,
    GetPublicGlossaryTerm,
    GetPublicMethodologyPage,
    ListPublicChangelog,
    ListPublicGlossary,
    ListPublicMethodology,
    PublishChangelogEntry,
    PublishGlossaryTerm,
    PublishMethodologyPage,
    RejectChangelogEntry,
    RejectGlossaryTerm,
    RejectMethodologyPage,
    ReopenChangelogEntry,
    ReopenGlossaryTerm,
    ReopenMethodologyPage,
    RequestChangesChangelogEntry,
    RequestChangesGlossaryTerm,
    RequestChangesMethodologyPage,
    RetractChangelogEntry,
    RetractGlossaryTerm,
    RetractMethodologyPage,
    SubmitChangelogEntry,
    SubmitGlossaryTerm,
    SubmitMethodologyPage,
    TransitionInput,
    UpdateChangelogEntry,
    UpdateChangelogEntryInput,
    UpdateGlossaryTerm,
    UpdateGlossaryTermInput,
    UpdateMethodologyPage,
    UpdateMethodologyPageInput,
)
from atlas.presentation.api.dependencies import get_uow, require_role
from atlas.presentation.api.responses import offloaded_json_response
from atlas.presentation.api.schemas.cms import (
    CreateChangelogEntryRequest,
    CreateGlossaryTermRequest,
    CreateMethodologyPageRequest,
    EditorialChangelogEntry,
    EditorialGlossaryTerm,
    EditorialMethodologyPage,
    PublicChangelogEntry,
    PublicChangelogListResponse,
    PublicGlossaryListResponse,
    PublicGlossaryTerm,
    PublicMethodologyListResponse,
    PublicMethodologyPage,
    PublicMethodologySection,
    TransitionRequest,
    UpdateChangelogEntryRequest,
    UpdateGlossaryTermRequest,
    UpdateMethodologyPageRequest,
)

# Two routers: one for public reads, one for editorial writes.  Same
# file so the routes stay co-located.

public_router = APIRouter(tags=["cms-public"])
editorial_router = APIRouter(tags=["cms-editorial"])


# ── Public: glossary ─────────────────────────────────────────────────────────


@public_router.get("/public/glossary", response_model=PublicGlossaryListResponse)
async def list_public_glossary(
    uow: UnitOfWork = Depends(get_uow, scope="function"),
    _user: CurrentUser = Depends(require_role(*EDITORIAL_ROLES)),
) -> Response:
    terms = await ListPublicGlossary(uow).execute()
    payload = PublicGlossaryListResponse(
        items=[
            PublicGlossaryTerm(
                term=t.term,
                display_term=t.display_term,
                body_markdown=t.body_markdown,
                last_published_at=t.last_published_at,
            )
            for t in terms
        ]
    )
    return await offloaded_json_response(payload.model_dump(mode="json"))


@public_router.get("/public/glossary/{term}", response_model=PublicGlossaryTerm)
async def get_public_glossary_term(
    term: str,
    uow: UnitOfWork = Depends(get_uow, scope="function"),
    _user: CurrentUser = Depends(require_role(*EDITORIAL_ROLES)),
) -> Response:
    t = await GetPublicGlossaryTerm(uow).execute(term)
    payload = PublicGlossaryTerm(
        term=t.term,
        display_term=t.display_term,
        body_markdown=t.body_markdown,
        last_published_at=t.last_published_at,
    )
    return await offloaded_json_response(payload.model_dump(mode="json"))


# ── Public: methodology ─────────────────────────────────────────────────────


@public_router.get("/public/methodology", response_model=PublicMethodologyListResponse)
async def list_public_methodology(
    uow: UnitOfWork = Depends(get_uow, scope="function"),
    _user: CurrentUser = Depends(require_role(*EDITORIAL_ROLES)),
) -> Response:
    sections = await ListPublicMethodology(uow).execute()
    payload = PublicMethodologyListResponse(
        sections=[
            PublicMethodologySection(
                section=s.section,
                pages=[
                    PublicMethodologyPage(
                        slug=p.slug,
                        title=p.title,
                        section=p.section,
                        section_order=p.section_order,
                        body_markdown=p.body_markdown,
                        last_published_at=p.last_published_at,
                    )
                    for p in s.pages
                ],
            )
            for s in sections
        ]
    )
    return await offloaded_json_response(payload.model_dump(mode="json"))


@public_router.get("/public/methodology/{slug}", response_model=PublicMethodologyPage)
async def get_public_methodology_page(
    slug: str,
    uow: UnitOfWork = Depends(get_uow, scope="function"),
    _user: CurrentUser = Depends(require_role(*EDITORIAL_ROLES)),
) -> Response:
    p = await GetPublicMethodologyPage(uow).execute(slug)
    payload = PublicMethodologyPage(
        slug=p.slug,
        title=p.title,
        section=p.section,
        section_order=p.section_order,
        body_markdown=p.body_markdown,
        last_published_at=p.last_published_at,
    )
    return await offloaded_json_response(payload.model_dump(mode="json"))


# ── Public: changelog ───────────────────────────────────────────────────────


@public_router.get("/public/changelog", response_model=PublicChangelogListResponse)
async def list_public_changelog(
    limit: int = Query(default=25, ge=1, le=100),
    cursor: UUID | None = Query(default=None),
    uow: UnitOfWork = Depends(get_uow, scope="function"),
    _user: CurrentUser = Depends(require_role(*EDITORIAL_ROLES)),
) -> Response:
    result = await ListPublicChangelog(uow).execute(limit=limit, after_id=cursor)
    payload = PublicChangelogListResponse(
        items=[
            PublicChangelogEntry(
                slug=e.slug,
                title=e.title,
                effective_date=e.effective_date,
                body_markdown=e.body_markdown,
                last_published_at=e.last_published_at,
            )
            for e in result.items
        ],
        next_cursor=result.next_cursor,
    )
    return await offloaded_json_response(payload.model_dump(mode="json"))


@public_router.get("/public/changelog/{slug}", response_model=PublicChangelogEntry)
async def get_public_changelog_entry(
    slug: str,
    uow: UnitOfWork = Depends(get_uow, scope="function"),
    _user: CurrentUser = Depends(require_role(*EDITORIAL_ROLES)),
) -> Response:
    e = await GetPublicChangelogEntry(uow).execute(slug)
    payload = PublicChangelogEntry(
        slug=e.slug,
        title=e.title,
        effective_date=e.effective_date,
        body_markdown=e.body_markdown,
        last_published_at=e.last_published_at,
    )
    return await offloaded_json_response(payload.model_dump(mode="json"))


# ── Editorial helpers ────────────────────────────────────────────────────────


def _glossary_to_editorial(t) -> EditorialGlossaryTerm:
    return EditorialGlossaryTerm(
        id=t.id,
        term=t.term,
        display_term=t.display_term,
        body_markdown=t.body_markdown,
        status=t.status.value if hasattr(t.status, "value") else t.status,
        version=t.version,
        first_published_at=t.first_published_at,
        last_published_at=t.last_published_at,
        retraction_note=t.retraction_note,
        created_at=t.created_at,
        updated_at=t.updated_at,
    )


def _methodology_to_editorial(p) -> EditorialMethodologyPage:
    return EditorialMethodologyPage(
        id=p.id,
        slug=p.slug,
        title=p.title,
        section=p.section,
        section_order=p.section_order,
        body_markdown=p.body_markdown,
        status=p.status.value if hasattr(p.status, "value") else p.status,
        version=p.version,
        first_published_at=p.first_published_at,
        last_published_at=p.last_published_at,
        retraction_note=p.retraction_note,
        created_at=p.created_at,
        updated_at=p.updated_at,
    )


def _changelog_to_editorial(e) -> EditorialChangelogEntry:
    return EditorialChangelogEntry(
        id=e.id,
        slug=e.slug,
        title=e.title,
        effective_date=e.effective_date,
        body_markdown=e.body_markdown,
        status=e.status.value if hasattr(e.status, "value") else e.status,
        version=e.version,
        first_published_at=e.first_published_at,
        last_published_at=e.last_published_at,
        retraction_note=e.retraction_note,
        created_at=e.created_at,
        updated_at=e.updated_at,
    )


# ── Editorial: glossary ─────────────────────────────────────────────────────


@editorial_router.post("/editorial/glossary", response_model=EditorialGlossaryTerm, status_code=201)
async def create_glossary_term(
    request: CreateGlossaryTermRequest,
    uow: UnitOfWork = Depends(get_uow, scope="function"),
    user: CurrentUser = Depends(require_role(*EDITORIAL_ROLES)),
) -> Response:
    term = await CreateGlossaryTerm(uow).execute(
        CreateGlossaryTermInput(
            term=request.term,
            display_term=request.display_term,
            body_markdown=request.body_markdown,
            editor_user_id=user.user_id,
        )
    )
    return await offloaded_json_response(
        _glossary_to_editorial(term).model_dump(mode="json"), status_code=201
    )


@editorial_router.put("/editorial/glossary/{term_id}", response_model=EditorialGlossaryTerm)
async def update_glossary_term(
    term_id: UUID,
    request: UpdateGlossaryTermRequest,
    uow: UnitOfWork = Depends(get_uow, scope="function"),
    user: CurrentUser = Depends(require_role(*EDITORIAL_ROLES)),
) -> Response:
    term = await UpdateGlossaryTerm(uow).execute(
        UpdateGlossaryTermInput(
            term_id=term_id,
            expected_version=request.expected_version,
            display_term=request.display_term,
            body_markdown=request.body_markdown,
            editor_user_id=user.user_id,
        )
    )
    return await offloaded_json_response(_glossary_to_editorial(term).model_dump(mode="json"))


def _make_transition_route_glossary(use_case_cls: type, allowed_roles: tuple) -> Any:
    async def handler(
        term_id: UUID,
        request: TransitionRequest,
        uow: UnitOfWork = Depends(get_uow, scope="function"),
        user: CurrentUser = Depends(require_role(*allowed_roles)),
    ) -> Response:
        term = await use_case_cls(uow).execute(
            TransitionInput(
                entity_id=term_id,
                expected_version=request.expected_version,
                editor_user_id=user.user_id,
                transition_reason=request.transition_reason,
                retraction_note=request.retraction_note,
            )
        )
        return await offloaded_json_response(_glossary_to_editorial(term).model_dump(mode="json"))

    return handler


# Register all glossary transitions.
for _path_suffix, _use_case, _roles in [
    ("submit", SubmitGlossaryTerm, TRANSITION_ROLES),
    ("request-changes", RequestChangesGlossaryTerm, TRANSITION_ROLES),
    ("approve", ApproveGlossaryTerm, TRANSITION_ROLES),
    ("reject", RejectGlossaryTerm, TRANSITION_ROLES),
    ("publish", PublishGlossaryTerm, TRANSITION_ROLES),
    ("archive", ArchiveGlossaryTerm, TRANSITION_ROLES),
    ("reopen", ReopenGlossaryTerm, TRANSITION_ROLES),
    ("retract", RetractGlossaryTerm, RETRACT_ROLES),
]:
    editorial_router.add_api_route(
        f"/editorial/glossary/{{term_id}}/{_path_suffix}",
        _make_transition_route_glossary(_use_case, _roles),
        methods=["POST"],
        response_model=EditorialGlossaryTerm,
    )


# ── Editorial: methodology ──────────────────────────────────────────────────


@editorial_router.post(
    "/editorial/methodology",
    response_model=EditorialMethodologyPage,
    status_code=201,
)
async def create_methodology_page(
    request: CreateMethodologyPageRequest,
    uow: UnitOfWork = Depends(get_uow, scope="function"),
    user: CurrentUser = Depends(require_role(*EDITORIAL_ROLES)),
) -> Response:
    page = await CreateMethodologyPage(uow).execute(
        CreateMethodologyPageInput(
            slug=request.slug,
            title=request.title,
            section=request.section,
            section_order=request.section_order,
            body_markdown=request.body_markdown,
            editor_user_id=user.user_id,
        )
    )
    return await offloaded_json_response(
        _methodology_to_editorial(page).model_dump(mode="json"), status_code=201
    )


@editorial_router.put("/editorial/methodology/{page_id}", response_model=EditorialMethodologyPage)
async def update_methodology_page(
    page_id: UUID,
    request: UpdateMethodologyPageRequest,
    uow: UnitOfWork = Depends(get_uow, scope="function"),
    user: CurrentUser = Depends(require_role(*EDITORIAL_ROLES)),
) -> Response:
    page = await UpdateMethodologyPage(uow).execute(
        UpdateMethodologyPageInput(
            page_id=page_id,
            expected_version=request.expected_version,
            title=request.title,
            section=request.section,
            section_order=request.section_order,
            body_markdown=request.body_markdown,
            editor_user_id=user.user_id,
        )
    )
    return await offloaded_json_response(_methodology_to_editorial(page).model_dump(mode="json"))


def _make_transition_route_methodology(use_case_cls: type, allowed_roles: tuple) -> Any:
    async def handler(
        page_id: UUID,
        request: TransitionRequest,
        uow: UnitOfWork = Depends(get_uow, scope="function"),
        user: CurrentUser = Depends(require_role(*allowed_roles)),
    ) -> Response:
        page = await use_case_cls(uow).execute(
            TransitionInput(
                entity_id=page_id,
                expected_version=request.expected_version,
                editor_user_id=user.user_id,
                transition_reason=request.transition_reason,
                retraction_note=request.retraction_note,
            )
        )
        return await offloaded_json_response(
            _methodology_to_editorial(page).model_dump(mode="json")
        )

    return handler


for _path_suffix, _use_case, _roles in [
    ("submit", SubmitMethodologyPage, TRANSITION_ROLES),
    ("request-changes", RequestChangesMethodologyPage, TRANSITION_ROLES),
    ("approve", ApproveMethodologyPage, TRANSITION_ROLES),
    ("reject", RejectMethodologyPage, TRANSITION_ROLES),
    ("publish", PublishMethodologyPage, TRANSITION_ROLES),
    ("archive", ArchiveMethodologyPage, TRANSITION_ROLES),
    ("reopen", ReopenMethodologyPage, TRANSITION_ROLES),
    ("retract", RetractMethodologyPage, RETRACT_ROLES),
]:
    editorial_router.add_api_route(
        f"/editorial/methodology/{{page_id}}/{_path_suffix}",
        _make_transition_route_methodology(_use_case, _roles),
        methods=["POST"],
        response_model=EditorialMethodologyPage,
    )


# ── Editorial: changelog ────────────────────────────────────────────────────


@editorial_router.post(
    "/editorial/changelog",
    response_model=EditorialChangelogEntry,
    status_code=201,
)
async def create_changelog_entry(
    request: CreateChangelogEntryRequest,
    uow: UnitOfWork = Depends(get_uow, scope="function"),
    user: CurrentUser = Depends(require_role(*EDITORIAL_ROLES)),
) -> Response:
    entry = await CreateChangelogEntry(uow).execute(
        CreateChangelogEntryInput(
            slug=request.slug,
            title=request.title,
            effective_date=request.effective_date,
            body_markdown=request.body_markdown,
            editor_user_id=user.user_id,
        )
    )
    return await offloaded_json_response(
        _changelog_to_editorial(entry).model_dump(mode="json"), status_code=201
    )


@editorial_router.put("/editorial/changelog/{entry_id}", response_model=EditorialChangelogEntry)
async def update_changelog_entry(
    entry_id: UUID,
    request: UpdateChangelogEntryRequest,
    uow: UnitOfWork = Depends(get_uow, scope="function"),
    user: CurrentUser = Depends(require_role(*EDITORIAL_ROLES)),
) -> Response:
    entry = await UpdateChangelogEntry(uow).execute(
        UpdateChangelogEntryInput(
            entry_id=entry_id,
            expected_version=request.expected_version,
            title=request.title,
            effective_date=request.effective_date,
            body_markdown=request.body_markdown,
            editor_user_id=user.user_id,
        )
    )
    return await offloaded_json_response(_changelog_to_editorial(entry).model_dump(mode="json"))


def _make_transition_route_changelog(use_case_cls: type, allowed_roles: tuple) -> Any:
    async def handler(
        entry_id: UUID,
        request: TransitionRequest,
        uow: UnitOfWork = Depends(get_uow, scope="function"),
        user: CurrentUser = Depends(require_role(*allowed_roles)),
    ) -> Response:
        entry = await use_case_cls(uow).execute(
            TransitionInput(
                entity_id=entry_id,
                expected_version=request.expected_version,
                editor_user_id=user.user_id,
                transition_reason=request.transition_reason,
                retraction_note=request.retraction_note,
            )
        )
        return await offloaded_json_response(_changelog_to_editorial(entry).model_dump(mode="json"))

    return handler


for _path_suffix, _use_case, _roles in [
    ("submit", SubmitChangelogEntry, TRANSITION_ROLES),
    ("request-changes", RequestChangesChangelogEntry, TRANSITION_ROLES),
    ("approve", ApproveChangelogEntry, TRANSITION_ROLES),
    ("reject", RejectChangelogEntry, TRANSITION_ROLES),
    ("publish", PublishChangelogEntry, TRANSITION_ROLES),
    ("archive", ArchiveChangelogEntry, TRANSITION_ROLES),
    ("reopen", ReopenChangelogEntry, TRANSITION_ROLES),
    ("retract", RetractChangelogEntry, RETRACT_ROLES),
]:
    editorial_router.add_api_route(
        f"/editorial/changelog/{{entry_id}}/{_path_suffix}",
        _make_transition_route_changelog(_use_case, _roles),
        methods=["POST"],
        response_model=EditorialChangelogEntry,
    )
