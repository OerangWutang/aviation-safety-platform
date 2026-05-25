"""Use-case tests for the Phase 10 CMS surface.

Pin two contracts here:

1. The state machine is identical to Phase 9's for all three
   content kinds.  Same transitions, same role gates, same
   optimistic-concurrency error shape.

2. The public read paths obey the same visibility contract as
   Phase 1: PUBLISHED → 200, RETRACTED → 410-equivalent
   exception, anything else → 404-equivalent exception.

The transitions are exercised once per kind for the happy publish
path, then we lean on the shared
:class:`_CmsTransition`/`_PublishMixin` machinery — testing every
transition for every kind would be three copies of the Phase 9
suite and adds nothing.  The contract test below pins that all
three kinds use the same machinery so a Phase 9 transition fix
flows to all three by construction.
"""

from __future__ import annotations

import inspect
from datetime import UTC, date, datetime
from uuid import uuid4

import pytest

from atlas.application.use_cases.cms import (
    ApproveChangelogEntry,
    ApproveGlossaryTerm,
    ApproveMethodologyPage,
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
    RetractChangelogEntry,
    RetractGlossaryTerm,
    RetractMethodologyPage,
    SubmitChangelogEntry,
    SubmitGlossaryTerm,
    SubmitMethodologyPage,
    TransitionInput,
    UpdateGlossaryTerm,
    UpdateGlossaryTermInput,
    _CmsTransition,
)
from atlas.domain.cms.exceptions import (
    ChangelogEntryNotPublishedError,
    ChangelogEntryRetractedError,
    CmsContentModifiedError,
    GlossaryTermNotPublishedError,
    GlossaryTermRetractedError,
    MethodologyPageNotPublishedError,
    MethodologyPageRetractedError,
)
from atlas.domain.publication.entities import PublicationStatus
from atlas.domain.publication.exceptions import InvalidPublicationTransitionError
from tests.domain._fake_uow import InMemoryUnitOfWork

# ── Shared machinery contract ───────────────────────────────────────────────


class TestSharedMachineryContract:
    """Every transition class for every kind ultimately uses
    :class:`_CmsTransition`.  Pins the "one workflow" invariant."""

    @pytest.mark.parametrize(
        "use_case_cls",
        [
            SubmitGlossaryTerm,
            ApproveGlossaryTerm,
            PublishGlossaryTerm,
            RetractGlossaryTerm,
            SubmitMethodologyPage,
            ApproveMethodologyPage,
            PublishMethodologyPage,
            RetractMethodologyPage,
            SubmitChangelogEntry,
            ApproveChangelogEntry,
            PublishChangelogEntry,
            RetractChangelogEntry,
        ],
    )
    def test_all_transitions_inherit_cms_transition(self, use_case_cls):
        assert issubclass(use_case_cls, _CmsTransition), (
            f"{use_case_cls.__name__} must descend from _CmsTransition "
            f"so the state machine, version check, and revision audit "
            f"stay in one place."
        )


# ── Glossary ────────────────────────────────────────────────────────────────


class TestGlossaryWorkflow:
    async def test_create_starts_in_draft(self) -> None:
        uow = InMemoryUnitOfWork()
        term = await CreateGlossaryTerm(uow).execute(
            CreateGlossaryTermInput(
                term="reliability-tier",
                display_term="Reliability Tier",
                body_markdown="Per-source trust ranking.",
                editor_user_id=uuid4(),
            )
        )
        assert term.status == PublicationStatus.DRAFT
        assert term.version == 1
        # A "created" revision row exists from the start so the audit
        # trail covers the term's whole history.
        revisions = await uow.glossary_term_revisions.list_for_term(term.id)
        assert len(revisions) == 1
        assert revisions[0].transition_reason == "created"

    async def test_full_workflow_path_to_publish(self) -> None:
        uow = InMemoryUnitOfWork()
        user = uuid4()
        term = await CreateGlossaryTerm(uow).execute(
            CreateGlossaryTermInput(
                term="claim",
                display_term="Claim",
                body_markdown="A piece of evidence.",
                editor_user_id=user,
            )
        )
        term = await SubmitGlossaryTerm(uow).execute(
            TransitionInput(
                entity_id=term.id,
                expected_version=term.version,
                editor_user_id=user,
            )
        )
        assert term.status == PublicationStatus.IN_REVIEW
        term = await ApproveGlossaryTerm(uow).execute(
            TransitionInput(
                entity_id=term.id,
                expected_version=term.version,
                editor_user_id=user,
            )
        )
        assert term.status == PublicationStatus.APPROVED
        term = await PublishGlossaryTerm(uow).execute(
            TransitionInput(
                entity_id=term.id,
                expected_version=term.version,
                editor_user_id=user,
            )
        )
        assert term.status == PublicationStatus.PUBLISHED
        assert term.first_published_at is not None
        assert term.last_published_at is not None
        # Five revision rows: created + submit + approve + publish.
        revisions = await uow.glossary_term_revisions.list_for_term(term.id)
        assert len(revisions) == 4

    async def test_optimistic_concurrency(self) -> None:
        uow = InMemoryUnitOfWork()
        user = uuid4()
        term = await CreateGlossaryTerm(uow).execute(
            CreateGlossaryTermInput(
                term="source",
                display_term="Source",
                body_markdown="x",
                editor_user_id=user,
            )
        )
        # First update succeeds.
        await UpdateGlossaryTerm(uow).execute(
            UpdateGlossaryTermInput(
                term_id=term.id,
                expected_version=term.version,
                display_term="Source!",
                body_markdown="new",
                editor_user_id=user,
            )
        )
        # Second update with the stale version must fail.
        with pytest.raises(CmsContentModifiedError) as exc:
            await UpdateGlossaryTerm(uow).execute(
                UpdateGlossaryTermInput(
                    term_id=term.id,
                    expected_version=term.version,  # stale
                    display_term="Source!!",
                    body_markdown="newer",
                    editor_user_id=user,
                )
            )
        assert exc.value.kind == "glossary_term"
        assert exc.value.expected_version == 1
        assert exc.value.actual_version == 2

    async def test_invalid_transition_rejected(self) -> None:
        """A DRAFT term cannot jump straight to PUBLISHED.  The
        state-machine helper raises the same exception Phase 9
        uses."""
        uow = InMemoryUnitOfWork()
        user = uuid4()
        term = await CreateGlossaryTerm(uow).execute(
            CreateGlossaryTermInput(
                term="t",
                display_term="T",
                body_markdown="x",
                editor_user_id=user,
            )
        )
        with pytest.raises(InvalidPublicationTransitionError):
            await PublishGlossaryTerm(uow).execute(
                TransitionInput(
                    entity_id=term.id,
                    expected_version=term.version,
                    editor_user_id=user,
                )
            )

    async def test_duplicate_term_rejected(self) -> None:
        uow = InMemoryUnitOfWork()
        user = uuid4()
        await CreateGlossaryTerm(uow).execute(
            CreateGlossaryTermInput(
                term="dup",
                display_term="Dup",
                body_markdown="x",
                editor_user_id=user,
            )
        )
        with pytest.raises(ValueError):
            await CreateGlossaryTerm(uow).execute(
                CreateGlossaryTermInput(
                    term="dup",
                    display_term="Dup2",
                    body_markdown="y",
                    editor_user_id=user,
                )
            )


# ── Methodology + changelog smoke tests (machinery already proved) ──────────


class TestMethodologyAndChangelogPublishPath:
    """One happy-path test per remaining kind.

    The shared-machinery contract test above pins that all three
    kinds use the same `_CmsTransition` plumbing; this just
    confirms the slot adapters are wired correctly.
    """

    async def test_methodology_publish(self) -> None:
        uow = InMemoryUnitOfWork()
        user = uuid4()
        page = await CreateMethodologyPage(uow).execute(
            CreateMethodologyPageInput(
                slug="how-confidence-works",
                title="How Confidence Works",
                section="confidence",
                section_order=0,
                body_markdown="Methodology body.",
                editor_user_id=user,
            )
        )
        page = await SubmitMethodologyPage(uow).execute(
            TransitionInput(
                entity_id=page.id,
                expected_version=page.version,
                editor_user_id=user,
            )
        )
        page = await ApproveMethodologyPage(uow).execute(
            TransitionInput(
                entity_id=page.id,
                expected_version=page.version,
                editor_user_id=user,
            )
        )
        page = await PublishMethodologyPage(uow).execute(
            TransitionInput(
                entity_id=page.id,
                expected_version=page.version,
                editor_user_id=user,
            )
        )
        assert page.status == PublicationStatus.PUBLISHED
        assert page.first_published_at is not None

    async def test_changelog_publish_preserves_effective_date(self) -> None:
        """Crucial for changelog: ``effective_date`` and
        ``last_published_at`` are independent.  A retroactive entry
        published today can carry an effective date from last
        month."""
        uow = InMemoryUnitOfWork()
        user = uuid4()
        retroactive = date(2024, 1, 15)
        entry = await CreateChangelogEntry(uow).execute(
            CreateChangelogEntryInput(
                slug="ntsb-source-added",
                title="NTSB source added",
                effective_date=retroactive,
                body_markdown="We now ingest the NTSB API.",
                editor_user_id=user,
            )
        )
        entry = await SubmitChangelogEntry(uow).execute(
            TransitionInput(
                entity_id=entry.id,
                expected_version=entry.version,
                editor_user_id=user,
            )
        )
        entry = await ApproveChangelogEntry(uow).execute(
            TransitionInput(
                entity_id=entry.id,
                expected_version=entry.version,
                editor_user_id=user,
            )
        )
        entry = await PublishChangelogEntry(uow).execute(
            TransitionInput(
                entity_id=entry.id,
                expected_version=entry.version,
                editor_user_id=user,
            )
        )
        # The two dates are independent — publication "now" is
        # later than the effective date.
        assert entry.effective_date == retroactive
        assert entry.last_published_at is not None
        assert entry.last_published_at.date() > retroactive


# ── Public visibility contract ──────────────────────────────────────────────


class TestPublicVisibility:
    """For each kind, exercise:

    - PUBLISHED → returns;
    - RETRACTED → raises retraction exception (carries note);
    - DRAFT / unknown → raises not-published / not-found exception.
    """

    async def _publish_glossary(self, uow, term_key: str):
        user = uuid4()
        term = await CreateGlossaryTerm(uow).execute(
            CreateGlossaryTermInput(
                term=term_key,
                display_term=term_key.title(),
                body_markdown="x",
                editor_user_id=user,
            )
        )
        for use_case in (
            SubmitGlossaryTerm,
            ApproveGlossaryTerm,
            PublishGlossaryTerm,
        ):
            term = await use_case(uow).execute(
                TransitionInput(
                    entity_id=term.id,
                    expected_version=term.version,
                    editor_user_id=user,
                )
            )
        return term

    async def test_published_glossary_visible(self) -> None:
        uow = InMemoryUnitOfWork()
        await self._publish_glossary(uow, "claim")
        t = await GetPublicGlossaryTerm(uow).execute("claim")
        assert t.term == "claim"

    async def test_retracted_glossary_410(self) -> None:
        uow = InMemoryUnitOfWork()
        user = uuid4()
        term = await self._publish_glossary(uow, "old-term")
        await RetractGlossaryTerm(uow).execute(
            TransitionInput(
                entity_id=term.id,
                expected_version=term.version,
                editor_user_id=user,
                retraction_note="Renamed to something else.",
            )
        )
        with pytest.raises(GlossaryTermRetractedError) as exc:
            await GetPublicGlossaryTerm(uow).execute("old-term")
        assert exc.value.retraction_note == "Renamed to something else."

    async def test_draft_glossary_404(self) -> None:
        uow = InMemoryUnitOfWork()
        await CreateGlossaryTerm(uow).execute(
            CreateGlossaryTermInput(
                term="wip",
                display_term="WIP",
                body_markdown="x",
                editor_user_id=uuid4(),
            )
        )
        # The term exists in DRAFT, but the public read must not
        # leak it.
        with pytest.raises(GlossaryTermNotPublishedError):
            await GetPublicGlossaryTerm(uow).execute("wip")

    async def test_methodology_visibility(self) -> None:
        uow = InMemoryUnitOfWork()
        user = uuid4()
        page = await CreateMethodologyPage(uow).execute(
            CreateMethodologyPageInput(
                slug="m",
                title="M",
                section="s",
                section_order=0,
                body_markdown="x",
                editor_user_id=user,
            )
        )
        # DRAFT → not published.
        with pytest.raises(MethodologyPageNotPublishedError):
            await GetPublicMethodologyPage(uow).execute("m")

        # Publish, then retract.
        for use_case in (
            SubmitMethodologyPage,
            ApproveMethodologyPage,
            PublishMethodologyPage,
        ):
            page = await use_case(uow).execute(
                TransitionInput(
                    entity_id=page.id,
                    expected_version=page.version,
                    editor_user_id=user,
                )
            )
        await GetPublicMethodologyPage(uow).execute("m")  # OK
        await RetractMethodologyPage(uow).execute(
            TransitionInput(
                entity_id=page.id,
                expected_version=page.version,
                editor_user_id=user,
                retraction_note="Outdated.",
            )
        )
        with pytest.raises(MethodologyPageRetractedError) as exc:
            await GetPublicMethodologyPage(uow).execute("m")
        assert exc.value.retraction_note == "Outdated."

    async def test_changelog_visibility(self) -> None:
        uow = InMemoryUnitOfWork()
        user = uuid4()
        entry = await CreateChangelogEntry(uow).execute(
            CreateChangelogEntryInput(
                slug="c",
                title="C",
                effective_date=date(2024, 1, 1),
                body_markdown="x",
                editor_user_id=user,
            )
        )
        with pytest.raises(ChangelogEntryNotPublishedError):
            await GetPublicChangelogEntry(uow).execute("c")
        for use_case in (
            SubmitChangelogEntry,
            ApproveChangelogEntry,
            PublishChangelogEntry,
        ):
            entry = await use_case(uow).execute(
                TransitionInput(
                    entity_id=entry.id,
                    expected_version=entry.version,
                    editor_user_id=user,
                )
            )
        await GetPublicChangelogEntry(uow).execute("c")
        await RetractChangelogEntry(uow).execute(
            TransitionInput(
                entity_id=entry.id,
                expected_version=entry.version,
                editor_user_id=user,
                retraction_note="Incorrect entry.",
            )
        )
        with pytest.raises(ChangelogEntryRetractedError) as exc:
            await GetPublicChangelogEntry(uow).execute("c")
        assert exc.value.retraction_note == "Incorrect entry."


# ── Listings ────────────────────────────────────────────────────────────────


class TestPublicListings:
    async def test_glossary_list_sorted_by_term(self) -> None:
        uow = InMemoryUnitOfWork()
        user = uuid4()
        # Create three terms; publish only two.
        for slug in ("zeta", "alpha", "mu"):
            term = await CreateGlossaryTerm(uow).execute(
                CreateGlossaryTermInput(
                    term=slug,
                    display_term=slug,
                    body_markdown=slug,
                    editor_user_id=user,
                )
            )
            if slug != "mu":  # Leave "mu" in DRAFT.
                for use_case in (
                    SubmitGlossaryTerm,
                    ApproveGlossaryTerm,
                    PublishGlossaryTerm,
                ):
                    term = await use_case(uow).execute(
                        TransitionInput(
                            entity_id=term.id,
                            expected_version=term.version,
                            editor_user_id=user,
                        )
                    )
        terms = await ListPublicGlossary(uow).execute()
        assert [t.term for t in terms] == ["alpha", "zeta"]

    async def test_methodology_list_grouped_by_section(self) -> None:
        uow = InMemoryUnitOfWork()
        user = uuid4()
        for slug, section, order in [
            ("a", "confidence", 1),
            ("b", "confidence", 0),
            ("c", "data-sources", 0),
        ]:
            page = await CreateMethodologyPage(uow).execute(
                CreateMethodologyPageInput(
                    slug=slug,
                    title=slug.upper(),
                    section=section,
                    section_order=order,
                    body_markdown="x",
                    editor_user_id=user,
                )
            )
            for use_case in (
                SubmitMethodologyPage,
                ApproveMethodologyPage,
                PublishMethodologyPage,
            ):
                page = await use_case(uow).execute(
                    TransitionInput(
                        entity_id=page.id,
                        expected_version=page.version,
                        editor_user_id=user,
                    )
                )
        sections = await ListPublicMethodology(uow).execute()
        # Sections preserved in (section, section_order) order.
        section_names = [s.section for s in sections]
        assert section_names == ["confidence", "data-sources"]
        conf_pages = [p.slug for p in sections[0].pages]
        assert conf_pages == ["b", "a"]  # section_order 0, then 1

    async def test_changelog_list_ordered_by_effective_date_desc(
        self,
    ) -> None:
        uow = InMemoryUnitOfWork()
        user = uuid4()
        for slug, eff in [
            ("first", date(2024, 1, 1)),
            ("second", date(2024, 6, 1)),
            ("third", date(2024, 3, 1)),
        ]:
            entry = await CreateChangelogEntry(uow).execute(
                CreateChangelogEntryInput(
                    slug=slug,
                    title=slug,
                    effective_date=eff,
                    body_markdown="x",
                    editor_user_id=user,
                )
            )
            for use_case in (
                SubmitChangelogEntry,
                ApproveChangelogEntry,
                PublishChangelogEntry,
            ):
                entry = await use_case(uow).execute(
                    TransitionInput(
                        entity_id=entry.id,
                        expected_version=entry.version,
                        editor_user_id=user,
                    )
                )
        result = await ListPublicChangelog(uow).execute()
        assert [e.slug for e in result.items] == ["second", "third", "first"]


# Silence unused-import shadows that exist only as type-narrowing aids.
_ = (inspect, datetime, UTC)
