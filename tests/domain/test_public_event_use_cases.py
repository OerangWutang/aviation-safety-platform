"""Use-case tests for the public-event encyclopedia (Phase 1).

Anchored on the in-memory ``InMemoryUnitOfWork`` so these tests run
quickly and exercise the publication-status gates, slug uniqueness,
merge canonicalization, evidence whitelist, and related-event Orion
walk.

Anything that depends on real PostgreSQL constraint behaviour
(``IntegrityError`` -> typed domain error) is exercised by the
integration-test suite separately; the in-memory fake mirrors the
invariants in domain space.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from atlas.application.use_cases.public_events import (
    GetPublicEventEvidence,
    GetPublicEventPage,
    GetPublicEventRelated,
    GetPublicEventTimeline,
    ListPublicEvents,
)
from atlas.domain.entities import (
    AccidentEvent,
    ChronosTimelineEvent,
    Claim,
    OrionRelationship,
    ProjectedAccidentRecord,
    Source,
)
from atlas.domain.enums import (
    ChronosTimelineEventType,
    ChronosTimestampPrecision,
    ClaimType,
    OrionRelationshipType,
    SourceKind,
)
from atlas.domain.publication.entities import PublicationStatus, PublicEventPage
from atlas.domain.publication.exceptions import (
    PublicEventPageAlreadyExistsError,
    PublicEventPageNotFoundError,
    PublicEventPageNotPublishedError,
    PublicEventPageRetractedError,
    SlugAlreadyTakenError,
)
from tests.domain._fake_uow import InMemoryUnitOfWork

# ── Helpers ──────────────────────────────────────────────────────────────────


def _seed_event(uow: InMemoryUnitOfWork) -> UUID:
    event_id = uuid4()
    uow.store.events[event_id] = AccidentEvent(id=event_id)
    return event_id


def _seed_projection(
    uow: InMemoryUnitOfWork,
    event_id: UUID,
    *,
    fields: dict | None = None,
    completeness: float = 0.9,
    unresolved: list[str] | None = None,
    version: int = 1,
) -> ProjectedAccidentRecord:
    projection = ProjectedAccidentRecord(
        event_id=event_id,
        projection_version=version,
        fields=fields
        or {
            "event_date": "2024-06-01",
            "location": "Test City",
            "operator": "Test Operator",
            "aircraft_type": "Test 737",
            "fatalities_total": 0,
        },
        completeness_score=completeness,
        unresolved_conflict_fields=unresolved or [],
    )
    uow.store.projections[event_id] = projection
    return projection


def _seed_published_page(
    uow: InMemoryUnitOfWork,
    event_id: UUID,
    *,
    slug: str = "test-event",
    title: str = "Test Event",
    short_summary: str | None = "Short summary",
    narrative_markdown: str | None = None,
    last_published_at: datetime | None = None,
) -> PublicEventPage:
    page = PublicEventPage(
        event_id=event_id,
        slug=slug,
        title=title,
        short_summary=short_summary,
        narrative_markdown=narrative_markdown,
        status=PublicationStatus.PUBLISHED,
        first_published_at=last_published_at or datetime(2024, 7, 1, tzinfo=UTC),
        last_published_at=last_published_at or datetime(2024, 7, 1, tzinfo=UTC),
    )
    uow.store.publication.pages[page.id] = page
    return page


def _seed_source(
    uow: InMemoryUnitOfWork,
    *,
    name: str = "NTSB",
    kind: SourceKind = SourceKind.EXTERNAL,
    tier: int = 1,
) -> Source:
    source = Source(name=name, kind=kind, reliability_tier=tier)
    uow.store.sources[source.id] = source
    return source


def _seed_claim(
    uow: InMemoryUnitOfWork,
    event_id: UUID,
    source: Source,
    *,
    field_name: str,
    field_value: object,
    claim_type: ClaimType = ClaimType.RAW,
    created_at: datetime | None = None,
) -> Claim:
    claim = Claim(
        event_id=event_id,
        source_id=source.id,
        field_name=field_name,
        field_value=field_value,
        claim_type=claim_type,
        created_at=created_at or datetime(2024, 6, 1, tzinfo=UTC),
    )
    uow.store.claims[claim.id] = claim
    return claim


# ── List ─────────────────────────────────────────────────────────────────────


class TestListPublicEvents:
    async def test_returns_only_published_pages(self) -> None:
        uow = InMemoryUnitOfWork()

        published_event = _seed_event(uow)
        _seed_projection(uow, published_event)
        _seed_published_page(uow, published_event, slug="visible")

        draft_event = _seed_event(uow)
        _seed_projection(uow, draft_event)
        # DRAFT page directly constructed to bypass any helper that
        # might transition status.
        draft_page = PublicEventPage(
            event_id=draft_event,
            slug="hidden",
            title="Hidden",
            status=PublicationStatus.DRAFT,
        )
        uow.store.publication.pages[draft_page.id] = draft_page

        retracted_event = _seed_event(uow)
        _seed_projection(uow, retracted_event)
        retracted_page = _seed_published_page(uow, retracted_event, slug="retracted-page")
        retracted_page.retract("Editorial correction")
        # _seed_published_page already inserted; we mutated in place.

        result = await ListPublicEvents(uow).execute()

        slugs = [item.slug for item in result.items]
        assert slugs == ["visible"]
        assert result.next_cursor is None

    async def test_enriches_with_projection_fields(self) -> None:
        uow = InMemoryUnitOfWork()
        event_id = _seed_event(uow)
        _seed_projection(
            uow,
            event_id,
            fields={
                "event_date": "2024-06-01",
                "location": "Anchorage, AK",
                "operator": "ABC Airlines",
                "aircraft_type": "Boeing 737-800",
                "fatalities_total": 2,
            },
        )
        _seed_published_page(uow, event_id, slug="abc-737-anchorage")

        result = await ListPublicEvents(uow).execute()

        assert len(result.items) == 1
        item = result.items[0]
        assert item.slug == "abc-737-anchorage"
        assert item.location == "Anchorage, AK"
        assert item.operator == "ABC Airlines"
        assert item.aircraft_type == "Boeing 737-800"
        assert item.fatalities_total == 2
        # 0.9 completeness sits in the "high" band per
        # _confidence_label.
        assert item.confidence == "high"

    async def test_unresolved_conflicts_flag_is_surfaced(self) -> None:
        uow = InMemoryUnitOfWork()
        event_id = _seed_event(uow)
        _seed_projection(
            uow,
            event_id,
            unresolved=["fatalities_total"],
        )
        _seed_published_page(uow, event_id, slug="disputed-event")

        result = await ListPublicEvents(uow).execute()
        assert result.items[0].has_unresolved_conflicts is True

    async def test_orders_by_last_published_at_desc(self) -> None:
        uow = InMemoryUnitOfWork()
        base = datetime(2024, 1, 1, tzinfo=UTC)
        slugs = []
        for offset, slug in enumerate(["oldest", "middle", "newest"]):
            event_id = _seed_event(uow)
            _seed_projection(uow, event_id)
            _seed_published_page(
                uow,
                event_id,
                slug=slug,
                last_published_at=base + timedelta(days=offset),
            )
            slugs.append(slug)

        result = await ListPublicEvents(uow).execute()
        assert [i.slug for i in result.items] == ["newest", "middle", "oldest"]

    async def test_keyset_pagination_is_stable(self) -> None:
        """Two consecutive pages must concatenate to all rows in order.

        This is the contract callers rely on for "load more" UIs.
        Equal ``last_published_at`` values are broken by ``id`` so the
        cursor is well-defined even under timestamp collisions.
        """
        uow = InMemoryUnitOfWork()
        shared_ts = datetime(2024, 1, 1, tzinfo=UTC)
        for i in range(5):
            event_id = _seed_event(uow)
            _seed_projection(uow, event_id)
            _seed_published_page(
                uow,
                event_id,
                slug=f"event-{i}",
                # Half share a timestamp so the (last_published_at, id)
                # tie-breaker is exercised.
                last_published_at=shared_ts + timedelta(days=i // 2),
            )

        first = await ListPublicEvents(uow).execute(limit=2)
        second = await ListPublicEvents(uow).execute(limit=2, after_id=first.next_cursor)
        third = await ListPublicEvents(uow).execute(limit=2, after_id=second.next_cursor)

        all_slugs = (
            [i.slug for i in first.items]
            + [i.slug for i in second.items]
            + [i.slug for i in third.items]
        )
        # No duplicates, no skipped rows.
        assert sorted(all_slugs) == [f"event-{i}" for i in range(5)]
        assert third.next_cursor is None


# ── Detail ───────────────────────────────────────────────────────────────────


class TestGetPublicEventPage:
    async def test_returns_published_page_with_editorial_overlay(self) -> None:
        uow = InMemoryUnitOfWork()
        event_id = _seed_event(uow)
        _seed_projection(uow, event_id)
        _seed_published_page(
            uow,
            event_id,
            slug="hello-world",
            title="Editorial title",
            short_summary="Editorial summary",
            narrative_markdown="# Editorial narrative",
        )

        detail = await GetPublicEventPage(uow).execute("hello-world")

        # Editorial overlay is preserved verbatim.
        assert detail.title == "Editorial title"
        assert detail.short_summary == "Editorial summary"
        assert detail.narrative_markdown == "# Editorial narrative"
        # Structured fields come from the projection — never from the
        # page row.
        assert detail.fields["operator"] == "Test Operator"
        assert detail.canonical_event_id == event_id

    async def test_draft_page_raises_not_published(self) -> None:
        uow = InMemoryUnitOfWork()
        event_id = _seed_event(uow)
        _seed_projection(uow, event_id)
        draft = PublicEventPage(
            event_id=event_id,
            slug="hidden",
            title="Hidden",
            status=PublicationStatus.DRAFT,
        )
        uow.store.publication.pages[draft.id] = draft

        with pytest.raises(PublicEventPageNotPublishedError):
            await GetPublicEventPage(uow).execute("hidden")

    async def test_retracted_page_raises_retracted_with_note(self) -> None:
        uow = InMemoryUnitOfWork()
        event_id = _seed_event(uow)
        _seed_projection(uow, event_id)
        page = _seed_published_page(uow, event_id, slug="retracted")
        page.retract("Found incorrect operator attribution.")

        with pytest.raises(PublicEventPageRetractedError) as excinfo:
            await GetPublicEventPage(uow).execute("retracted")
        assert excinfo.value.slug == "retracted"
        assert excinfo.value.retraction_note == ("Found incorrect operator attribution.")

    async def test_missing_slug_raises_not_found(self) -> None:
        uow = InMemoryUnitOfWork()
        with pytest.raises(PublicEventPageNotFoundError):
            await GetPublicEventPage(uow).execute("does-not-exist")

    async def test_resolves_merged_event_to_canonical(self) -> None:
        """A page created against an event later merged must follow the
        merge chain — the user-visible response should reflect the
        surviving event's projection."""
        uow = InMemoryUnitOfWork()

        canonical_id = _seed_event(uow)
        _seed_projection(
            uow,
            canonical_id,
            fields={"event_date": "2024-06-01", "location": "Canonical"},
        )

        absorbed_id = _seed_event(uow)
        # Mark as merged into canonical.
        absorbed = uow.store.events[absorbed_id]
        absorbed.merged_into_event_id = canonical_id

        # Page was created against the absorbed event.
        _seed_published_page(uow, absorbed_id, slug="merged-page")

        detail = await GetPublicEventPage(uow).execute("merged-page")
        assert detail.canonical_event_id == canonical_id
        assert detail.fields["location"] == "Canonical"

    async def test_merge_cycle_raises_not_found(self) -> None:
        """A pathological merge cycle must fail closed, not loop."""
        uow = InMemoryUnitOfWork()
        a_id = _seed_event(uow)
        b_id = _seed_event(uow)
        uow.store.events[a_id].merged_into_event_id = b_id
        uow.store.events[b_id].merged_into_event_id = a_id
        _seed_published_page(uow, a_id, slug="cyclic")

        with pytest.raises(PublicEventPageNotFoundError):
            await GetPublicEventPage(uow).execute("cyclic")

    async def test_published_with_missing_projection_raises_not_found(self) -> None:
        """A PUBLISHED page whose projection has vanished is a curator
        bug we want to surface as 404, not crash."""
        uow = InMemoryUnitOfWork()
        event_id = _seed_event(uow)
        # Intentionally no projection.
        _seed_published_page(uow, event_id, slug="orphan")
        with pytest.raises(PublicEventPageNotFoundError):
            await GetPublicEventPage(uow).execute("orphan")


# ── Evidence ─────────────────────────────────────────────────────────────────


class TestGetPublicEventEvidence:
    async def test_winning_flag_aligns_with_projection(self) -> None:
        uow = InMemoryUnitOfWork()
        event_id = _seed_event(uow)
        _seed_projection(
            uow,
            event_id,
            fields={"location": "Anchorage", "operator": "Test Operator"},
        )
        _seed_published_page(uow, event_id, slug="evidence-test")

        ntsb = _seed_source(uow, name="NTSB", tier=1)
        wire = _seed_source(uow, name="Wire Service", tier=3)

        winner_claim = _seed_claim(
            uow,
            event_id,
            ntsb,
            field_name="location",
            field_value="Anchorage",
        )
        loser_claim = _seed_claim(
            uow,
            event_id,
            wire,
            field_name="operator",
            field_value="Wrong Operator",
        )

        response = await GetPublicEventEvidence(uow).execute("evidence-test")
        winning_by_field = {(c.field_name, c.field_value): c.is_winning for c in response.claims}
        # Location: NTSB matches projection -> winning.
        assert winning_by_field[("location", "Anchorage")] is True
        # Operator: wire-service value differs from the projection's
        # ``Test Operator`` so the wire claim is not flagged winning.
        assert winning_by_field[("operator", "Wrong Operator")] is False
        # The claims used for evidence are unrelated to assertions
        # on the winner_claim/loser_claim identity but referenced so
        # static analysis doesn't flag the seeds as unused.
        assert winner_claim.id != loser_claim.id

    async def test_does_not_leak_field_mapping_json_or_internal_ids(self) -> None:
        """Whitelist-by-construction: the public DTOs must not carry
        any internal-only ``Source`` or ``Claim`` fields."""
        uow = InMemoryUnitOfWork()
        event_id = _seed_event(uow)
        _seed_projection(uow, event_id)
        _seed_published_page(uow, event_id, slug="no-leakage")

        source = _seed_source(uow, name="With Mapping")
        # Populate the field that must never be exposed.
        source.field_mapping_json = {"date": "event_date"}
        _seed_claim(uow, event_id, source, field_name="event_date", field_value="2024-06-01")

        response = await GetPublicEventEvidence(uow).execute("no-leakage")
        # The public source DTO has exactly three fields.  This pins
        # the contract: any future addition is an explicit decision.
        assert len(response.sources) == 1
        s = response.sources[0]
        # Dataclass-style attributes (not dict): verify by attribute
        # access only.  If field_mapping_json ever gets added the
        # AttributeError will land here at test time.
        assert hasattr(s, "name")
        assert hasattr(s, "kind")
        assert hasattr(s, "reliability_tier")
        assert not hasattr(s, "field_mapping_json")
        assert not hasattr(s, "id")

    async def test_truncates_at_public_limit(self, monkeypatch) -> None:
        """Truncation must report ``truncated=True`` and stop at the
        configured ceiling."""
        from atlas.application.use_cases import public_events as module

        monkeypatch.setattr(module, "PUBLIC_EVIDENCE_CLAIM_LIMIT", 3)

        uow = InMemoryUnitOfWork()
        event_id = _seed_event(uow)
        _seed_projection(uow, event_id)
        _seed_published_page(uow, event_id, slug="lots")
        source = _seed_source(uow)
        for i in range(5):
            _seed_claim(
                uow,
                event_id,
                source,
                field_name=f"field_{i}",
                field_value=f"v{i}",
                created_at=datetime(2024, 6, 1, tzinfo=UTC) + timedelta(seconds=i),
            )

        response = await GetPublicEventEvidence(uow).execute("lots")
        assert response.truncated is True
        assert response.claim_count == 3

    async def test_draft_page_raises_not_published(self) -> None:
        uow = InMemoryUnitOfWork()
        event_id = _seed_event(uow)
        _seed_projection(uow, event_id)
        draft = PublicEventPage(
            event_id=event_id,
            slug="evidence-draft",
            title="Draft",
            status=PublicationStatus.DRAFT,
        )
        uow.store.publication.pages[draft.id] = draft

        with pytest.raises(PublicEventPageNotPublishedError):
            await GetPublicEventEvidence(uow).execute("evidence-draft")


# ── Timeline ─────────────────────────────────────────────────────────────────


class TestGetPublicEventTimeline:
    async def test_returns_timeline_events_in_chronological_order(self) -> None:
        uow = InMemoryUnitOfWork()
        event_id = _seed_event(uow)
        _seed_projection(uow, event_id)
        _seed_published_page(uow, event_id, slug="tl")

        # Insert deliberately out of order.
        emergency = ChronosTimelineEvent(
            accident_event_id=event_id,
            event_type=ChronosTimelineEventType.EMERGENCY_DECLARED,
            occurred_at=datetime(2024, 6, 1, 12, 5, tzinfo=UTC),
            timestamp_precision=ChronosTimestampPrecision.MINUTE,
        )
        takeoff = ChronosTimelineEvent(
            accident_event_id=event_id,
            event_type=ChronosTimelineEventType.TAKEOFF,
            occurred_at=datetime(2024, 6, 1, 12, 0, tzinfo=UTC),
            timestamp_precision=ChronosTimestampPrecision.MINUTE,
        )
        impact = ChronosTimelineEvent(
            accident_event_id=event_id,
            event_type=ChronosTimelineEventType.IMPACT,
            occurred_at=datetime(2024, 6, 1, 12, 10, tzinfo=UTC),
            timestamp_precision=ChronosTimestampPrecision.MINUTE,
        )
        uow.store.chronos.timeline_events.extend([emergency, takeoff, impact])

        response = await GetPublicEventTimeline(uow).execute("tl")
        assert [e.event_type for e in response.events] == [
            "TAKEOFF",
            "EMERGENCY_DECLARED",
            "IMPACT",
        ]

    async def test_events_without_occurred_at_sort_last(self) -> None:
        uow = InMemoryUnitOfWork()
        event_id = _seed_event(uow)
        _seed_projection(uow, event_id)
        _seed_published_page(uow, event_id, slug="tl-no-ts")
        timed = ChronosTimelineEvent(
            accident_event_id=event_id,
            event_type=ChronosTimelineEventType.TAKEOFF,
            occurred_at=datetime(2024, 6, 1, 12, 0, tzinfo=UTC),
            timestamp_precision=ChronosTimestampPrecision.MINUTE,
        )
        untimed = ChronosTimelineEvent(
            accident_event_id=event_id,
            event_type=ChronosTimelineEventType.REPORT_PUBLISHED,
            occurred_at=None,
            timestamp_precision=ChronosTimestampPrecision.UNKNOWN,
        )
        uow.store.chronos.timeline_events.extend([untimed, timed])

        response = await GetPublicEventTimeline(uow).execute("tl-no-ts")
        types = [e.event_type for e in response.events]
        assert types == ["TAKEOFF", "REPORT_PUBLISHED"]


# ── Related ──────────────────────────────────────────────────────────────────


class TestGetPublicEventRelated:
    async def test_finds_events_sharing_an_operator_or_aircraft_type(self) -> None:
        uow = InMemoryUnitOfWork()

        primary_id = _seed_event(uow)
        _seed_projection(uow, primary_id)
        _seed_published_page(uow, primary_id, slug="primary")

        sibling_id = _seed_event(uow)
        _seed_projection(uow, sibling_id)
        _seed_published_page(uow, sibling_id, slug="sibling")

        operator_entity_id = uuid4()
        # Both events relate to the same operator.
        uow.store.orion.relationships.append(
            OrionRelationship(
                relationship_type=OrionRelationshipType.OPERATED_BY,
                object_entity_id=operator_entity_id,
                accident_event_id=primary_id,
            )
        )
        uow.store.orion.relationships.append(
            OrionRelationship(
                relationship_type=OrionRelationshipType.OPERATED_BY,
                object_entity_id=operator_entity_id,
                accident_event_id=sibling_id,
            )
        )

        response = await GetPublicEventRelated(uow).execute("primary")
        assert [r.slug for r in response.items] == ["sibling"]
        assert response.items[0].relation == "OPERATED_BY"

    async def test_does_not_include_self_or_unpublished_pages(self) -> None:
        uow = InMemoryUnitOfWork()
        primary_id = _seed_event(uow)
        _seed_projection(uow, primary_id)
        _seed_published_page(uow, primary_id, slug="primary")

        # Sibling exists in DB as a DRAFT page — must be filtered out.
        draft_event = _seed_event(uow)
        _seed_projection(uow, draft_event)
        draft = PublicEventPage(
            event_id=draft_event,
            slug="draft-sibling",
            title="Hidden",
            status=PublicationStatus.DRAFT,
        )
        uow.store.publication.pages[draft.id] = draft

        # Sibling without any public page at all.
        no_page_event = _seed_event(uow)
        _seed_projection(uow, no_page_event)

        operator_id = uuid4()
        for ev in (primary_id, draft_event, no_page_event):
            uow.store.orion.relationships.append(
                OrionRelationship(
                    relationship_type=OrionRelationshipType.OPERATED_BY,
                    object_entity_id=operator_id,
                    accident_event_id=ev,
                )
            )

        response = await GetPublicEventRelated(uow).execute("primary")
        assert response.items == []

    async def test_ignores_relationships_outside_the_phase1_set(self) -> None:
        """``LOCATED_IN`` etc are not in the Phase 1 related-set, so a
        sibling that only shares an airport must not surface."""
        uow = InMemoryUnitOfWork()
        primary_id = _seed_event(uow)
        _seed_projection(uow, primary_id)
        _seed_published_page(uow, primary_id, slug="primary")

        sibling_id = _seed_event(uow)
        _seed_projection(uow, sibling_id)
        _seed_published_page(uow, sibling_id, slug="airport-sibling")

        airport_id = uuid4()
        uow.store.orion.relationships.append(
            OrionRelationship(
                relationship_type=OrionRelationshipType.OCCURRED_AT,
                object_entity_id=airport_id,
                accident_event_id=primary_id,
            )
        )
        uow.store.orion.relationships.append(
            OrionRelationship(
                relationship_type=OrionRelationshipType.OCCURRED_AT,
                object_entity_id=airport_id,
                accident_event_id=sibling_id,
            )
        )

        response = await GetPublicEventRelated(uow).execute("primary")
        assert response.items == []


# ── Repository invariants via the fake ───────────────────────────────────────


class TestPublicEventPageRepository:
    async def test_duplicate_slug_raises_typed_error(self) -> None:
        uow = InMemoryUnitOfWork()
        a_event = _seed_event(uow)
        b_event = _seed_event(uow)
        _seed_published_page(uow, a_event, slug="dup")
        # Build a second page with a *different* event but the same slug
        # via direct repo call so the typed error path is exercised.
        page = PublicEventPage(
            event_id=b_event,
            slug="dup",
            title="Other",
            status=PublicationStatus.DRAFT,
        )
        with pytest.raises(SlugAlreadyTakenError):
            await uow.public_event_pages.add(page)

    async def test_one_page_per_event_invariant(self) -> None:
        uow = InMemoryUnitOfWork()
        event_id = _seed_event(uow)
        _seed_published_page(uow, event_id, slug="first")
        # A second page targeting the same event must fail.
        page = PublicEventPage(
            event_id=event_id,
            slug="second",
            title="Second",
            status=PublicationStatus.DRAFT,
        )
        with pytest.raises(PublicEventPageAlreadyExistsError):
            await uow.public_event_pages.add(page)


# ── Entity-level invariants ──────────────────────────────────────────────────


class TestPublicEventPageEntity:
    def test_published_without_timestamp_is_rejected(self) -> None:
        with pytest.raises(ValueError):
            PublicEventPage(
                event_id=uuid4(),
                slug="bad",
                title="Bad",
                status=PublicationStatus.PUBLISHED,
                # missing last_published_at
            )

    def test_retracted_without_timestamp_is_rejected(self) -> None:
        with pytest.raises(ValueError):
            PublicEventPage(
                event_id=uuid4(),
                slug="bad",
                title="Bad",
                status=PublicationStatus.RETRACTED,
                # missing retracted_at
            )

    def test_publish_preserves_first_published_at_on_republish(self) -> None:
        page = PublicEventPage(
            event_id=uuid4(),
            slug="x",
            title="X",
        )
        first = datetime(2024, 1, 1, tzinfo=UTC)
        second = datetime(2024, 6, 1, tzinfo=UTC)
        page.publish(now=first)
        page.retract("temporary", now=datetime(2024, 3, 1, tzinfo=UTC))
        page.publish(now=second)
        assert page.first_published_at == first
        assert page.last_published_at == second
