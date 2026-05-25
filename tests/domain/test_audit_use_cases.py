"""Use-case tests for the Phase 11 audit endpoints.

These exercise the explanation surface end-to-end against the
in-memory UoW: page audit, field explanation, claim explanation, and
source verification.

The plain-English prose is asserted on substring keywords rather than
exact wording so a copy editor can tune the phrasing without breaking
tests, but the wording-defining branches (e.g. "manual override
beats raw") are pinned.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import cast
from uuid import uuid4

import pytest

from atlas.application.use_cases.audit import (
    GetClaimExplanation,
    GetFieldExplanation,
    GetPublicEventAudit,
    GetSourceVerification,
)
from atlas.domain.entities import (
    AccidentEvent,
    Claim,
    ClaimConflict,
    ClaimHistory,
    ProjectedAccidentRecord,
    RawSnapshot,
    Source,
)
from atlas.domain.enums import (
    ClaimType,
    ConflictModifierReason,
    ConflictStatus,
    ModifierType,
    SourceKind,
)
from atlas.domain.exceptions import NotFoundError
from atlas.domain.publication.entities import PublicationStatus, PublicEventPage
from tests.domain._fake_uow import InMemoryUnitOfWork

# ── Fixtures / helpers ──────────────────────────────────────────────────────


def _seed_event(uow: InMemoryUnitOfWork):
    event = AccidentEvent()
    uow.store.events[event.id] = event
    return event


def _seed_source(
    uow: InMemoryUnitOfWork,
    *,
    name: str = "NTSB",
    kind: SourceKind = SourceKind.EXTERNAL,
    tier: int = 1,
) -> Source:
    s = Source(name=name, kind=kind, reliability_tier=tier)
    uow.store.sources[s.id] = s
    return s


def _seed_projection(
    uow: InMemoryUnitOfWork,
    event_id,
    *,
    fields: dict | None = None,
    completeness: float = 0.9,
    unresolved: list[str] | None = None,
):
    p = ProjectedAccidentRecord(
        event_id=event_id,
        projection_version=1,
        fields=fields or {"operator": "ABC Airlines", "location": "Anchorage"},
        completeness_score=completeness,
        unresolved_conflict_fields=unresolved or [],
    )
    uow.store.projections[event_id] = p
    return p


def _seed_claim(
    uow: InMemoryUnitOfWork,
    event_id,
    source: Source,
    *,
    field_name: str,
    field_value,
    claim_type: ClaimType = ClaimType.RAW,
    created_at: datetime | None = None,
    raw_snapshot_id=None,
) -> Claim:
    c = Claim(
        event_id=event_id,
        source_id=source.id,
        field_name=field_name,
        field_value=field_value,
        claim_type=claim_type,
        created_at=created_at or datetime(2024, 6, 1, tzinfo=UTC),
        raw_snapshot_id=raw_snapshot_id,
    )
    uow.store.claims[c.id] = c
    return c


def _seed_published_page(uow: InMemoryUnitOfWork, event_id, slug: str):
    page = PublicEventPage(
        event_id=event_id,
        slug=slug,
        title="Some title",
        short_summary="Short summary",
        status=PublicationStatus.PUBLISHED,
        first_published_at=datetime(2024, 7, 1, tzinfo=UTC),
        last_published_at=datetime(2024, 7, 1, tzinfo=UTC),
    )
    uow.store.publication.pages[page.id] = page
    return page


# ── Page audit ──────────────────────────────────────────────────────────────


class TestGetPublicEventAudit:
    async def test_response_includes_all_projected_fields(self) -> None:
        uow = InMemoryUnitOfWork()
        event = _seed_event(uow)
        _seed_projection(
            uow,
            event.id,
            fields={
                "operator": "ABC Airlines",
                "location": "Anchorage",
                "fatalities_total": 0,
            },
        )
        _seed_published_page(uow, event.id, "test-event")

        result = await GetPublicEventAudit(uow).execute("test-event")
        names = {row.field_name for row in result.fields}
        assert names == {"operator", "location", "fatalities_total"}

    async def test_disputed_fields_are_flagged(self) -> None:
        uow = InMemoryUnitOfWork()
        event = _seed_event(uow)
        _seed_projection(
            uow,
            event.id,
            unresolved=["operator"],
        )
        _seed_published_page(uow, event.id, "disputed-page")

        result = await GetPublicEventAudit(uow).execute("disputed-page")
        operator_row = next(r for r in result.fields if r.field_name == "operator")
        location_row = next(r for r in result.fields if r.field_name == "location")
        assert operator_row.is_disputed is True
        assert location_row.is_disputed is False
        # Disputed row's prose explicitly mentions disagreement.
        assert (
            "disagree" in operator_row.plain_english.lower()
            or "dispute" in operator_row.plain_english.lower()
        )

    async def test_manual_override_fields_are_flagged(self) -> None:
        uow = InMemoryUnitOfWork()
        event = _seed_event(uow)
        _seed_projection(uow, event.id)
        _seed_published_page(uow, event.id, "override-page")
        source = _seed_source(uow)
        _seed_claim(
            uow,
            event.id,
            source,
            field_name="operator",
            field_value="ABC Airlines",
            claim_type=ClaimType.MANUAL_OVERRIDE,
        )

        result = await GetPublicEventAudit(uow).execute("override-page")
        operator_row = next(r for r in result.fields if r.field_name == "operator")
        assert operator_row.is_manually_overridden is True
        # Wording must explain it was an editor, not "system override".
        assert "editor" in operator_row.plain_english.lower()

    async def test_summary_describes_field_count(self) -> None:
        uow = InMemoryUnitOfWork()
        event = _seed_event(uow)
        _seed_projection(uow, event.id, fields={"operator": "X"})
        _seed_published_page(uow, event.id, "small-page")
        result = await GetPublicEventAudit(uow).execute("small-page")
        # The summary mentions the field count somehow — substring not
        # exact wording.
        assert "1" in result.summary

    async def test_confidence_meaning_is_present(self) -> None:
        uow = InMemoryUnitOfWork()
        event = _seed_event(uow)
        _seed_projection(uow, event.id, completeness=0.92)
        _seed_published_page(uow, event.id, "high-conf")
        result = await GetPublicEventAudit(uow).execute("high-conf")
        assert result.confidence == "high"
        # Plain-English meaning, not just the label.
        assert result.confidence_meaning != "high"
        assert len(result.confidence_meaning) > 10

    async def test_missing_projection_raises_not_found(self) -> None:
        uow = InMemoryUnitOfWork()
        event = _seed_event(uow)
        _seed_published_page(uow, event.id, "no-proj")
        # No projection seeded.
        with pytest.raises(NotFoundError):
            await GetPublicEventAudit(uow).execute("no-proj")


# ── Field explanation ───────────────────────────────────────────────────────


class TestGetFieldExplanation:
    async def test_winner_is_chosen_by_reliability_tier(self) -> None:
        """When two RAW claims agree, the more-reliable source wins."""
        uow = InMemoryUnitOfWork()
        event = _seed_event(uow)
        _seed_projection(uow, event.id, fields={"operator": "ABC Airlines"})

        ntsb = _seed_source(uow, name="NTSB", tier=1)  # most trusted
        wire = _seed_source(uow, name="Wire", tier=5)
        _seed_claim(uow, event.id, ntsb, field_name="operator", field_value="ABC Airlines")
        _seed_claim(uow, event.id, wire, field_name="operator", field_value="ABC Airlines")

        result = await GetFieldExplanation(uow).execute(event.id, "operator")
        assert result.winner is not None
        assert result.winner.source_name == "NTSB"
        # Plain-English explanation references reliability.
        assert "reliab" in result.winner.plain_english.lower()

    async def test_manual_override_wins_over_raw(self) -> None:
        uow = InMemoryUnitOfWork()
        event = _seed_event(uow)
        _seed_projection(uow, event.id, fields={"operator": "ABC Airlines"})
        ntsb = _seed_source(uow, name="NTSB", tier=1)
        editor_source = _seed_source(uow, name="Editor", kind=SourceKind.INTERNAL, tier=999)
        _seed_claim(uow, event.id, ntsb, field_name="operator", field_value="ABC Airlines")
        _seed_claim(
            uow,
            event.id,
            editor_source,
            field_name="operator",
            field_value="ABC Airlines",
            claim_type=ClaimType.MANUAL_OVERRIDE,
        )

        result = await GetFieldExplanation(uow).execute(event.id, "operator")
        assert result.winner is not None
        # Override beats NTSB even though NTSB is more reliable.
        assert result.winner.source_name == "Editor"
        assert "manual" in result.winner.plain_english.lower() or (
            "editor" in result.winner.plain_english.lower()
        )

    async def test_losers_listed_with_explanations(self) -> None:
        uow = InMemoryUnitOfWork()
        event = _seed_event(uow)
        _seed_projection(uow, event.id, fields={"operator": "ABC Airlines"})
        ntsb = _seed_source(uow, name="NTSB", tier=1)
        wrong = _seed_source(uow, name="Outdated", tier=3)
        _seed_claim(uow, event.id, ntsb, field_name="operator", field_value="ABC Airlines")
        _seed_claim(
            uow,
            event.id,
            wrong,
            field_name="operator",
            field_value="XYZ Airlines",
        )

        result = await GetFieldExplanation(uow).execute(event.id, "operator")
        loser_sources = [loser.source_name for loser in result.losers]
        assert "Outdated" in loser_sources
        # The different-value loser explanation mentions disagreement.
        outdated = next(r for r in result.losers if r.source_name == "Outdated")
        assert (
            "different" in outdated.plain_english.lower()
            or "disagree" in outdated.plain_english.lower()
        )

    async def test_expert_mode_includes_claim_ids_and_tiers(self) -> None:
        uow = InMemoryUnitOfWork()
        event = _seed_event(uow)
        _seed_projection(uow, event.id, fields={"operator": "ABC Airlines"})
        source = _seed_source(uow, tier=1)
        claim = _seed_claim(
            uow, event.id, source, field_name="operator", field_value="ABC Airlines"
        )

        summary = await GetFieldExplanation(uow).execute(event.id, "operator")
        assert summary.winner is not None
        assert summary.winner.expert is None

        expert = await GetFieldExplanation(uow, expert=True).execute(event.id, "operator")
        assert expert.winner is not None
        assert expert.winner.expert is not None
        assert expert.winner.expert.claim_id == claim.id
        assert expert.winner.expert.source_reliability_tier == 1

    async def test_unknown_field_returns_not_found(self) -> None:
        """Probing for fields not in the projection must 404."""
        uow = InMemoryUnitOfWork()
        event = _seed_event(uow)
        _seed_projection(uow, event.id, fields={"operator": "X"})
        with pytest.raises(NotFoundError):
            await GetFieldExplanation(uow).execute(event.id, "secret_field")

    async def test_truncates_losers_at_cap(self, monkeypatch) -> None:
        from atlas.application.use_cases import audit as audit_module

        monkeypatch.setattr(audit_module, "_MAX_LOSING_CLAIMS", 3)
        uow = InMemoryUnitOfWork()
        event = _seed_event(uow)
        _seed_projection(uow, event.id, fields={"operator": "ABC Airlines"})
        winner_source = _seed_source(uow, name="NTSB", tier=1)
        _seed_claim(
            uow,
            event.id,
            winner_source,
            field_name="operator",
            field_value="ABC Airlines",
        )
        # 5 losers — should be truncated to 3.
        for i in range(5):
            s = _seed_source(uow, name=f"Other-{i}", tier=2 + i)
            _seed_claim(
                uow,
                event.id,
                s,
                field_name="operator",
                field_value=f"Wrong {i}",
                created_at=datetime(2024, 6, 1, i + 1, tzinfo=UTC),
            )
        result = await GetFieldExplanation(uow).execute(event.id, "operator")
        assert len(result.losers) == 3
        assert result.losers_truncated is True

    async def test_open_conflict_surfaces_in_explanation(self) -> None:
        uow = InMemoryUnitOfWork()
        event = _seed_event(uow)
        _seed_projection(
            uow,
            event.id,
            fields={"operator": "ABC Airlines"},
            unresolved=["operator"],
        )
        # Add a conflict row.
        conflict = ClaimConflict(
            event_id=event.id,
            field_name="operator",
            status=ConflictStatus.OPEN,
            last_modified_reason=ConflictModifierReason.INITIAL,
        )
        uow.store.conflicts[conflict.id] = conflict
        source = _seed_source(uow)
        _seed_claim(
            uow,
            event.id,
            source,
            field_name="operator",
            field_value="ABC Airlines",
        )

        result = await GetFieldExplanation(uow).execute(event.id, "operator")
        assert result.conflict is not None
        assert result.conflict.status == "OPEN"
        assert (
            "disagree" in result.conflict.plain_english.lower()
            or "review" in result.conflict.plain_english.lower()
        )


# ── Claim explanation ───────────────────────────────────────────────────────


class TestGetClaimExplanation:
    async def test_winning_claim_is_flagged(self) -> None:
        uow = InMemoryUnitOfWork()
        event = _seed_event(uow)
        _seed_projection(uow, event.id, fields={"operator": "ABC Airlines"})
        source = _seed_source(uow, name="NTSB", tier=1)
        claim = _seed_claim(
            uow,
            event.id,
            source,
            field_name="operator",
            field_value="ABC Airlines",
        )
        result = await GetClaimExplanation(uow).execute(claim.id)
        assert result.is_winning is True
        assert result.is_active is True
        assert result.is_superseded is False

    async def test_superseded_claim_is_flagged(self) -> None:
        uow = InMemoryUnitOfWork()
        event = _seed_event(uow)
        _seed_projection(uow, event.id, fields={"operator": "ABC Airlines"})
        source = _seed_source(uow)
        claim = _seed_claim(
            uow,
            event.id,
            source,
            field_name="operator",
            field_value="Old Operator",
            claim_type=ClaimType.SUPERSEDED,
        )
        result = await GetClaimExplanation(uow).execute(claim.id)
        assert result.is_superseded is True
        assert result.is_winning is False
        # Prose reflects the superseded role.
        assert "replaced" in result.plain_english.lower() or (
            "no longer" in result.plain_english.lower()
        )

    async def test_loser_claim_not_winning(self) -> None:
        """A RAW claim whose value differs from the projection isn't winning."""
        uow = InMemoryUnitOfWork()
        event = _seed_event(uow)
        _seed_projection(uow, event.id, fields={"operator": "ABC Airlines"})
        good_source = _seed_source(uow, name="NTSB", tier=1)
        _seed_claim(
            uow,
            event.id,
            good_source,
            field_name="operator",
            field_value="ABC Airlines",
        )
        bad_source = _seed_source(uow, name="Bad", tier=5)
        bad_claim = _seed_claim(
            uow,
            event.id,
            bad_source,
            field_name="operator",
            field_value="WRONG",
        )
        result = await GetClaimExplanation(uow).execute(bad_claim.id)
        assert result.is_winning is False
        assert result.is_active is True

    async def test_history_rows_returned_in_order(self) -> None:
        uow = InMemoryUnitOfWork()
        event = _seed_event(uow)
        _seed_projection(uow, event.id, fields={"operator": "ABC Airlines"})
        source = _seed_source(uow)
        claim = _seed_claim(
            uow, event.id, source, field_name="operator", field_value="ABC Airlines"
        )
        # Append two history rows out of order.
        h1 = ClaimHistory(
            claim_id=claim.id,
            event_id=event.id,
            to_claim_type=ClaimType.RAW,
            action="created",
            reason="initial",
            modifier_type=ModifierType.INGESTION,
            created_at=datetime(2024, 6, 1, tzinfo=UTC),
        )
        h2 = ClaimHistory(
            claim_id=claim.id,
            event_id=event.id,
            from_claim_type=ClaimType.RAW,
            to_claim_type=ClaimType.CONFIRMED,
            action="updated",
            reason="confirmed by editor",
            modifier_type=ModifierType.USER,
            created_at=datetime(2024, 7, 1, tzinfo=UTC),
        )
        # Insert in reverse to ensure sort is by the use case.
        uow.store.claim_history.extend([h2, h1])

        result = await GetClaimExplanation(uow).execute(claim.id)
        actions = [h.action for h in result.history]
        assert actions == ["created", "updated"]

    async def test_missing_claim_returns_not_found(self) -> None:
        uow = InMemoryUnitOfWork()
        with pytest.raises(NotFoundError):
            await GetClaimExplanation(uow).execute(uuid4())


# ── Source verification ─────────────────────────────────────────────────────


class TestGetSourceVerification:
    async def test_returns_hash_and_recipe(self) -> None:
        uow = InMemoryUnitOfWork()
        source = _seed_source(uow, name="NTSB")
        snap = RawSnapshot(
            source_id=source.id,
            ingestion_run_id=uuid4(),
            payload_hash="deadbeef",
            payload_json={"any": "thing"},
            captured_at=datetime(2024, 6, 1, tzinfo=UTC),
            raw_payload_hash=("0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"),
            source_record_id="NTSB-ABC-123",
        )
        uow.store.snapshots[snap.id] = snap

        result = await GetSourceVerification(uow).execute(snap.id)
        # The verifiable hash is exposed.
        assert result.raw_payload_hash == snap.raw_payload_hash
        # Recipe is documented and versioned.
        assert result.recipe_version  # nonempty
        assert len(result.recipe_steps) >= 3
        # Non-redistribution note present.
        assert "redistribut" in result.verification_note.lower() or (
            "fetch" in result.verification_note.lower()
        )

    async def test_payload_is_not_exposed(self) -> None:
        """The verification response must never carry the raw payload."""
        from atlas.presentation.api.schemas.audit import (
            SourceVerificationResponse as Schema,
        )

        uow = InMemoryUnitOfWork()
        source = _seed_source(uow)
        snap = RawSnapshot(
            source_id=source.id,
            ingestion_run_id=uuid4(),
            payload_hash="x",
            payload_json={"sensitive": "payload"},
            captured_at=datetime(2024, 6, 1, tzinfo=UTC),
            raw_payload_hash="abc123",
        )
        uow.store.snapshots[snap.id] = snap

        result = await GetSourceVerification(uow).execute(snap.id)
        # The Pydantic schema has a fixed set of fields — confirm
        # payload_json is not one of them.  This is the
        # whitelist-by-construction contract.
        schema_fields = set(Schema.model_fields.keys())
        assert "payload_json" not in schema_fields
        assert "payload" not in schema_fields
        # And confirm the dataclass doesn't carry it either.
        assert not hasattr(result, "payload_json")
        assert not hasattr(result, "payload")

    async def test_missing_snapshot_returns_not_found(self) -> None:
        uow = InMemoryUnitOfWork()
        with pytest.raises(NotFoundError):
            await GetSourceVerification(uow).execute(uuid4())

    async def test_missing_hash_returns_none_not_error(self) -> None:
        """Older snapshots predating the hash column should still
        return a response, just with ``raw_payload_hash=None``.

        Refusing to serve a verification view for legacy data would
        be worse than serving one that says "no hash on file".
        """
        uow = InMemoryUnitOfWork()
        source = _seed_source(uow)
        snap = RawSnapshot(
            source_id=source.id,
            ingestion_run_id=uuid4(),
            payload_hash="x",
            payload_json={"any": "thing"},
            captured_at=datetime(2024, 6, 1, tzinfo=UTC),
            raw_payload_hash=None,
        )
        uow.store.snapshots[snap.id] = snap
        result = await GetSourceVerification(uow).execute(snap.id)
        assert result.raw_payload_hash is None
        # Recipe still present so the consumer can act when a hash
        # is filled in later.
        assert result.recipe_version


# Silence unused-import warnings for symbols re-bound only as type
# narrowing aids to readers.
_ = cast
