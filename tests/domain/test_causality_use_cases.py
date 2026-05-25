"""Use-case tests for Phase 4 causality.

Pins:

1. **Visibility inheritance** — HFACS and SHELO reads return PUBLISHED
   data, raise the Phase 1 retracted error on RETRACTED, and the
   Phase 1 not-published error on DRAFT/IN_REVIEW/APPROVED/ARCHIVED.

2. **HFACS attribution natural key** — duplicates rejected at the
   repo (translates to 409 at the router); same-category-same-
   subcategory uniqueness honoured including the NULL-subcategory
   case.

3. **Optimistic concurrency** — update with stale ``expected_version``
   fails with the conflict exception.

4. **SHELO interactions** — self-loop rejected with typed error;
   cross-event factors rejected; duplicate natural key rejected.

5. **Deletes are idempotent** — deleting a non-existent attribution
   or factor is a no-op so editorial UIs can retry safely.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

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
    HfacsCategory,
    HfacsSubcategory,
    HfacsTier,
    SheloClass,
    SheloFactorInteraction,
    SheloInteractionKind,
)
from atlas.domain.causality.exceptions import (
    HfacsAttributionConflictError,
    HfacsAttributionNotFoundError,
    HfacsCategoryNotFoundError,
    HfacsSubcategoryNotFoundError,
    SheloFactorInteractionConflictError,
    SheloFactorInteractionSameNodeError,
    SheloFactorNotFoundError,
)
from atlas.domain.entities import AccidentEvent, ProjectedAccidentRecord
from atlas.domain.publication.entities import (
    PublicationStatus,
    PublicEventPage,
)
from atlas.domain.publication.exceptions import (
    PublicEventPageNotPublishedError,
    PublicEventPageRetractedError,
)
from tests.domain._fake_uow import InMemoryUnitOfWork

# ── Helpers ──────────────────────────────────────────────────────────────────


def _seed_event_and_page(
    uow: InMemoryUnitOfWork,
    *,
    slug: str = "evt",
    status: PublicationStatus = PublicationStatus.PUBLISHED,
    retraction_note: str | None = None,
):
    """Seed an event + a public page in the requested status."""
    event = AccidentEvent()
    uow.store.events[event.id] = event
    uow.store.projections[event.id] = ProjectedAccidentRecord(
        event_id=event.id, fields={}, completeness_score=0.5
    )
    now = datetime(2024, 6, 1, tzinfo=UTC)
    page = PublicEventPage(
        event_id=event.id,
        slug=slug,
        title=slug.upper(),
        status=status,
        first_published_at=now
        if status in (PublicationStatus.PUBLISHED, PublicationStatus.RETRACTED)
        else None,
        last_published_at=now
        if status in (PublicationStatus.PUBLISHED, PublicationStatus.RETRACTED)
        else None,
        retracted_at=now if status == PublicationStatus.RETRACTED else None,
        retraction_note=retraction_note,
    )
    uow.store.publication.pages[page.id] = page
    return event, page


def _seed_hfacs_category(
    uow: InMemoryUnitOfWork,
    *,
    code: str = "PRE-CRM",
    tier: HfacsTier = HfacsTier.PRECONDITIONS,
) -> HfacsCategory:
    cat = HfacsCategory(
        tier_code=code.split("-")[0],
        code=code,
        tier=tier,
        name=code,
        description="x",
    )
    uow.store.causality.hfacs_categories[cat.id] = cat
    return cat


def _seed_hfacs_subcategory(
    uow: InMemoryUnitOfWork,
    *,
    category: HfacsCategory,
    code: str,
) -> HfacsSubcategory:
    sub = HfacsSubcategory(category_id=category.id, code=code, name=code)
    uow.store.causality.hfacs_subcategories[sub.id] = sub
    return sub


# ── HFACS taxonomy read ─────────────────────────────────────────────────────


class TestHfacsTaxonomy:
    async def test_lists_categories_with_subcategories(self) -> None:
        uow = InMemoryUnitOfWork()
        cat_a = _seed_hfacs_category(uow, code="ACT-SBE", tier=HfacsTier.UNSAFE_ACTS)
        cat_b = _seed_hfacs_category(uow, code="ORG-RM", tier=HfacsTier.ORGANIZATIONAL)
        _seed_hfacs_subcategory(uow, category=cat_a, code="ACT-SBE-1")
        _seed_hfacs_subcategory(uow, category=cat_a, code="ACT-SBE-2")
        view = await ListHfacsTaxonomy(uow).execute()
        # Sorted by (tier_code, code): "ACT" < "ORG".
        codes = [cat.code for cat, _ in view.categories]
        assert codes == ["ACT-SBE", "ORG-RM"]
        # Subcategories on cat_a:
        idx = next(i for i, (c, _) in enumerate(view.categories) if c.id == cat_a.id)
        sub_codes = [s.code for s in view.categories[idx][1]]
        assert sub_codes == ["ACT-SBE-1", "ACT-SBE-2"]
        _ = cat_b


# ── HFACS visibility inheritance ────────────────────────────────────────────


class TestHfacsVisibility:
    async def test_published_event_returns_attributions(self) -> None:
        uow = InMemoryUnitOfWork()
        event, _page = _seed_event_and_page(uow, slug="ok")
        cat = _seed_hfacs_category(uow)
        await AttachEventHfacsAttribution(uow).execute(
            AttachHfacsAttributionInput(
                event_id=event.id,
                category_id=cat.id,
                subcategory_id=None,
                confidence=0.8,
                note=None,
                editor_user_id=uuid4(),
            )
        )
        view = await GetEventHfacs(uow).execute(slug="ok")
        assert len(view.attributions) == 1
        assert view.attributions[0][1].code == "PRE-CRM"

    async def test_draft_event_returns_404(self) -> None:
        """An event in DRAFT must not leak HFACS attributions
        publicly, regardless of whether any attributions exist."""
        uow = InMemoryUnitOfWork()
        event, _ = _seed_event_and_page(uow, slug="wip", status=PublicationStatus.DRAFT)
        cat = _seed_hfacs_category(uow)
        # Editorial attach succeeds even for DRAFT events — analysts
        # prep HFACS while the page is still in workflow.
        await AttachEventHfacsAttribution(uow).execute(
            AttachHfacsAttributionInput(
                event_id=event.id,
                category_id=cat.id,
                subcategory_id=None,
                confidence=0.8,
                note=None,
                editor_user_id=uuid4(),
            )
        )
        with pytest.raises(PublicEventPageNotPublishedError):
            await GetEventHfacs(uow).execute(slug="wip")

    async def test_retracted_event_returns_410_with_note(self) -> None:
        uow = InMemoryUnitOfWork()
        _seed_event_and_page(
            uow,
            slug="gone",
            status=PublicationStatus.RETRACTED,
            retraction_note="Wrong investigation linked.",
        )
        with pytest.raises(PublicEventPageRetractedError) as exc:
            await GetEventHfacs(uow).execute(slug="gone")
        assert exc.value.retraction_note == "Wrong investigation linked."

    async def test_unknown_slug_returns_404(self) -> None:
        uow = InMemoryUnitOfWork()
        with pytest.raises(PublicEventPageNotPublishedError):
            await GetEventHfacs(uow).execute(slug="no-such-page")


# ── HFACS attribution writes ────────────────────────────────────────────────


class TestHfacsAttributionWrites:
    async def test_attach_happy_path(self) -> None:
        uow = InMemoryUnitOfWork()
        event, _ = _seed_event_and_page(uow)
        cat = _seed_hfacs_category(uow)
        a = await AttachEventHfacsAttribution(uow).execute(
            AttachHfacsAttributionInput(
                event_id=event.id,
                category_id=cat.id,
                subcategory_id=None,
                confidence=0.9,
                note="CRM breakdown on approach",
                editor_user_id=uuid4(),
            )
        )
        assert a.confidence == 0.9
        assert a.version == 1
        assert a.subcategory_id is None

    async def test_unknown_category_404(self) -> None:
        uow = InMemoryUnitOfWork()
        event, _ = _seed_event_and_page(uow)
        with pytest.raises(HfacsCategoryNotFoundError):
            await AttachEventHfacsAttribution(uow).execute(
                AttachHfacsAttributionInput(
                    event_id=event.id,
                    category_id=uuid4(),
                    subcategory_id=None,
                    confidence=0.5,
                    note=None,
                    editor_user_id=uuid4(),
                )
            )

    async def test_subcategory_cross_category_rejected(self) -> None:
        """Subcategory exists but belongs to a different category."""
        uow = InMemoryUnitOfWork()
        event, _ = _seed_event_and_page(uow)
        cat_a = _seed_hfacs_category(uow, code="ACT-SBE", tier=HfacsTier.UNSAFE_ACTS)
        cat_b = _seed_hfacs_category(uow, code="ORG-RM", tier=HfacsTier.ORGANIZATIONAL)
        sub_of_b = _seed_hfacs_subcategory(uow, category=cat_b, code="ORG-RM-1")
        with pytest.raises(HfacsSubcategoryNotFoundError):
            await AttachEventHfacsAttribution(uow).execute(
                AttachHfacsAttributionInput(
                    event_id=event.id,
                    category_id=cat_a.id,
                    subcategory_id=sub_of_b.id,
                    confidence=0.5,
                    note=None,
                    editor_user_id=uuid4(),
                )
            )

    async def test_duplicate_natural_key_rejected(self) -> None:
        """Two category-only attributions for the same (event, category)
        violate the natural-key uniqueness from the migration."""
        uow = InMemoryUnitOfWork()
        event, _ = _seed_event_and_page(uow)
        cat = _seed_hfacs_category(uow)
        await AttachEventHfacsAttribution(uow).execute(
            AttachHfacsAttributionInput(
                event_id=event.id,
                category_id=cat.id,
                subcategory_id=None,
                confidence=0.5,
                note=None,
                editor_user_id=uuid4(),
            )
        )
        with pytest.raises(HfacsAttributionConflictError):
            await AttachEventHfacsAttribution(uow).execute(
                AttachHfacsAttributionInput(
                    event_id=event.id,
                    category_id=cat.id,
                    subcategory_id=None,
                    confidence=0.6,
                    note="duplicate",
                    editor_user_id=uuid4(),
                )
            )

    async def test_same_category_different_subcategory_allowed(self) -> None:
        """Two attributions to the same category but different
        subcategories ARE allowed — they're distinct editorial claims."""
        uow = InMemoryUnitOfWork()
        event, _ = _seed_event_and_page(uow)
        cat = _seed_hfacs_category(uow)
        s1 = _seed_hfacs_subcategory(uow, category=cat, code="A")
        s2 = _seed_hfacs_subcategory(uow, category=cat, code="B")
        for sub in (s1, s2):
            await AttachEventHfacsAttribution(uow).execute(
                AttachHfacsAttributionInput(
                    event_id=event.id,
                    category_id=cat.id,
                    subcategory_id=sub.id,
                    confidence=0.5,
                    note=None,
                    editor_user_id=uuid4(),
                )
            )
        view = await GetEventHfacs(uow).execute(slug="evt")
        assert len(view.attributions) == 2

    async def test_optimistic_concurrency_on_update(self) -> None:
        uow = InMemoryUnitOfWork()
        event, _ = _seed_event_and_page(uow)
        cat = _seed_hfacs_category(uow)
        a = await AttachEventHfacsAttribution(uow).execute(
            AttachHfacsAttributionInput(
                event_id=event.id,
                category_id=cat.id,
                subcategory_id=None,
                confidence=0.5,
                note=None,
                editor_user_id=uuid4(),
            )
        )
        # v1 update succeeds.
        await UpdateEventHfacsAttribution(uow).execute(
            UpdateHfacsAttributionInput(
                attribution_id=a.id,
                expected_version=1,
                confidence=0.7,
                note="revised",
                editor_user_id=uuid4(),
            )
        )
        # Stale v1 update conflicts.
        with pytest.raises(HfacsAttributionConflictError):
            await UpdateEventHfacsAttribution(uow).execute(
                UpdateHfacsAttributionInput(
                    attribution_id=a.id,
                    expected_version=1,  # stale
                    confidence=0.8,
                    note="conflicted",
                    editor_user_id=uuid4(),
                )
            )

    async def test_update_unknown_attribution_404(self) -> None:
        uow = InMemoryUnitOfWork()
        with pytest.raises(HfacsAttributionNotFoundError):
            await UpdateEventHfacsAttribution(uow).execute(
                UpdateHfacsAttributionInput(
                    attribution_id=uuid4(),
                    expected_version=1,
                    confidence=0.5,
                    note=None,
                    editor_user_id=uuid4(),
                )
            )

    async def test_delete_is_idempotent(self) -> None:
        """Deleting a non-existent attribution does not raise."""
        uow = InMemoryUnitOfWork()
        await DeleteEventHfacsAttribution(uow).execute(uuid4())  # nothing


# ── SHELO factors ───────────────────────────────────────────────────────────


class TestSheloFactors:
    async def test_attach_and_list_grouped_by_class(self) -> None:
        uow = InMemoryUnitOfWork()
        event, _ = _seed_event_and_page(uow)
        for klass, label in [
            (SheloClass.SOFTWARE, "FADEC fault"),
            (SheloClass.LIVEWARE, "fatigued pilot"),
            (SheloClass.HARDWARE, "right engine"),
        ]:
            await AttachSheloFactor(uow).execute(
                AttachSheloFactorInput(
                    event_id=event.id,
                    factor_class=klass,
                    label=label,
                    description=None,
                    editor_user_id=uuid4(),
                )
            )
        view = await GetEventShelo(uow).execute(slug="evt")
        # Sorted by factor_class enum string ascending.
        labels = [f.label for f in view.factors]
        # Alphabetical order on factor_class: HARDWARE, LIVEWARE, SOFTWARE.
        assert labels == ["right engine", "fatigued pilot", "FADEC fault"]

    async def test_update_concurrency(self) -> None:
        uow = InMemoryUnitOfWork()
        event, _ = _seed_event_and_page(uow)
        factor = await AttachSheloFactor(uow).execute(
            AttachSheloFactorInput(
                event_id=event.id,
                factor_class=SheloClass.HARDWARE,
                label="x",
                description=None,
                editor_user_id=uuid4(),
            )
        )
        await UpdateSheloFactor(uow).execute(
            UpdateSheloFactorInput(
                factor_id=factor.id,
                expected_version=1,
                factor_class=SheloClass.HARDWARE,
                label="x revised",
                description="more detail",
                editor_user_id=uuid4(),
            )
        )
        # Second update with stale v1 must fail.
        from atlas.domain.causality.exceptions import SheloFactorConflictError

        with pytest.raises(SheloFactorConflictError):
            await UpdateSheloFactor(uow).execute(
                UpdateSheloFactorInput(
                    factor_id=factor.id,
                    expected_version=1,
                    factor_class=SheloClass.HARDWARE,
                    label="y",
                    description=None,
                    editor_user_id=uuid4(),
                )
            )

    async def test_update_unknown_factor_404(self) -> None:
        uow = InMemoryUnitOfWork()
        with pytest.raises(SheloFactorNotFoundError):
            await UpdateSheloFactor(uow).execute(
                UpdateSheloFactorInput(
                    factor_id=uuid4(),
                    expected_version=1,
                    factor_class=SheloClass.OTHER,
                    label="x",
                    description=None,
                    editor_user_id=uuid4(),
                )
            )

    async def test_delete_cascades_interactions(self) -> None:
        """Deleting a factor drops interactions referencing it.

        Schema enforces this via ``ondelete=CASCADE``; the fake repo
        replicates it so use-case tests see the same behaviour.
        """
        uow = InMemoryUnitOfWork()
        event, _ = _seed_event_and_page(uow)
        a = await AttachSheloFactor(uow).execute(
            AttachSheloFactorInput(
                event_id=event.id,
                factor_class=SheloClass.SOFTWARE,
                label="A",
                description=None,
                editor_user_id=uuid4(),
            )
        )
        b = await AttachSheloFactor(uow).execute(
            AttachSheloFactorInput(
                event_id=event.id,
                factor_class=SheloClass.LIVEWARE,
                label="B",
                description=None,
                editor_user_id=uuid4(),
            )
        )
        await AttachSheloInteraction(uow).execute(
            AttachSheloInteractionInput(
                event_id=event.id,
                source_factor_id=a.id,
                target_factor_id=b.id,
                interaction_kind=SheloInteractionKind.AGGRAVATED,
                note=None,
                editor_user_id=uuid4(),
            )
        )
        view = await GetEventShelo(uow).execute(slug="evt")
        assert len(view.interactions) == 1
        # Delete A; the AGGRAVATED edge from A→B should vanish too.
        await DeleteSheloFactor(uow).execute(a.id)
        view = await GetEventShelo(uow).execute(slug="evt")
        assert len(view.interactions) == 0


# ── SHELO interactions ──────────────────────────────────────────────────────


class TestSheloInteractions:
    async def test_self_loop_rejected_at_use_case(self) -> None:
        uow = InMemoryUnitOfWork()
        event, _ = _seed_event_and_page(uow)
        f = await AttachSheloFactor(uow).execute(
            AttachSheloFactorInput(
                event_id=event.id,
                factor_class=SheloClass.OTHER,
                label="x",
                description=None,
                editor_user_id=uuid4(),
            )
        )
        with pytest.raises(SheloFactorInteractionSameNodeError):
            await AttachSheloInteraction(uow).execute(
                AttachSheloInteractionInput(
                    event_id=event.id,
                    source_factor_id=f.id,
                    target_factor_id=f.id,
                    interaction_kind=SheloInteractionKind.AGGRAVATED,
                    note=None,
                    editor_user_id=uuid4(),
                )
            )

    async def test_self_loop_rejected_at_entity_level(self) -> None:
        """The entity-level validator catches the same invariant.

        Belt-and-braces: schema CHECK + entity validator + use case
        all reject self-loops.
        """
        with pytest.raises(ValueError):
            SheloFactorInteraction(
                event_id=uuid4(),
                source_factor_id=(_id := uuid4()),
                target_factor_id=_id,
                interaction_kind=SheloInteractionKind.PRECONDITION,
                editor_user_id=uuid4(),
            )

    async def test_factor_must_exist_on_event(self) -> None:
        """Interaction referencing a factor from a different event
        is rejected as "factor not found on this event"."""
        uow = InMemoryUnitOfWork()
        event_a, _ = _seed_event_and_page(uow, slug="a")
        event_b, _ = _seed_event_and_page(uow, slug="b")
        f_b = await AttachSheloFactor(uow).execute(
            AttachSheloFactorInput(
                event_id=event_b.id,
                factor_class=SheloClass.SOFTWARE,
                label="b1",
                description=None,
                editor_user_id=uuid4(),
            )
        )
        f_a = await AttachSheloFactor(uow).execute(
            AttachSheloFactorInput(
                event_id=event_a.id,
                factor_class=SheloClass.SOFTWARE,
                label="a1",
                description=None,
                editor_user_id=uuid4(),
            )
        )
        with pytest.raises(SheloFactorNotFoundError):
            await AttachSheloInteraction(uow).execute(
                AttachSheloInteractionInput(
                    event_id=event_a.id,
                    source_factor_id=f_a.id,
                    target_factor_id=f_b.id,  # belongs to event_b
                    interaction_kind=SheloInteractionKind.AGGRAVATED,
                    note=None,
                    editor_user_id=uuid4(),
                )
            )

    async def test_duplicate_natural_key_rejected(self) -> None:
        uow = InMemoryUnitOfWork()
        event, _ = _seed_event_and_page(uow)
        f1 = await AttachSheloFactor(uow).execute(
            AttachSheloFactorInput(
                event_id=event.id,
                factor_class=SheloClass.SOFTWARE,
                label="1",
                description=None,
                editor_user_id=uuid4(),
            )
        )
        f2 = await AttachSheloFactor(uow).execute(
            AttachSheloFactorInput(
                event_id=event.id,
                factor_class=SheloClass.HARDWARE,
                label="2",
                description=None,
                editor_user_id=uuid4(),
            )
        )
        await AttachSheloInteraction(uow).execute(
            AttachSheloInteractionInput(
                event_id=event.id,
                source_factor_id=f1.id,
                target_factor_id=f2.id,
                interaction_kind=SheloInteractionKind.AGGRAVATED,
                note=None,
                editor_user_id=uuid4(),
            )
        )
        with pytest.raises(SheloFactorInteractionConflictError):
            await AttachSheloInteraction(uow).execute(
                AttachSheloInteractionInput(
                    event_id=event.id,
                    source_factor_id=f1.id,
                    target_factor_id=f2.id,
                    interaction_kind=SheloInteractionKind.AGGRAVATED,
                    note=None,
                    editor_user_id=uuid4(),
                )
            )

    async def test_repo_add_enforces_conflict_directly(self) -> None:
        """Repo-level guard: calling ``add`` twice with the same
        natural key raises the typed conflict error WITHOUT relying
        on the use-case pre-check.

        This pins parity between the fake and the SQL repo — both
        enforce the natural key inside ``add`` so a duplicate
        surfaces as a 409, never a raw IntegrityError (-> 500).  A
        regression that removed either guard would fail here even
        though the use-case-level test might still pass.
        """
        uow = InMemoryUnitOfWork()
        event, _ = _seed_event_and_page(uow)
        f1 = await AttachSheloFactor(uow).execute(
            AttachSheloFactorInput(
                event_id=event.id,
                factor_class=SheloClass.SOFTWARE,
                label="1",
                description=None,
                editor_user_id=uuid4(),
            )
        )
        f2 = await AttachSheloFactor(uow).execute(
            AttachSheloFactorInput(
                event_id=event.id,
                factor_class=SheloClass.HARDWARE,
                label="2",
                description=None,
                editor_user_id=uuid4(),
            )
        )
        interaction = SheloFactorInteraction(
            event_id=event.id,
            source_factor_id=f1.id,
            target_factor_id=f2.id,
            interaction_kind=SheloInteractionKind.MASKED,
            editor_user_id=uuid4(),
        )
        await uow.shelo_factor_interactions.add(interaction)
        # Second add of an equivalent edge (new id, same natural key).
        dup = SheloFactorInteraction(
            event_id=event.id,
            source_factor_id=f1.id,
            target_factor_id=f2.id,
            interaction_kind=SheloInteractionKind.MASKED,
            editor_user_id=uuid4(),
        )
        with pytest.raises(SheloFactorInteractionConflictError):
            await uow.shelo_factor_interactions.add(dup)

    async def test_different_kinds_same_edge_allowed(self) -> None:
        """A→B can simultaneously be AGGRAVATED and PRECONDITION.
        They're different editorial claims."""
        uow = InMemoryUnitOfWork()
        event, _ = _seed_event_and_page(uow)
        f1 = await AttachSheloFactor(uow).execute(
            AttachSheloFactorInput(
                event_id=event.id,
                factor_class=SheloClass.SOFTWARE,
                label="1",
                description=None,
                editor_user_id=uuid4(),
            )
        )
        f2 = await AttachSheloFactor(uow).execute(
            AttachSheloFactorInput(
                event_id=event.id,
                factor_class=SheloClass.HARDWARE,
                label="2",
                description=None,
                editor_user_id=uuid4(),
            )
        )
        for kind in (
            SheloInteractionKind.AGGRAVATED,
            SheloInteractionKind.PRECONDITION,
        ):
            await AttachSheloInteraction(uow).execute(
                AttachSheloInteractionInput(
                    event_id=event.id,
                    source_factor_id=f1.id,
                    target_factor_id=f2.id,
                    interaction_kind=kind,
                    note=None,
                    editor_user_id=uuid4(),
                )
            )
        view = await GetEventShelo(uow).execute(slug="evt")
        kinds = sorted(i.interaction_kind.value for i in view.interactions)
        assert kinds == ["AGGRAVATED", "PRECONDITION"]

    async def test_delete_interaction_idempotent(self) -> None:
        uow = InMemoryUnitOfWork()
        await DeleteSheloInteraction(uow).execute(uuid4())  # no-op

    async def test_cycles_permitted(self) -> None:
        """Schema and use-case both allow A→B and B→A coexisting.
        Real causal graphs sometimes contain mutual feedback loops;
        Phase 4 surfaces them rather than rejecting at INSERT."""
        uow = InMemoryUnitOfWork()
        event, _ = _seed_event_and_page(uow)
        f1 = await AttachSheloFactor(uow).execute(
            AttachSheloFactorInput(
                event_id=event.id,
                factor_class=SheloClass.SOFTWARE,
                label="1",
                description=None,
                editor_user_id=uuid4(),
            )
        )
        f2 = await AttachSheloFactor(uow).execute(
            AttachSheloFactorInput(
                event_id=event.id,
                factor_class=SheloClass.HARDWARE,
                label="2",
                description=None,
                editor_user_id=uuid4(),
            )
        )
        await AttachSheloInteraction(uow).execute(
            AttachSheloInteractionInput(
                event_id=event.id,
                source_factor_id=f1.id,
                target_factor_id=f2.id,
                interaction_kind=SheloInteractionKind.AGGRAVATED,
                note=None,
                editor_user_id=uuid4(),
            )
        )
        await AttachSheloInteraction(uow).execute(
            AttachSheloInteractionInput(
                event_id=event.id,
                source_factor_id=f2.id,
                target_factor_id=f1.id,
                interaction_kind=SheloInteractionKind.MASKED,
                note=None,
                editor_user_id=uuid4(),
            )
        )
        view = await GetEventShelo(uow).execute(slug="evt")
        assert len(view.interactions) == 2


# ── SHELO visibility inheritance ────────────────────────────────────────────


class TestSheloVisibility:
    async def test_draft_event_returns_404(self) -> None:
        uow = InMemoryUnitOfWork()
        _seed_event_and_page(uow, slug="wip", status=PublicationStatus.DRAFT)
        with pytest.raises(PublicEventPageNotPublishedError):
            await GetEventShelo(uow).execute(slug="wip")

    async def test_retracted_event_returns_410(self) -> None:
        uow = InMemoryUnitOfWork()
        _seed_event_and_page(
            uow,
            slug="gone",
            status=PublicationStatus.RETRACTED,
            retraction_note="Misattributed factors.",
        )
        with pytest.raises(PublicEventPageRetractedError):
            await GetEventShelo(uow).execute(slug="gone")
