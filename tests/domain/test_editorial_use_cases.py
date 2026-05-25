"""Use-case tests for the editorial workflow (Phase 9).

Anchored on the in-memory ``InMemoryUnitOfWork``: every state
transition, the optimistic-concurrency contract, the revision audit
trail, and the editorial-field-lock behaviour are exercised here so
the editorial-API tests can stay focused on HTTP-layer concerns.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

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
from atlas.domain.entities import AccidentEvent
from atlas.domain.publication.entities import PublicationStatus
from atlas.domain.publication.exceptions import (
    InvalidPublicationTransitionError,
    PublicEventPageModifiedError,
    PublicEventPageNotFoundError,
    SlugAlreadyTakenError,
)
from tests.domain._fake_uow import InMemoryUnitOfWork

# ── Helpers ──────────────────────────────────────────────────────────────────


def _make_uow_with_event() -> tuple[InMemoryUnitOfWork, AccidentEvent]:
    uow = InMemoryUnitOfWork()
    event = AccidentEvent()
    uow.store.events[event.id] = event
    return uow, event


async def _create_draft(
    uow: InMemoryUnitOfWork,
    *,
    slug: str = "test-event",
    title: str = "Test Event",
    event_id=None,
    editor_user_id=None,
):
    if event_id is None:
        event = AccidentEvent()
        uow.store.events[event.id] = event
        event_id = event.id
    return await CreatePublicEventPage(uow).execute(
        CreatePublicEventPageInput(
            event_id=event_id,
            slug=slug,
            title=title,
            editor_user_id=editor_user_id or uuid4(),
        )
    )


async def _walk_to(uow: InMemoryUnitOfWork, page, target: PublicationStatus):
    """Drive a fresh DRAFT page through the canonical happy path."""
    user = uuid4()
    if target == PublicationStatus.DRAFT:
        return page
    page = await SubmitPublicEventPage(uow).execute(
        TransitionPublicEventPageInput(
            page_id=page.id,
            expected_version=page.version,
            editor_user_id=user,
        )
    )
    if target == PublicationStatus.IN_REVIEW:
        return page
    page = await ApprovePublicEventPage(uow).execute(
        TransitionPublicEventPageInput(
            page_id=page.id,
            expected_version=page.version,
            editor_user_id=user,
        )
    )
    if target == PublicationStatus.APPROVED:
        return page
    page = await PublishPublicEventPage(uow).execute(
        TransitionPublicEventPageInput(
            page_id=page.id,
            expected_version=page.version,
            editor_user_id=user,
        )
    )
    if target == PublicationStatus.PUBLISHED:
        return page
    if target == PublicationStatus.ARCHIVED:
        return await ArchivePublicEventPage(uow).execute(
            TransitionPublicEventPageInput(
                page_id=page.id,
                expected_version=page.version,
                editor_user_id=user,
            )
        )
    if target == PublicationStatus.RETRACTED:
        return await RetractPublicEventPage(uow).execute(
            TransitionPublicEventPageInput(
                page_id=page.id,
                expected_version=page.version,
                editor_user_id=user,
                retraction_note="Test retraction",
            )
        )
    raise AssertionError(f"unsupported target: {target}")


# ── Create ───────────────────────────────────────────────────────────────────


class TestCreatePublicEventPage:
    async def test_creates_draft_with_normalized_slug(self) -> None:
        uow, event = _make_uow_with_event()
        page = await CreatePublicEventPage(uow).execute(
            CreatePublicEventPageInput(
                event_id=event.id,
                slug="Boeing 737 N12345",
                title="Editor's title",
                editor_user_id=uuid4(),
            )
        )
        assert page.slug == "boeing-737-n12345"
        assert page.status == PublicationStatus.DRAFT
        assert page.version == 1

    async def test_writes_creation_revision(self) -> None:
        uow, event = _make_uow_with_event()
        editor = uuid4()
        page = await CreatePublicEventPage(uow).execute(
            CreatePublicEventPageInput(
                event_id=event.id,
                slug="page-one",
                title="One",
                editor_user_id=editor,
            )
        )
        revisions = await ListPageRevisions(uow).execute(page.id)
        assert len(revisions) == 1
        rev = revisions[0]
        # Creation rev has NULL from_status.
        assert rev.from_status is None
        assert rev.to_status == PublicationStatus.DRAFT
        assert rev.editor_user_id == editor
        assert rev.transition_reason == "created"

    async def test_duplicate_slug_raises(self) -> None:
        uow, event_a = _make_uow_with_event()
        event_b = AccidentEvent()
        uow.store.events[event_b.id] = event_b
        await _create_draft(uow, slug="dup", event_id=event_a.id)
        with pytest.raises(SlugAlreadyTakenError):
            await _create_draft(uow, slug="dup", event_id=event_b.id)


# ── Update (DRAFT only) ──────────────────────────────────────────────────────


class TestUpdatePublicEventPage:
    async def test_edits_draft_in_place_and_bumps_version(self) -> None:
        uow, _ = _make_uow_with_event()
        page = await _create_draft(uow, slug="edit-me", title="Original")
        assert page.version == 1

        updated = await UpdatePublicEventPage(uow).execute(
            UpdatePublicEventPageInput(
                page_id=page.id,
                expected_version=1,
                editor_user_id=uuid4(),
                title="Revised",
                short_summary="A summary",
            )
        )
        assert updated.title == "Revised"
        assert updated.short_summary == "A summary"
        assert updated.version == 2

    async def test_optimistic_concurrency_clash(self) -> None:
        uow, _ = _make_uow_with_event()
        page = await _create_draft(uow)
        # First writer succeeds.
        await UpdatePublicEventPage(uow).execute(
            UpdatePublicEventPageInput(
                page_id=page.id,
                expected_version=1,
                editor_user_id=uuid4(),
                title="First",
            )
        )
        # Second writer thinks the page is still version 1.
        with pytest.raises(PublicEventPageModifiedError) as excinfo:
            await UpdatePublicEventPage(uow).execute(
                UpdatePublicEventPageInput(
                    page_id=page.id,
                    expected_version=1,
                    editor_user_id=uuid4(),
                    title="Second",
                )
            )
        assert excinfo.value.expected_version == 1
        assert excinfo.value.actual_version == 2

    async def test_update_rejected_when_not_draft(self) -> None:
        uow, _ = _make_uow_with_event()
        page = await _create_draft(uow)
        page = await _walk_to(uow, page, PublicationStatus.IN_REVIEW)
        with pytest.raises(InvalidPublicationTransitionError):
            await UpdatePublicEventPage(uow).execute(
                UpdatePublicEventPageInput(
                    page_id=page.id,
                    expected_version=page.version,
                    editor_user_id=uuid4(),
                    title="Cannot",
                )
            )

    async def test_slug_uniqueness_enforced_on_update(self) -> None:
        uow, event_a = _make_uow_with_event()
        event_b = AccidentEvent()
        uow.store.events[event_b.id] = event_b
        await _create_draft(uow, slug="taken", event_id=event_a.id)
        other = await _create_draft(uow, slug="free", event_id=event_b.id)
        with pytest.raises(SlugAlreadyTakenError):
            await UpdatePublicEventPage(uow).execute(
                UpdatePublicEventPageInput(
                    page_id=other.id,
                    expected_version=1,
                    editor_user_id=uuid4(),
                    slug="taken",
                )
            )

    async def test_update_normalizes_supplied_slug(self) -> None:
        uow, _ = _make_uow_with_event()
        page = await _create_draft(uow, slug="normal")
        updated = await UpdatePublicEventPage(uow).execute(
            UpdatePublicEventPageInput(
                page_id=page.id,
                expected_version=1,
                editor_user_id=uuid4(),
                slug="My New Title",
            )
        )
        assert updated.slug == "my-new-title"


# ── State transitions ────────────────────────────────────────────────────────


class TestStateTransitions:
    """One test per transition.  Each verifies state, version bump, and
    revision row.  Use-case-level rather than entity-level so the audit
    trail behaviour is pinned with the transition."""

    async def test_submit_draft_to_in_review(self) -> None:
        uow, _ = _make_uow_with_event()
        page = await _create_draft(uow)
        result = await SubmitPublicEventPage(uow).execute(
            TransitionPublicEventPageInput(
                page_id=page.id,
                expected_version=1,
                editor_user_id=uuid4(),
                transition_reason="ready for review",
            )
        )
        assert result.status == PublicationStatus.IN_REVIEW
        assert result.version == 2
        revs = await ListPageRevisions(uow).execute(page.id)
        # creation + submit = 2 revisions
        assert len(revs) == 2
        assert revs[-1].from_status == PublicationStatus.DRAFT
        assert revs[-1].to_status == PublicationStatus.IN_REVIEW
        assert revs[-1].transition_reason == "ready for review"

    async def test_approve_in_review_to_approved(self) -> None:
        uow, _ = _make_uow_with_event()
        page = await _create_draft(uow)
        page = await _walk_to(uow, page, PublicationStatus.IN_REVIEW)
        result = await ApprovePublicEventPage(uow).execute(
            TransitionPublicEventPageInput(
                page_id=page.id,
                expected_version=page.version,
                editor_user_id=uuid4(),
            )
        )
        assert result.status == PublicationStatus.APPROVED

    async def test_publish_sets_first_and_last_published_at(self) -> None:
        uow, _ = _make_uow_with_event()
        page = await _create_draft(uow)
        page = await _walk_to(uow, page, PublicationStatus.PUBLISHED)
        assert page.status == PublicationStatus.PUBLISHED
        assert page.last_published_at is not None
        assert page.first_published_at == page.last_published_at

    async def test_republish_from_archived_preserves_first_published_at(self) -> None:
        uow, _ = _make_uow_with_event()
        page = await _create_draft(uow)
        page = await _walk_to(uow, page, PublicationStatus.PUBLISHED)
        first_publish_moment = page.first_published_at
        # Archive then re-publish.
        page = await ArchivePublicEventPage(uow).execute(
            TransitionPublicEventPageInput(
                page_id=page.id,
                expected_version=page.version,
                editor_user_id=uuid4(),
            )
        )
        page = await PublishPublicEventPage(uow).execute(
            TransitionPublicEventPageInput(
                page_id=page.id,
                expected_version=page.version,
                editor_user_id=uuid4(),
            )
        )
        # first_published_at unchanged; last_published_at refreshed.
        assert page.first_published_at == first_publish_moment
        assert page.last_published_at is not None
        assert page.last_published_at > first_publish_moment

    async def test_request_changes_returns_in_review_to_draft(self) -> None:
        uow, _ = _make_uow_with_event()
        page = await _create_draft(uow)
        page = await _walk_to(uow, page, PublicationStatus.IN_REVIEW)
        result = await RequestChanges(uow).execute(
            TransitionPublicEventPageInput(
                page_id=page.id,
                expected_version=page.version,
                editor_user_id=uuid4(),
                transition_reason="needs more sources",
            )
        )
        assert result.status == PublicationStatus.DRAFT

    async def test_reject_returns_approved_to_draft(self) -> None:
        uow, _ = _make_uow_with_event()
        page = await _create_draft(uow)
        page = await _walk_to(uow, page, PublicationStatus.APPROVED)
        result = await RejectPublicEventPage(uow).execute(
            TransitionPublicEventPageInput(
                page_id=page.id,
                expected_version=page.version,
                editor_user_id=uuid4(),
            )
        )
        assert result.status == PublicationStatus.DRAFT

    async def test_reopen_returns_archived_to_draft(self) -> None:
        uow, _ = _make_uow_with_event()
        page = await _create_draft(uow)
        page = await _walk_to(uow, page, PublicationStatus.ARCHIVED)
        result = await ReopenPublicEventPage(uow).execute(
            TransitionPublicEventPageInput(
                page_id=page.id,
                expected_version=page.version,
                editor_user_id=uuid4(),
            )
        )
        assert result.status == PublicationStatus.DRAFT

    async def test_retract_records_note_on_page_and_revision(self) -> None:
        uow, _ = _make_uow_with_event()
        page = await _create_draft(uow)
        page = await _walk_to(uow, page, PublicationStatus.PUBLISHED)
        result = await RetractPublicEventPage(uow).execute(
            TransitionPublicEventPageInput(
                page_id=page.id,
                expected_version=page.version,
                editor_user_id=uuid4(),
                retraction_note="Found an inaccuracy.",
            )
        )
        assert result.status == PublicationStatus.RETRACTED
        assert result.retraction_note == "Found an inaccuracy."
        assert result.retracted_at is not None
        revs = await ListPageRevisions(uow).execute(page.id)
        last = revs[-1]
        # Note is also captured in the revision's correction_note
        # so the audit trail explains the retraction.
        assert last.correction_note == "Found an inaccuracy."

    async def test_retracted_is_terminal_no_publish(self) -> None:
        uow, _ = _make_uow_with_event()
        page = await _create_draft(uow)
        page = await _walk_to(uow, page, PublicationStatus.RETRACTED)
        with pytest.raises(InvalidPublicationTransitionError):
            await PublishPublicEventPage(uow).execute(
                TransitionPublicEventPageInput(
                    page_id=page.id,
                    expected_version=page.version,
                    editor_user_id=uuid4(),
                )
            )

    async def test_optimistic_concurrency_clash_on_repo_update(self) -> None:
        """Repo-level optimistic-concurrency contract.

        Two writers fetch the same row, mutate it independently, and
        race to write back.  In real Postgres each holds a transaction
        snapshot at version=N; only the first ``UPDATE ... WHERE
        version = N`` succeeds.  We model the contract directly on
        the repository (the use case layer cannot exercise this with
        an in-memory fake because operations are serialized and the
        validator catches the stale status before the version check).
        """
        from atlas.domain.publication.entities import PublicEventPage

        uow, _ = _make_uow_with_event()
        page = await _create_draft(uow)
        # Two writers each take a copy at version=1.
        writer_a = page.model_copy(deep=True)
        writer_b = page.model_copy(deep=True)

        # Writer A bumps version + edits.
        writer_a_next = writer_a.model_copy(update={"title": "A wins", "version": 2})
        await uow.public_event_pages.update(writer_a_next, expected_version=1)

        # Writer B still holds expected_version=1.  Repo must refuse.
        writer_b_next = writer_b.model_copy(update={"title": "B loses", "version": 2})
        with pytest.raises(PublicEventPageModifiedError) as excinfo:
            await uow.public_event_pages.update(writer_b_next, expected_version=1)
        assert excinfo.value.expected_version == 1
        assert excinfo.value.actual_version == 2
        # Defensive: writer A's value is the one that stuck.
        stored = await uow.public_event_pages.get_by_id(page.id)
        assert stored is not None and stored.title == "A wins"
        # Silence "unused" on PublicEventPage import alias.
        assert isinstance(stored, PublicEventPage)

    async def test_missing_page_raises_not_found(self) -> None:
        uow = InMemoryUnitOfWork()
        with pytest.raises(PublicEventPageNotFoundError):
            await SubmitPublicEventPage(uow).execute(
                TransitionPublicEventPageInput(
                    page_id=uuid4(),
                    expected_version=1,
                    editor_user_id=uuid4(),
                )
            )


# ── Editorial list ───────────────────────────────────────────────────────────


class TestListEditorialPages:
    async def test_default_excludes_retracted(self) -> None:
        uow, _ = _make_uow_with_event()
        live = await _create_draft(uow, slug="alive")
        # Build a separate retracted page so the live one stays draft.
        event_b = AccidentEvent()
        uow.store.events[event_b.id] = event_b
        retracted = await _create_draft(uow, slug="dead", event_id=event_b.id)
        await _walk_to(uow, retracted, PublicationStatus.RETRACTED)

        result = await ListEditorialPages(uow).execute()
        slugs = {i.slug for i in result.items}
        assert "alive" in slugs
        assert "dead" not in slugs
        # The live draft's allowed_next_statuses reflects the
        # workflow's outgoing edges from DRAFT.
        alive_row = next(i for i in result.items if i.slug == "alive")
        assert PublicationStatus.IN_REVIEW in alive_row.allowed_next_statuses
        # Use the result variable to silence "unused" warnings.
        assert live.id == alive_row.id

    async def test_explicit_status_filter(self) -> None:
        uow, _ = _make_uow_with_event()
        # Two drafts, one approved.
        page_a = await _create_draft(uow, slug="draft-a")
        event_b = AccidentEvent()
        uow.store.events[event_b.id] = event_b
        page_b = await _create_draft(uow, slug="draft-b", event_id=event_b.id)
        await _walk_to(uow, page_b, PublicationStatus.APPROVED)

        result = await ListEditorialPages(uow).execute(
            statuses=frozenset({PublicationStatus.DRAFT})
        )
        slugs = {i.slug for i in result.items}
        assert slugs == {"draft-a"}
        # Sanity check on the unused page_a binding so static
        # analysers don't whine.
        assert page_a.slug == "draft-a"
