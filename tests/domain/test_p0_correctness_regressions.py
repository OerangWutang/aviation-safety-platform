"""Regression tests for P0 correctness fixes.

P0.1a - projection_builder: resolved winner silently dropped when inactive
P0.1b - ingest: resolved conflict winner not updated after source-record correction
P0.2  - merge: superseded_by_claim_id pointed to wrong claim (first source claim)
P0.3  - identity: second known registration not searchable after conflicting ingestion
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from atlas.application.dto import IngestionClaimDTO
from atlas.application.use_cases.ingest_source_data import IngestSourceData
from atlas.application.use_cases.merge_duplicate_events import MergeDuplicateEvents
from atlas.application.use_cases.resolve_conflict import ResolveConflict
from atlas.domain.entities import (
    Claim,
    ClaimConflict,
    Source,
)
from atlas.domain.enums import (
    ClaimType,
    ConflictStatus,
    SourceKind,
)
from atlas.domain.services.projection_builder import ProjectionBuilder
from tests.domain._fake_uow import InMemoryUnitOfWork, make_settings

pytestmark = pytest.mark.asyncio


# ─── helpers ──────────────────────────────────────────────────────────────────


@pytest.fixture
def uow() -> InMemoryUnitOfWork:
    return InMemoryUnitOfWork()


async def _add_source(uow: InMemoryUnitOfWork, tier: int = 1) -> Source:
    src = Source(
        id=uuid4(),
        name=f"S-{uuid4().hex[:6]}",
        kind=SourceKind.EXTERNAL,
        reliability_tier=tier,
    )
    await uow.sources.add(src)
    return src


def _claims(**kw) -> list[IngestionClaimDTO]:
    defaults = {"event_date": "2024-06-01", "registration": "N123AB", "operator": "AirX"}
    defaults.update(kw)
    return [IngestionClaimDTO(field_name=k, field_value=v) for k, v in defaults.items()]


async def _ingest(uow, source, claims=None, *, source_record_id=None, event_id=None):
    return await IngestSourceData(uow, make_settings()).execute(
        source_id=source.id,
        raw_payload={"r": uuid4().hex},
        ingestion_run_id=uuid4(),
        claims_data=claims or _claims(),
        source_record_id=source_record_id,
        event_id=event_id,
    )


# ─── P0.1a: projection_builder silent winner omission ─────────────────────────


def _make_source_entity(tier: int = 1) -> Source:
    return Source(id=uuid4(), name=f"S{tier}", kind=SourceKind.EXTERNAL, reliability_tier=tier)


def _make_claim(event_id, source_id, field, value, claim_type=ClaimType.RAW) -> Claim:
    return Claim(
        event_id=event_id,
        source_id=source_id,
        field_name=field,
        field_value=value,
        claim_type=claim_type,
    )


async def test_projection_builder_does_not_drop_field_when_resolved_winner_is_superseded():
    """When a resolved conflict's winning claim is superseded, the projection
    must still include the field (via winner-policy fallback), not silently omit it.

    Regression for: projection_builder silently returned {} for the field when
    the resolved winner was no longer in field_claims (active claims only).
    """
    event_id = uuid4()
    source = _make_source_entity()

    # Two claims conflict; c1 wins the resolution.
    c1 = _make_claim(event_id, source.id, "operator", "AirlineA")
    c2 = _make_claim(event_id, source.id, "operator", "AirlineB")

    # c1 is later superseded by c3 (source-record correction).
    c3 = _make_claim(event_id, source.id, "operator", "AirlineC")
    c1_superseded = Claim(
        id=c1.id,
        event_id=event_id,
        source_id=source.id,
        field_name="operator",
        field_value="AirlineA",
        claim_type=ClaimType.SUPERSEDED,
        superseded_by_claim_id=c3.id,
    )

    resolved_conflict = ClaimConflict(
        event_id=event_id,
        field_name="operator",
        status=ConflictStatus.RESOLVED,
        winning_claim_id=c1.id,  # still points at superseded claim
        claim_ids=[c1.id, c2.id],
    )

    # The active claims passed to the builder exclude superseded claims.
    # c2 and c3 are the only active ones, but c2 is for a different source
    # value - here we just pass c3 to reflect "one active claim after correction".
    projection = ProjectionBuilder().build(
        event_id=event_id,
        claims=[c1_superseded, c2, c3],
        conflicts=[resolved_conflict],
        sources_by_id={source.id: source},
        projection_version=1,
    )

    # The field must be present; its value comes from winner_policy on c2/c3.
    assert "operator" in projection.fields, (
        "Resolved-winner supersession must NOT silently drop the field from the projection"
    )


async def test_projection_builder_uses_active_winner_from_policy_when_resolved_winner_gone():
    """With a single remaining active claim, winner policy picks it deterministically."""
    event_id = uuid4()
    source = _make_source_entity()

    winner_claim = _make_claim(event_id, source.id, "location", "Paris")
    superseded_winner = Claim(
        id=winner_claim.id,
        event_id=event_id,
        source_id=source.id,
        field_name="location",
        field_value="Paris",
        claim_type=ClaimType.SUPERSEDED,
        superseded_by_claim_id=uuid4(),  # some newer claim
    )
    replacement = _make_claim(event_id, source.id, "location", "Lyon")

    conflict = ClaimConflict(
        event_id=event_id,
        field_name="location",
        status=ConflictStatus.RESOLVED,
        winning_claim_id=winner_claim.id,
        claim_ids=[winner_claim.id],
    )

    projection = ProjectionBuilder().build(
        event_id=event_id,
        claims=[superseded_winner, replacement],
        conflicts=[conflict],
        sources_by_id={source.id: source},
        projection_version=1,
    )

    assert projection.fields.get("location") == "Lyon"


# ─── P0.1b: resolved conflict winner update after source-record correction ────


async def test_resolved_winner_updated_when_correction_provides_same_value(uow):
    """If a source record is corrected and the new claim has the same normalised
    value as the old winning claim, the conflict stays RESOLVED and winning_claim_id
    is updated to the new claim.
    """
    source_a = await _add_source(uow)
    source_b = await _add_source(uow)
    record_id = "REG-SAME-VAL-001"

    # Source A provides initial claim; Source B provides different value -> conflict.
    event_id = await _ingest(
        uow,
        source_a,
        claims=[IngestionClaimDTO(field_name="operator", field_value="AirlineX")],
        source_record_id=record_id,
    )
    await _ingest(
        uow,
        source_b,
        claims=[IngestionClaimDTO(field_name="operator", field_value="AirlineY")],
        event_id=event_id,
    )

    conflict = await uow.conflicts.find_open_by_event_field(event_id, "operator")
    assert conflict is not None, "Expected an open conflict to exist"

    # Curator resolves the conflict in favour of Source A's claim.
    source_a_claim = next(
        c
        for c in uow.store.claims.values()
        if c.event_id == event_id
        and c.field_name == "operator"
        and c.field_value == "AirlineX"
        and c.is_active
    )
    await ResolveConflict(uow).execute(
        conflict_id=conflict.id,
        expected_version=conflict.version,
        winning_claim_id=source_a_claim.id,
        current_user_id=uuid4(),
    )
    resolved = await uow.conflicts.get(conflict.id)
    assert resolved is not None
    assert resolved.status == ConflictStatus.RESOLVED
    assert resolved.winning_claim_id == source_a_claim.id

    # Source A now corrects the same record but with the SAME operator value.
    await _ingest(
        uow,
        source_a,
        claims=[IngestionClaimDTO(field_name="operator", field_value="AirlineX")],
        source_record_id=record_id,
    )

    refreshed = await uow.conflicts.get(conflict.id)
    assert refreshed is not None
    assert refreshed.status == ConflictStatus.RESOLVED, (
        "Conflict must stay RESOLVED when correction value matches the old winner"
    )
    # winning_claim_id should now point at the NEW Source A claim, not the old
    # superseded one.
    new_winner_claim = await uow.claims.get(refreshed.winning_claim_id)
    assert new_winner_claim is not None
    assert new_winner_claim.is_active, (
        "winning_claim_id must be updated to an active claim, not the superseded one"
    )
    assert new_winner_claim.field_value == "AirlineX"


async def test_resolved_winner_causes_conflict_reopen_when_correction_changes_value(uow):
    """A RESOLVED conflict must be reopened when:
    1. Source C adds a new claim (agreeing with the winner) AFTER resolution
    2. Then the winning source corrects to a DIFFERENT value
    so that active evidence now disagrees.

    ResolveConflict supersedes the losing claim, so the original loser is gone.
    The only way to create post-correction disagreement is a claim that arrived
    AFTER resolution and agrees with the original winner.
    """
    source_a = await _add_source(uow)
    source_b = await _add_source(uow)
    source_c = await _add_source(uow)
    record_id = "REG-REOPEN-001"

    event_id = await _ingest(
        uow,
        source_a,
        claims=[IngestionClaimDTO(field_name="operator", field_value="AirlineA")],
        source_record_id=record_id,
    )
    await _ingest(
        uow,
        source_b,
        claims=[IngestionClaimDTO(field_name="operator", field_value="AirlineB")],
        event_id=event_id,
    )

    conflict = await uow.conflicts.find_open_by_event_field(event_id, "operator")
    assert conflict is not None

    # Curator resolves in favour of Source A. ResolveConflict also supersedes
    # Source B's losing claim, so only AirlineA stays active.
    winner_claim = next(
        c
        for c in uow.store.claims.values()
        if c.event_id == event_id and c.field_value == "AirlineA" and c.is_active
    )
    await ResolveConflict(uow).execute(
        conflict_id=conflict.id,
        expected_version=conflict.version,
        winning_claim_id=winner_claim.id,
        current_user_id=uuid4(),
    )

    resolved = await uow.conflicts.get(conflict.id)
    assert resolved.status == ConflictStatus.RESOLVED

    # Source C ingests AFTER resolution with the SAME value as the winner.
    # No new conflict opens; conflict stays RESOLVED.
    await _ingest(
        uow,
        source_c,
        claims=[IngestionClaimDTO(field_name="operator", field_value="AirlineA")],
        event_id=event_id,
    )
    still_resolved = await uow.conflicts.get(conflict.id)
    assert still_resolved.status == ConflictStatus.RESOLVED, (
        "Source C agreeing with winner must not change conflict status"
    )

    # Now Source A corrects to a DIFFERENT value (AirlineC). After this:
    #   - AirlineA (Source A original, winner) is superseded
    #   - AirlineA (Source C, still active) disagrees with new AirlineC
    # Active claims: [AirlineC, AirlineA-from-C] -> two distinct values -> reopen.
    await _ingest(
        uow,
        source_a,
        claims=[IngestionClaimDTO(field_name="operator", field_value="AirlineC")],
        source_record_id=record_id,
    )

    reopened = await uow.conflicts.get(conflict.id)
    assert reopened is not None
    assert reopened.status == ConflictStatus.OPEN, (
        "Conflict must be REOPENED when the winning claim is superseded and "
        "remaining active claims disagree"
    )
    assert reopened.winning_claim_id is None, (
        "Reopened conflict must not retain the stale winning_claim_id"
    )


async def test_projection_after_resolved_winner_correction_still_has_field(uow):
    """After source-record correction replaces a resolved winner, the projection
    builder must still produce a value for that field (P0.1a + P0.1b together).
    """
    source_a = await _add_source(uow)
    source_b = await _add_source(uow)
    record_id = "REG-PROJ-001"

    event_id = await _ingest(
        uow,
        source_a,
        claims=[IngestionClaimDTO(field_name="operator", field_value="AirX")],
        source_record_id=record_id,
    )
    await _ingest(
        uow,
        source_b,
        claims=[IngestionClaimDTO(field_name="operator", field_value="AirY")],
        event_id=event_id,
    )

    conflict = await uow.conflicts.find_open_by_event_field(event_id, "operator")
    winner_claim = next(
        c
        for c in uow.store.claims.values()
        if c.event_id == event_id and c.field_value == "AirX" and c.is_active
    )
    await ResolveConflict(uow).execute(
        conflict_id=conflict.id,
        expected_version=conflict.version,
        winning_claim_id=winner_claim.id,
        current_user_id=uuid4(),
    )

    # Correction: same value (winner stays resolved, winning_claim_id updated).
    await _ingest(
        uow,
        source_a,
        claims=[IngestionClaimDTO(field_name="operator", field_value="AirX")],
        source_record_id=record_id,
    )

    # Build projection manually from in-memory state.
    active_claims = await uow.claims.find_active_by_event(event_id)
    all_conflicts = await uow.conflicts.find_by_event(event_id)
    sources_by_id = {s.id: s for s in uow.store.sources.values()}

    from atlas.domain.services.projection_builder import ProjectionBuilder

    projection = ProjectionBuilder().build(
        event_id=event_id,
        claims=active_claims,
        conflicts=all_conflicts,
        sources_by_id=sources_by_id,
        projection_version=1,
    )

    assert "operator" in projection.fields, (
        "Projection must include the operator field even after winner was replaced"
    )
    assert projection.fields["operator"] == "AirX"


# ─── P0.2: merge audit linkage ────────────────────────────────────────────────


async def test_merge_each_source_claim_superseded_by_its_own_target_claim(uow):
    """After a merge, every old source claim must have superseded_by_claim_id
    pointing to the corresponding new target-side claim, NOT to an arbitrary
    first source claim.

    Regression for: bulk_supersede(source_ids, by_claim_id=source_ids[0])
    which set all superseded_by_claim_id to source_ids[0] (a source claim, not
    a target claim).
    """
    source = await _add_source(uow)

    event_a = await _ingest(
        uow,
        source,
        claims=[
            IngestionClaimDTO(field_name="event_date", field_value="2024-06-01"),
            IngestionClaimDTO(field_name="operator", field_value="AirlineA"),
        ],
    )
    event_b = await _ingest(
        uow,
        source,
        claims=[
            IngestionClaimDTO(field_name="event_date", field_value="2024-06-02"),
            IngestionClaimDTO(field_name="registration", field_value="N999ZZ"),
        ],
    )

    await MergeDuplicateEvents(uow).execute(
        source_event_id=event_b,
        target_event_id=event_a,
        resolved_by=uuid4(),
    )

    source_claims = [c for c in uow.store.claims.values() if c.event_id == event_b]
    # All source claims must be superseded.
    assert all(c.claim_type == ClaimType.SUPERSEDED for c in source_claims)

    target_claim_ids = {c.id for c in uow.store.claims.values() if c.event_id == event_a}
    source_claim_ids = {c.id for c in source_claims}

    for sc in source_claims:
        assert sc.superseded_by_claim_id is not None, (
            f"Source claim {sc.id} ({sc.field_name}) has no superseded_by_claim_id"
        )
        assert sc.superseded_by_claim_id in target_claim_ids, (
            f"Source claim {sc.id} ({sc.field_name}) points to "
            f"{sc.superseded_by_claim_id} which is not a target claim"
        )
        assert sc.superseded_by_claim_id not in source_claim_ids, (
            f"Source claim {sc.id} ({sc.field_name}) points to another source "
            f"claim rather than its target-side replacement"
        )


async def test_merge_claim_history_superseded_by_points_to_target_claim(uow):
    """The ClaimHistory 'superseded' entries written during merge must record
    the new target claim id, not the first source claim id.
    """
    source = await _add_source(uow)

    event_a = await _ingest(
        uow,
        source,
        claims=[IngestionClaimDTO(field_name="operator", field_value="AirX")],
    )
    event_b = await _ingest(
        uow,
        source,
        claims=[IngestionClaimDTO(field_name="operator", field_value="AirY")],
    )

    pre_history_count = len(uow.store.claim_history)
    await MergeDuplicateEvents(uow).execute(
        source_event_id=event_b,
        target_event_id=event_a,
        resolved_by=uuid4(),
    )

    # The "superseded" history entries are for the source claims.
    superseded_entries = [
        h for h in uow.store.claim_history[pre_history_count:] if h.action == "superseded"
    ]
    assert len(superseded_entries) == 1  # one source claim

    # The claim referenced by that history entry must be on the target event.
    superseded_claim_id = superseded_entries[0].claim_id
    superseded_claim = uow.store.claims[superseded_claim_id]
    assert superseded_claim.event_id == event_b  # it IS the source claim

    # And its superseded_by_claim_id must point to the target event.
    replacement = uow.store.claims.get(superseded_claim.superseded_by_claim_id)
    assert replacement is not None
    assert replacement.event_id == event_a, (
        "superseded_by_claim_id must point to the target-event replacement, "
        f"but got event_id={replacement.event_id}"
    )


async def test_merge_multiple_claims_each_get_distinct_replacement(uow):
    """When merging an event with two active claims, each source claim must point
    to its own distinct replacement on the target - not the same target claim.
    """
    source = await _add_source(uow)

    event_a = await _ingest(
        uow,
        source,
        claims=[
            IngestionClaimDTO(field_name="event_date", field_value="2024-01-01"),
        ],
    )
    event_b = await _ingest(
        uow,
        source,
        claims=[
            IngestionClaimDTO(field_name="operator", field_value="AirZ"),
            IngestionClaimDTO(field_name="location", field_value="Oslo"),
        ],
    )

    await MergeDuplicateEvents(uow).execute(
        source_event_id=event_b,
        target_event_id=event_a,
        resolved_by=uuid4(),
    )

    source_claims = [c for c in uow.store.claims.values() if c.event_id == event_b]
    assert len(source_claims) == 2

    replacement_ids = {c.superseded_by_claim_id for c in source_claims}
    # Each source claim must point to a *distinct* replacement.
    assert len(replacement_ids) == 2, (
        "Two source claims must each supersede their own distinct target claim, "
        f"but got replacement_ids={replacement_ids}"
    )

    # Both replacements must be on the target event.
    for rid in replacement_ids:
        assert rid is not None
        target_claim = uow.store.claims.get(rid)
        assert target_claim is not None
        assert target_claim.event_id == event_a


# ─── P0.3: identity searchability ────────────────────────────────────────────


async def test_identity_index_accumulates_all_known_registrations(uow):
    """After two ingestions that give different registrations for the same event,
    the identity index must retain BOTH so either one can find the event.
    """
    source_a = await _add_source(uow)
    source_b = await _add_source(uow)

    # Source A seeds the event with OLD123.
    event_id = await _ingest(
        uow,
        source_a,
        claims=[
            IngestionClaimDTO(field_name="event_date", field_value="2024-09-01"),
            IngestionClaimDTO(field_name="registration", field_value="OLD123"),
            IngestionClaimDTO(field_name="operator", field_value="AirX"),
        ],
    )

    # Source B attaches to the same event with NEW123 (explicit event_id).
    await _ingest(
        uow,
        source_b,
        claims=[
            IngestionClaimDTO(field_name="event_date", field_value="2024-09-01"),
            IngestionClaimDTO(field_name="registration", field_value="NEW123"),
            IngestionClaimDTO(field_name="operator", field_value="AirX"),
        ],
        event_id=event_id,
    )

    entry = uow.store.identity_index[event_id]
    # Both registrations must be in the accumulator list.
    assert "old123" in entry.registration_norms, (
        f"OLD123 should be in registration_norms, got: {entry.registration_norms}"
    )
    assert "new123" in entry.registration_norms, (
        f"NEW123 should be in registration_norms, got: {entry.registration_norms}"
    )


async def test_anonymous_ingestion_for_conflicting_registration_creates_review(uow):
    """P0.3 + v4 alias-semantics regression: when an event has two active conflicting
    registrations (OLD123 primary, NEW123 added explicitly), a third anonymous ingestion
    for the non-primary OLD123 must create a *duplicate review*, not silently create a
    fresh unlinked event and not blindly auto-attach.

    v3 behaviour (fixed in v4): OLD123 scored as a full 1.0 registration match
    (it was in registration_norms list) -> score 0.85 -> AUTO-ATTACH to the event
    even though OLD123 may have been corrected away.

    v4 correct behaviour: OLD123 is a historical alias of the event whose primary
    is now NEW123.  It scores at 0.5 x 0.45 (alias half-weight) + 0.30 (date) +
    0.10 (operator) = 0.625, which is in the UNCERTAIN range -> new event created +
    PendingDuplicateReview queued.  A curator then confirms or rejects the link.
    """
    source_a = await _add_source(uow)
    source_b = await _add_source(uow)
    source_c = await _add_source(uow)

    # Source A seeds with OLD123 - this becomes the initial primary registration.
    event_id = await _ingest(
        uow,
        source_a,
        claims=[
            IngestionClaimDTO(field_name="event_date", field_value="2024-09-01"),
            IngestionClaimDTO(field_name="registration", field_value="OLD123"),
            IngestionClaimDTO(field_name="operator", field_value="AirX"),
        ],
    )

    # Source B attaches with NEW123 via explicit event_id.
    # The upsert sets registration_norm = "new123" (primary); "old123" moves to
    # the historical alias list registration_norms.
    await _ingest(
        uow,
        source_b,
        claims=[
            IngestionClaimDTO(field_name="event_date", field_value="2024-09-01"),
            IngestionClaimDTO(field_name="registration", field_value="NEW123"),
            IngestionClaimDTO(field_name="operator", field_value="AirX"),
        ],
        event_id=event_id,
    )

    entry = uow.store.identity_index[event_id]
    assert "old123" in entry.registration_norms
    assert "new123" in entry.registration_norms
    assert entry.registration_norm == "new123", (
        "NEW123 must be the primary registration after Source B upsert"
    )

    # Source C anonymously ingests OLD123 (the historical, non-primary alias).
    # Alias scoring: 0.5x0.45 + 0.30 + 0.10 = 0.625 -> UNCERTAIN -> review, not attach.
    result_event = await _ingest(
        uow,
        source_c,
        claims=[
            IngestionClaimDTO(field_name="event_date", field_value="2024-09-01"),
            IngestionClaimDTO(field_name="registration", field_value="OLD123"),
            IngestionClaimDTO(field_name="operator", field_value="AirX"),
        ],
    )

    assert result_event != event_id, (
        "Historical alias must NOT auto-attach; a new event must be created"
    )
    reviews = list(uow.store.duplicate_reviews.values())
    assert len(reviews) == 1, (
        "A PendingDuplicateReview must be queued so a curator can confirm the link"
    )
    assert event_id in (reviews[0].event_id_a, reviews[0].event_id_b), (
        "The review must reference the original canonical event"
    )


async def test_anonymous_ingestion_for_other_conflicting_registration_finds_existing_event(uow):
    """Symmetric test: ingestion for NEW123 (the non-primary alias) must also find
    the existing event rather than creating a duplicate.
    """
    source_a = await _add_source(uow)
    source_b = await _add_source(uow)
    source_c = await _add_source(uow)

    event_id = await _ingest(
        uow,
        source_a,
        claims=[
            IngestionClaimDTO(field_name="event_date", field_value="2024-09-02"),
            IngestionClaimDTO(field_name="registration", field_value="OLD123"),
            IngestionClaimDTO(field_name="operator", field_value="AirX"),
        ],
    )
    await _ingest(
        uow,
        source_b,
        claims=[
            IngestionClaimDTO(field_name="event_date", field_value="2024-09-02"),
            IngestionClaimDTO(field_name="registration", field_value="NEW123"),
            IngestionClaimDTO(field_name="operator", field_value="AirX"),
        ],
        event_id=event_id,
    )

    result_event = await _ingest(
        uow,
        source_c,
        claims=[
            IngestionClaimDTO(field_name="event_date", field_value="2024-09-02"),
            IngestionClaimDTO(field_name="registration", field_value="NEW123"),
            IngestionClaimDTO(field_name="operator", field_value="AirX"),
        ],
    )

    assert result_event == event_id
    assert len(uow.store.events) == 1


async def test_identity_fields_property_separates_primary_from_historical_aliases(uow):
    """EventIdentityIndex.fields must expose the primary registration as a scalar
    (for full-weight matching) and historical aliases under a separate
    ``registration_norms`` key (for half-weight matching in score_match).

    v3 behaviour: registration exposed as a list - all aliases scored at 1.0.
    v4 correct behaviour: primary scalar -> 1.0; historical list -> 0.5 x weight.
    """
    from atlas.domain.entities import EventIdentityIndex

    # When registration_norms contains only the primary, no historical key.
    entry_only_primary = EventIdentityIndex(
        event_id=uuid4(),
        event_date_norm="2024-09-01",
        registration_norm="old123",
        registration_norms=["old123"],
    )
    fields = entry_only_primary.fields
    assert fields["registration"] == "old123", "registration must be the scalar primary, not a list"
    assert "registration_norms" not in fields, (
        "registration_norms key must be absent when there are no historical aliases"
    )

    # When registration_norms contains both primary and a historical alias,
    # only the historical one appears under registration_norms.
    entry_with_alias = EventIdentityIndex(
        event_id=uuid4(),
        event_date_norm="2024-09-01",
        registration_norm="new123",
        registration_norms=["old123", "new123"],
    )
    fields = entry_with_alias.fields
    assert fields["registration"] == "new123", "registration must be the scalar primary (new123)"
    assert "registration_norms" in fields, (
        "registration_norms key must be present when historical aliases exist"
    )
    assert isinstance(fields["registration_norms"], list)
    assert "old123" in fields["registration_norms"], "historical alias must appear"
    assert "new123" not in fields["registration_norms"], (
        "primary must NOT appear in the historical alias list"
    )

    # No registration at all - neither key should appear.
    entry_no_reg = EventIdentityIndex(
        event_id=uuid4(),
        event_date_norm="2024-09-01",
        registration_norm=None,
        registration_norms=[],
    )
    assert "registration" not in entry_no_reg.fields
    assert "registration_norms" not in entry_no_reg.fields


# ─── v4 Fix 1: historical alias semantics ─────────────────────────────────────


async def test_corrected_away_registration_creates_review_not_auto_attach(uow):
    """After a source_record_id correction changes registration from OLD to NEW,
    a future anonymous ingestion for OLD must trigger a duplicate review, not
    silently auto-attach to the corrected event.

    v3 bug: OLD was still in registration_norms with 1.0 weight -> auto-attach.
    v4 fix: OLD is a historical alias -> 0.5 x weight -> UNCERTAIN -> review.
    """
    source_a = await _add_source(uow)
    source_b = await _add_source(uow)
    record_id = "REC-ALIAS-CORR-001"

    # Source A first ingests with OLD registration.
    event_id = await _ingest(
        uow,
        source_a,
        claims=[
            IngestionClaimDTO(field_name="event_date", field_value="2024-11-01"),
            IngestionClaimDTO(field_name="registration", field_value="OLD555"),
            IngestionClaimDTO(field_name="operator", field_value="AirX"),
        ],
        source_record_id=record_id,
    )

    # Source A corrects: same record_id, new registration NEW555.
    # OLD555 claim is superseded; identity index primary updates to new555.
    await _ingest(
        uow,
        source_a,
        claims=[
            IngestionClaimDTO(field_name="event_date", field_value="2024-11-01"),
            IngestionClaimDTO(field_name="registration", field_value="NEW555"),
            IngestionClaimDTO(field_name="operator", field_value="AirX"),
        ],
        source_record_id=record_id,
    )

    entry = uow.store.identity_index[event_id]
    assert entry.registration_norm == "new555", "primary must be the corrected registration"
    assert "old555" in entry.registration_norms, "corrected-away alias must be retained"
    assert "new555" in entry.registration_norms

    # Source B anonymously ingests OLD555 - the corrected-away registration.
    # Must create a review (UNCERTAIN score), not auto-attach.
    result = await _ingest(
        uow,
        source_b,
        claims=[
            IngestionClaimDTO(field_name="event_date", field_value="2024-11-01"),
            IngestionClaimDTO(field_name="registration", field_value="OLD555"),
            IngestionClaimDTO(field_name="operator", field_value="AirX"),
        ],
    )

    assert result != event_id, (
        "Corrected-away registration must NOT auto-attach to the corrected event"
    )
    reviews = list(uow.store.duplicate_reviews.values())
    assert len(reviews) == 1, "A duplicate review must be queued for curator confirmation"
    assert event_id in (reviews[0].event_id_a, reviews[0].event_id_b)


async def test_primary_registration_still_auto_attaches_after_alias_accumulation(uow):
    """The primary registration (registration_norm) must still score 1.0 and
    auto-attach even when historical aliases exist in registration_norms.
    """
    source_a = await _add_source(uow)
    source_b = await _add_source(uow)
    record_id = "REC-ALIAS-PRIMARY-001"

    # Source A: OLD777 (primary after first ingest).
    event_id = await _ingest(
        uow,
        source_a,
        claims=[
            IngestionClaimDTO(field_name="event_date", field_value="2024-11-02"),
            IngestionClaimDTO(field_name="registration", field_value="OLD777"),
            IngestionClaimDTO(field_name="operator", field_value="AirY"),
        ],
        source_record_id=record_id,
    )

    # Source A corrects to NEW777 - primary is now new777.
    await _ingest(
        uow,
        source_a,
        claims=[
            IngestionClaimDTO(field_name="event_date", field_value="2024-11-02"),
            IngestionClaimDTO(field_name="registration", field_value="NEW777"),
            IngestionClaimDTO(field_name="operator", field_value="AirY"),
        ],
        source_record_id=record_id,
    )

    # Source B ingests NEW777 (the primary). Full 1.0 weight -> HIGH_CONFIDENCE.
    result = await _ingest(
        uow,
        source_b,
        claims=[
            IngestionClaimDTO(field_name="event_date", field_value="2024-11-02"),
            IngestionClaimDTO(field_name="registration", field_value="NEW777"),
            IngestionClaimDTO(field_name="operator", field_value="AirY"),
        ],
    )

    assert result == event_id, (
        "Primary registration must still auto-attach at full confidence "
        "even when historical aliases exist"
    )
    assert len(uow.store.events) == 1
    assert len(uow.store.duplicate_reviews) == 0


async def test_alias_score_math_puts_match_in_review_band(uow):
    """Prove that the alias scoring places the total in UNCERTAIN_LOW..HIGH_CONFIDENCE.

    alias_contribution = 0.5 * WEIGHTS["registration"] = 0.5 * 0.45 = 0.225
    date_contribution  = 1.0 * WEIGHTS["event_date"]   = 1.0 * 0.30 = 0.300
    op_contribution    = 1.0 * WEIGHTS["operator"]     = 1.0 * 0.10 = 0.100
    total              = 0.225 + 0.300 + 0.100 = 0.625

    0.40 (UNCERTAIN_LOW) <= 0.625 < 0.75 (HIGH_CONFIDENCE) -> review band ✓
    """
    from atlas.domain.services.event_matching import (
        HIGH_CONFIDENCE,
        UNCERTAIN_LOW,
        score_match,
    )

    incoming = {
        "event_date": "2024-11-03",
        "registration": "HIST123",
        "operator": "AirZ",
    }
    # Candidate: primary is CURR999; HIST123 is a historical alias.
    candidate = {
        "event_date": "2024-11-03",
        "registration": "curr999",  # primary - no match with incoming
        "registration_norms": ["hist123"],  # historical alias - half-weight match
        "operator": "airz",
    }

    result = score_match(incoming, candidate)

    assert "registration_alias" in result.matched_fields, (
        "Historical alias must be recorded in matched_fields"
    )
    assert "registration" not in result.matched_fields, (
        "Primary registration must NOT be in matched_fields (no primary match)"
    )
    assert UNCERTAIN_LOW <= result.score < HIGH_CONFIDENCE, (
        f"Alias match score {result.score} must be in the review band "
        f"[{UNCERTAIN_LOW}, {HIGH_CONFIDENCE})"
    )


# ─── v4 Fix 2: identity candidate searchability (>50 same-date events) ────────


async def test_registration_alias_found_when_more_than_50_same_date_events(uow):
    """find_candidates returns at most 50 events.  If there are 60 events on the
    same date, the target event may fall outside the window.  find_by_registration
    guarantees the correct event is STILL found by querying on the registration
    field directly, bypassing the 50-row cap.

    Regression for: duplicate event silently created when the candidate pool
    was full (60+ events on one date) and the correct match was beyond row 50.
    """

    target_source = await _add_source(uow)

    # Seed the TARGET event FIRST so its updated_at is the oldest of all
    # same-date events.  find_candidates orders newest-first (limit 50), so
    # once 50 newer noise events exist the target falls outside the window.
    target_event_id = await _ingest(
        uow,
        target_source,
        claims=[
            IngestionClaimDTO(field_name="event_date", field_value="2024-12-01"),
            IngestionClaimDTO(field_name="registration", field_value="TARGET001"),
            IngestionClaimDTO(field_name="operator", field_value="TargetAir"),
        ],
    )

    # Seed 55 noise events AFTER the target so they have newer updated_at values
    # and displace the target from the top-50 date-window fetch.
    noise_sources = [await _add_source(uow) for _ in range(55)]
    for i, ns in enumerate(noise_sources):
        await _ingest(
            uow,
            ns,
            claims=[
                IngestionClaimDTO(field_name="event_date", field_value="2024-12-01"),
                IngestionClaimDTO(field_name="registration", field_value=f"NOISE{i:04d}"),
                # Unique operators ensure inter-noise score stays at 0.30 (date only),
                # below UNCERTAIN_LOW (0.40), so no spurious noise-to-noise reviews.
                IngestionClaimDTO(field_name="operator", field_value=f"NoiseAir{i:04d}"),
            ],
        )

    # Confirm the target falls outside the date-only top-50.
    date_candidates = await uow.identity_index.find_candidates(
        event_date_norm="2024-12-01", limit=50
    )
    date_candidate_ids = {c.event_id for c in date_candidates}
    assert target_event_id not in date_candidate_ids, (
        "Precondition: target event must be outside the top-50 date window "
        "for this test to prove the registration-lookup path"
    )

    # A new source ingests TARGET001 anonymously.
    # Without find_by_registration, the target is invisible -> new duplicate event.
    # With fix: find_by_registration picks it up -> HIGH_CONFIDENCE match -> attach.
    new_source = await _add_source(uow)
    result_event = await _ingest(
        uow,
        new_source,
        claims=[
            IngestionClaimDTO(field_name="event_date", field_value="2024-12-01"),
            IngestionClaimDTO(field_name="registration", field_value="TARGET001"),
            IngestionClaimDTO(field_name="operator", field_value="TargetAir"),
        ],
    )

    assert result_event == target_event_id, (
        f"Registration-based lookup must find the target event {target_event_id} "
        f"even though it is outside the top-50 date-only window. "
        f"Got {result_event} instead."
    )
    # Exactly 56 events (55 noise + 1 target); no extra duplicate was created.
    assert len(uow.store.events) == 56
    assert len(uow.store.duplicate_reviews) == 0
