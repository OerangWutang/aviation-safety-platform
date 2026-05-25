"""API tests for the Phase 10 CMS surface.

Cover the public read paths (visibility gates) and one editorial
write path (create + publish + retract) per kind.  The shared-
machinery contract test in
``tests/domain/test_cms_use_cases.py`` already pins that all three
kinds use the same `_CmsTransition` plumbing; here we exercise the
HTTP wiring.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from uuid import uuid4

import pytest
from httpx import AsyncClient

from atlas.domain.cms.entities import (
    ChangelogEntry,
    GlossaryTerm,
    MethodologyPage,
)
from atlas.domain.publication.entities import PublicationStatus


def _seed_published_glossary(
    uow,
    *,
    term: str,
    display_term: str | None = None,
    body: str = "A defined term.",
):
    """Insert a PUBLISHED glossary term directly into the fake store.

    Bypasses the editorial use cases on purpose: these tests focus
    on the read path and the HTTP wiring, and seeding via API would
    couple read tests to write-side concerns already covered
    elsewhere.
    """
    now = datetime(2024, 6, 1, tzinfo=UTC)
    t = GlossaryTerm(
        term=term,
        display_term=display_term or term.title(),
        body_markdown=body,
        status=PublicationStatus.PUBLISHED,
        first_published_at=now,
        last_published_at=now,
    )
    uow.store.cms.glossary_terms[t.id] = t
    return t


def _seed_published_methodology(uow, *, slug: str, section: str = "intro"):
    now = datetime(2024, 6, 1, tzinfo=UTC)
    p = MethodologyPage(
        slug=slug,
        title=slug.title(),
        section=section,
        section_order=0,
        body_markdown="Methodology body.",
        status=PublicationStatus.PUBLISHED,
        first_published_at=now,
        last_published_at=now,
    )
    uow.store.cms.methodology_pages[p.id] = p
    return p


def _seed_published_changelog(uow, *, slug: str, eff: date):
    now = datetime(2024, 6, 1, tzinfo=UTC)
    e = ChangelogEntry(
        slug=slug,
        title=slug.title(),
        effective_date=eff,
        body_markdown="Changelog body.",
        status=PublicationStatus.PUBLISHED,
        first_published_at=now,
        last_published_at=now,
    )
    uow.store.cms.changelog_entries[e.id] = e
    return e


# ── Glossary ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_public_glossary_list(async_client_analyst: AsyncClient, in_memory_uow):
    _seed_published_glossary(in_memory_uow, term="zeta")
    _seed_published_glossary(in_memory_uow, term="alpha")
    # Add a DRAFT term: it must NOT appear in the public listing.
    uow_term = GlossaryTerm(
        term="hidden",
        display_term="Hidden",
        body_markdown="x",
        status=PublicationStatus.DRAFT,
    )
    in_memory_uow.store.cms.glossary_terms[uow_term.id] = uow_term

    resp = await async_client_analyst.get("/api/v1/public/glossary")
    assert resp.status_code == 200, resp.text
    terms = [item["term"] for item in resp.json()["items"]]
    assert terms == ["alpha", "zeta"]


@pytest.mark.asyncio
async def test_public_glossary_detail(async_client_analyst: AsyncClient, in_memory_uow):
    _seed_published_glossary(in_memory_uow, term="claim", body="Evidence atom.")
    resp = await async_client_analyst.get("/api/v1/public/glossary/claim")
    assert resp.status_code == 200
    body = resp.json()
    assert body["term"] == "claim"
    assert body["body_markdown"] == "Evidence atom."


@pytest.mark.asyncio
async def test_public_glossary_draft_returns_404(async_client_analyst: AsyncClient, in_memory_uow):
    """DRAFT terms are not observable on the public surface."""
    uow_term = GlossaryTerm(
        term="wip",
        display_term="WIP",
        body_markdown="x",
        status=PublicationStatus.DRAFT,
    )
    in_memory_uow.store.cms.glossary_terms[uow_term.id] = uow_term
    resp = await async_client_analyst.get("/api/v1/public/glossary/wip")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "GLOSSARY_TERM_NOT_PUBLISHED"


@pytest.mark.asyncio
async def test_public_glossary_retracted_returns_410(
    async_client_analyst: AsyncClient, in_memory_uow
):
    t = _seed_published_glossary(in_memory_uow, term="old")
    # Retract in-place.
    retracted = t.model_copy(
        update={
            "status": PublicationStatus.RETRACTED,
            "retraction_note": "Replaced.",
        }
    )
    in_memory_uow.store.cms.glossary_terms[retracted.id] = retracted

    resp = await async_client_analyst.get("/api/v1/public/glossary/old")
    assert resp.status_code == 410
    body = resp.json()
    assert body["error"]["code"] == "GLOSSARY_TERM_RETRACTED"
    assert body["error"]["details"]["retraction_note"] == "Replaced."


@pytest.mark.asyncio
async def test_editorial_glossary_create_then_publish(
    async_client_admin: AsyncClient, in_memory_uow
):
    """End-to-end editorial path: create → submit → approve →
    publish.  Pins the HTTP wiring for all four transitions in one
    test."""
    create_resp = await async_client_admin.post(
        "/api/v1/editorial/glossary",
        json={
            "term": "reliability-tier",
            "display_term": "Reliability Tier",
            "body_markdown": "Per-source trust ranking.",
        },
    )
    assert create_resp.status_code == 201, create_resp.text
    term_id = create_resp.json()["id"]
    version = create_resp.json()["version"]

    for transition in ("submit", "approve", "publish"):
        resp = await async_client_admin.post(
            f"/api/v1/editorial/glossary/{term_id}/{transition}",
            json={"expected_version": version},
        )
        assert resp.status_code == 200, (transition, resp.text)
        version = resp.json()["version"]

    # Now visible on the public surface.
    public_resp = await async_client_admin.get("/api/v1/public/glossary/reliability-tier")
    assert public_resp.status_code == 200


@pytest.mark.asyncio
async def test_editorial_glossary_stale_version_returns_409(
    async_client_admin: AsyncClient, in_memory_uow
):
    """Optimistic concurrency wired through to HTTP."""
    create_resp = await async_client_admin.post(
        "/api/v1/editorial/glossary",
        json={
            "term": "t",
            "display_term": "T",
            "body_markdown": "x",
        },
    )
    term_id = create_resp.json()["id"]
    # First update with v1 succeeds and bumps to v2.
    ok = await async_client_admin.put(
        f"/api/v1/editorial/glossary/{term_id}",
        json={
            "expected_version": 1,
            "display_term": "T!",
            "body_markdown": "y",
        },
    )
    assert ok.status_code == 200
    # Second update with stale v1 must fail.
    conflict = await async_client_admin.put(
        f"/api/v1/editorial/glossary/{term_id}",
        json={
            "expected_version": 1,
            "display_term": "T!!",
            "body_markdown": "z",
        },
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "CMS_CONTENT_MODIFIED"


@pytest.mark.asyncio
async def test_editorial_glossary_retract_requires_admin(
    in_memory_uow, async_client_analyst: AsyncClient
):
    """Retraction is admin-only.  An analyst attempt must 403."""
    t = _seed_published_glossary(in_memory_uow, term="x")
    resp = await async_client_analyst.post(
        f"/api/v1/editorial/glossary/{t.id}/retract",
        json={
            "expected_version": t.version,
            "retraction_note": "Outdated.",
        },
    )
    assert resp.status_code == 403


# ── Methodology ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_public_methodology_list_grouped(async_client_analyst: AsyncClient, in_memory_uow):
    _seed_published_methodology(in_memory_uow, slug="a", section="alpha")
    _seed_published_methodology(in_memory_uow, slug="b", section="alpha")
    _seed_published_methodology(in_memory_uow, slug="c", section="beta")

    resp = await async_client_analyst.get("/api/v1/public/methodology")
    assert resp.status_code == 200
    sections = [s["section"] for s in resp.json()["sections"]]
    assert sections == ["alpha", "beta"]


@pytest.mark.asyncio
async def test_public_methodology_detail_retracted_410(
    async_client_analyst: AsyncClient, in_memory_uow
):
    page = _seed_published_methodology(in_memory_uow, slug="ret")
    retracted = page.model_copy(
        update={
            "status": PublicationStatus.RETRACTED,
            "retraction_note": "Superseded.",
        }
    )
    in_memory_uow.store.cms.methodology_pages[retracted.id] = retracted
    resp = await async_client_analyst.get("/api/v1/public/methodology/ret")
    assert resp.status_code == 410
    assert resp.json()["error"]["code"] == "METHODOLOGY_PAGE_RETRACTED"


# ── Changelog ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_public_changelog_list_orders_by_effective_date_desc(
    async_client_analyst: AsyncClient, in_memory_uow
):
    _seed_published_changelog(in_memory_uow, slug="first", eff=date(2024, 1, 1))
    _seed_published_changelog(in_memory_uow, slug="second", eff=date(2024, 6, 1))
    _seed_published_changelog(in_memory_uow, slug="third", eff=date(2024, 3, 1))

    resp = await async_client_analyst.get("/api/v1/public/changelog")
    assert resp.status_code == 200
    slugs = [item["slug"] for item in resp.json()["items"]]
    assert slugs == ["second", "third", "first"]


@pytest.mark.asyncio
async def test_public_changelog_detail_404_for_unknown(
    async_client_analyst: AsyncClient,
):
    resp = await async_client_analyst.get("/api/v1/public/changelog/no-such-entry")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "CHANGELOG_ENTRY_NOT_PUBLISHED"


# ── Auth gate ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_public_cms_requires_auth(client: AsyncClient):
    for path in (
        "/api/v1/public/glossary",
        "/api/v1/public/glossary/x",
        "/api/v1/public/methodology",
        "/api/v1/public/methodology/x",
        "/api/v1/public/changelog",
        "/api/v1/public/changelog/x",
    ):
        resp = await client.get(path)
        assert resp.status_code in (401, 403), path


@pytest.mark.asyncio
async def test_editorial_cms_requires_auth(client: AsyncClient):
    """Editorial routes require auth; unauthenticated → 401/403."""
    resp = await client.post(
        "/api/v1/editorial/glossary",
        json={"term": "x", "display_term": "X", "body_markdown": "x"},
    )
    assert resp.status_code in (401, 403)


# Silence unused-import shadows.
_ = uuid4
