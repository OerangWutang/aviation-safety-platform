"""Tests for MergeDuplicateEvents and ReviewDuplicate use cases."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from atlas.application.dto import IngestionClaimDTO
from atlas.application.ingestion._event_resolution import EventResolutionService
from atlas.application.use_cases.ingest_source_data import IngestSourceData
from atlas.application.use_cases.merge_duplicate_events import MergeDuplicateEvents
from atlas.application.use_cases.review_duplicate import ReviewDuplicate
from atlas.domain.entities import (
    AccidentEvent,
    EventIdentityIndex,
    PendingDuplicateReview,
    Source,
)
from atlas.domain.enums import (
    ClaimType,
    ConflictModifierReason,
    ConflictStatus,
    DuplicateReviewStatus,
    SourceKind,
)
from atlas.domain.exceptions import (
    CannotMergeIntoSelfError,
    DomainValidationError,
    EventAlreadyMergedError,
    EventNotFoundError,
    ReviewAlreadyResolvedError,
    ReviewNotFoundError,
)
from tests.domain._fake_uow import InMemoryUnitOfWork, make_settings

pytestmark = pytest.mark.asyncio


@pytest.fixture
def uow() -> InMemoryUnitOfWork:
    return InMemoryUnitOfWork()


async def _setup_two_events(uow: InMemoryUnitOfWork):
    src = Source(id=uuid4(), name="Src", kind=SourceKind.EXTERNAL, reliability_tier=1)
    await uow.sources.add(src)
    event_a = AccidentEvent(id=uuid4())
    event_b = AccidentEvent(id=uuid4())
    await uow.events.add(event_a)
    await uow.events.add(event_b)
    for event, val in [(event_a, "2024-06-01"), (event_b, "2024-06-02")]:
        eid = await IngestSourceData(uow, make_settings()).execute(
            source_id=src.id,
            raw_payload={"r": val},
            ingestion_run_id=uuid4(),
            claims_data=[IngestionClaimDTO(field_name="event_date", field_value=val)],
            event_id=event.id,
        )
        assert eid == event.id
    return src, event_a, event_b


# ── MergeDuplicateEvents ──────────────────────────────────────────────────────


async def test_merge_reproduces_source_claims_on_target(uow):
    _src, event_a, event_b = await _setup_two_events(uow)
    result = await MergeDuplicateEvents(uow).execute(
        source_event_id=event_b.id,
        target_event_id=event_a.id,
        resolved_by=uuid4(),
        note="Confirmed duplicate",
    )
    assert result.source_event_id == event_b.id
    assert result.target_event_id == event_a.id
    assert result.claims_transferred == 1
    target_active = [
        c
        for c in uow.store.claims.values()
        if c.event_id == event_a.id and c.claim_type == ClaimType.RAW
    ]
    assert len(target_active) == 2


async def test_merge_preserves_transferred_claim_created_at(uow):
    _src, event_a, event_b = await _setup_two_events(uow)
    source_claim = next(c for c in uow.store.claims.values() if c.event_id == event_b.id)
    original_created_at = datetime.now(UTC) - timedelta(days=30)
    source_claim.created_at = original_created_at

    await MergeDuplicateEvents(uow).execute(
        source_event_id=event_b.id, target_event_id=event_a.id, resolved_by=uuid4()
    )

    transferred = [
        c
        for c in uow.store.claims.values()
        if c.event_id == event_a.id and c.raw_snapshot_id == source_claim.raw_snapshot_id
    ]
    assert len(transferred) == 1
    assert transferred[0].created_at == original_created_at


async def test_merge_supersedes_source_claims(uow):
    _src, event_a, event_b = await _setup_two_events(uow)
    await MergeDuplicateEvents(uow).execute(
        source_event_id=event_b.id, target_event_id=event_a.id, resolved_by=uuid4()
    )
    source_claims = [c for c in uow.store.claims.values() if c.event_id == event_b.id]
    assert all(c.claim_type == ClaimType.SUPERSEDED for c in source_claims)


async def test_merge_marks_source_event_as_merged(uow):
    _src, event_a, event_b = await _setup_two_events(uow)
    await MergeDuplicateEvents(uow).execute(
        source_event_id=event_b.id, target_event_id=event_a.id, resolved_by=uuid4()
    )
    assert uow.store.events[event_b.id].merged_into_event_id == event_a.id


async def test_merge_queues_outbox_event_for_target(uow):
    _src, event_a, event_b = await _setup_two_events(uow)
    await MergeDuplicateEvents(uow).execute(
        source_event_id=event_b.id, target_event_id=event_a.id, resolved_by=uuid4()
    )
    merge_outbox = [
        e for e in uow.store.outbox if e.aggregate_id == event_a.id and "merged_from" in e.payload
    ]
    assert len(merge_outbox) == 1


async def test_merge_unions_identity_index_into_target(uow):
    source_id = uuid4()
    target_id = uuid4()
    await uow.events.add(AccidentEvent(id=source_id))
    await uow.events.add(AccidentEvent(id=target_id))

    await uow.identity_index.upsert(
        EventIdentityIndex(
            event_id=source_id,
            event_date_norm="2020-06-15",
            registration_norm="xyzabc",
            operator_norm="alpha air",
            registration_norms=["xyzabc"],
            source_record_ids=["src-record-1"],
        )
    )
    await uow.identity_index.upsert(
        EventIdentityIndex(
            event_id=target_id,
            event_date_norm="2020-06-15",
            registration_norm="pqrdef",
            operator_norm=None,
            registration_norms=["pqrdef"],
            source_record_ids=["src-record-2"],
        )
    )

    await MergeDuplicateEvents(uow).execute(
        source_event_id=source_id,
        target_event_id=target_id,
        resolved_by=uuid4(),
    )

    target_entry = uow.store.identity_index[target_id]
    assert target_entry.registration_norm == "pqrdef"
    assert target_entry.operator_norm == "alpha air"
    assert target_entry.event_date_norm == "2020-06-15"
    assert "xyzabc" in target_entry.registration_norms
    assert "pqrdef" in target_entry.registration_norms
    assert "src-record-1" in target_entry.source_record_ids
    assert "src-record-2" in target_entry.source_record_ids


async def test_future_lookup_finds_target_by_absorbed_source_alias_after_merge(uow):
    event_a_id = uuid4()
    event_b_id = uuid4()
    await uow.events.add(AccidentEvent(id=event_a_id))
    await uow.events.add(AccidentEvent(id=event_b_id))

    await uow.identity_index.upsert(
        EventIdentityIndex(
            event_id=event_a_id,
            event_date_norm="2020-06-15",
            registration_norm="abc123",
            registration_norms=["abc123"],
            source_record_ids=["a-rec"],
        )
    )
    await uow.identity_index.upsert(
        EventIdentityIndex(
            event_id=event_b_id,
            event_date_norm="2020-06-15",
            registration_norm="xyz789",
            registration_norms=["xyz789"],
            source_record_ids=["b-rec"],
        )
    )

    await MergeDuplicateEvents(uow).execute(
        source_event_id=event_b_id,
        target_event_id=event_a_id,
        resolved_by=uuid4(),
    )

    a_entry = uow.store.identity_index[event_a_id]
    assert "xyz789" in a_entry.registration_norms

    found = await uow.identity_index.find_by_registration("xyz789")
    found_ids = {entry.event_id for entry in found}
    assert event_a_id in found_ids
    assert event_b_id in found_ids, (
        "Merged source rows intentionally remain searchable as historical aliases; "
        "EventResolutionService canonicalizes them before writing claims."
    )


async def test_alias_path_enriches_canonical_row_without_clobbering_scalars(uow):
    source_event_id = uuid4()
    canonical_event_id = uuid4()
    await uow.events.add(
        AccidentEvent(
            id=source_event_id,
            merged_into_event_id=canonical_event_id,
        )
    )
    await uow.events.add(AccidentEvent(id=canonical_event_id))

    await uow.identity_index.upsert(
        EventIdentityIndex(
            event_id=source_event_id,
            event_date_norm="2020-06-15",
            registration_norm="old999",
            registration_norms=["old999"],
        )
    )
    await uow.identity_index.upsert(
        EventIdentityIndex(
            event_id=canonical_event_id,
            event_date_norm="2020-06-15",
            registration_norm="new001",
            operator_norm=None,
            registration_norms=["new001"],
        )
    )

    event, review, created, attached_by = await EventResolutionService(uow).resolve(
        source_id=uuid4(),
        claims_data_fields={
            "event_date": "2020-06-15",
            "registration": "OLD-999",
            "operator": "Gamma Airlines",
        },
        ingestion_run_id=uuid4(),
        source_record_id="new-source-rec-3",
    )

    assert event.id == canonical_event_id
    assert review is None
    assert created is False
    assert attached_by == "identity_match"

    canonical_entry = uow.store.identity_index[canonical_event_id]
    assert canonical_entry.registration_norm == "new001"
    assert canonical_entry.operator_norm == "gamma airlines"
    assert "old999" in canonical_entry.registration_norms
    assert "new-source-rec-3" in canonical_entry.source_record_ids


async def test_merge_into_self_raises(uow):
    _src, event_a, _b = await _setup_two_events(uow)
    with pytest.raises(CannotMergeIntoSelfError):
        await MergeDuplicateEvents(uow).execute(
            source_event_id=event_a.id, target_event_id=event_a.id, resolved_by=uuid4()
        )


async def test_merge_unknown_source_raises(uow):
    _src, event_a, _b = await _setup_two_events(uow)
    with pytest.raises(EventNotFoundError):
        await MergeDuplicateEvents(uow).execute(
            source_event_id=uuid4(), target_event_id=event_a.id, resolved_by=uuid4()
        )


async def test_merge_already_merged_source_raises(uow):
    _src, event_a, event_b = await _setup_two_events(uow)
    event_b.merged_into_event_id = event_a.id
    uow.store.events[event_b.id] = event_b
    with pytest.raises(EventAlreadyMergedError):
        await MergeDuplicateEvents(uow).execute(
            source_event_id=event_b.id, target_event_id=event_a.id, resolved_by=uuid4()
        )


async def test_merge_into_merged_target_raises(uow):
    _src, event_a, event_b = await _setup_two_events(uow)
    event_a.merged_into_event_id = uuid4()
    uow.store.events[event_a.id] = event_a
    with pytest.raises(EventAlreadyMergedError):
        await MergeDuplicateEvents(uow).execute(
            source_event_id=event_b.id, target_event_id=event_a.id, resolved_by=uuid4()
        )


async def test_merge_resolves_pending_review(uow):
    _src, event_a, event_b = await _setup_two_events(uow)
    curator = uuid4()
    review = PendingDuplicateReview(
        id=uuid4(),
        event_id_a=event_a.id,
        event_id_b=event_b.id,
        status=DuplicateReviewStatus.PENDING,
        match_score=0.6,
        matched_fields=["event_date"],
    )
    await uow.duplicate_reviews.add(review)
    await MergeDuplicateEvents(uow).execute(
        source_event_id=event_b.id, target_event_id=event_a.id, resolved_by=curator
    )
    updated = uow.store.duplicate_reviews[review.id]
    assert updated.status == DuplicateReviewStatus.MERGED
    assert updated.resolved_by == curator


async def test_merge_writes_claim_history(uow):
    _src, event_a, event_b = await _setup_two_events(uow)
    pre = len(uow.store.claim_history)
    await MergeDuplicateEvents(uow).execute(
        source_event_id=event_b.id, target_event_id=event_a.id, resolved_by=uuid4()
    )
    added = len(uow.store.claim_history) - pre
    # 1 "merged" entry for new claim on target + 1 "superseded" for old claim on source
    assert added == 2


# ── ReviewDuplicate ───────────────────────────────────────────────────────────


async def _setup_review(uow):
    _src, event_a, event_b = await _setup_two_events(uow)
    review = PendingDuplicateReview(
        id=uuid4(),
        event_id_a=event_a.id,
        event_id_b=event_b.id,
        status=DuplicateReviewStatus.PENDING,
        match_score=0.55,
        matched_fields=["event_date"],
    )
    await uow.duplicate_reviews.add(review)
    return event_a, event_b, review


async def test_review_confirm_triggers_merge(uow):
    event_a, event_b, review = await _setup_review(uow)
    curator = uuid4()
    await ReviewDuplicate(uow).execute(
        review_id=review.id, action="CONFIRM", resolved_by=curator, note="Confirmed same crash"
    )
    # Default: absorb event_b (event_id_b) into event_a (event_id_a)
    assert uow.store.events[event_b.id].merged_into_event_id == event_a.id
    updated = uow.store.duplicate_reviews[review.id]
    assert updated.status == DuplicateReviewStatus.MERGED


async def test_review_reject_does_not_merge(uow):
    event_a, event_b, review = await _setup_review(uow)
    await ReviewDuplicate(uow).execute(
        review_id=review.id, action="REJECT", resolved_by=uuid4(), note="Different"
    )
    updated = uow.store.duplicate_reviews[review.id]
    assert updated.status == DuplicateReviewStatus.REJECTED
    assert uow.store.events[event_a.id].merged_into_event_id is None
    assert uow.store.events[event_b.id].merged_into_event_id is None


async def test_review_not_found_raises(uow):
    with pytest.raises(ReviewNotFoundError):
        await ReviewDuplicate(uow).execute(review_id=uuid4(), action="CONFIRM", resolved_by=uuid4())


async def test_review_invalid_action_raises(uow):
    _event_a, _event_b, review = await _setup_review(uow)
    with pytest.raises(DomainValidationError):
        await ReviewDuplicate(uow).execute(
            review_id=review.id, action="APPROVE", resolved_by=uuid4()
        )


async def test_confirm_already_resolved_review_raises(uow):
    _event_a, _event_b, review = await _setup_review(uow)
    await ReviewDuplicate(uow).execute(review_id=review.id, action="REJECT", resolved_by=uuid4())
    with pytest.raises(ReviewAlreadyResolvedError):
        await ReviewDuplicate(uow).execute(
            review_id=review.id, action="CONFIRM", resolved_by=uuid4()
        )


async def test_reject_already_resolved_review_raises(uow):
    _event_a, _event_b, review = await _setup_review(uow)
    await ReviewDuplicate(uow).execute(review_id=review.id, action="REJECT", resolved_by=uuid4())
    with pytest.raises(ReviewAlreadyResolvedError):
        await ReviewDuplicate(uow).execute(
            review_id=review.id, action="REJECT", resolved_by=uuid4()
        )


# ── Merge reopens resolved conflicts on the target ────────────────────────────


async def _setup_two_sources(uow: InMemoryUnitOfWork):
    """Return (source_a, source_b) with different reliability tiers."""
    src_a = Source(id=uuid4(), name="SrcA", kind=SourceKind.EXTERNAL, reliability_tier=1)
    src_b = Source(id=uuid4(), name="SrcB", kind=SourceKind.EXTERNAL, reliability_tier=2)
    await uow.sources.add(src_a)
    await uow.sources.add(src_b)
    return src_a, src_b


async def test_merge_reopens_resolved_conflict_on_target(uow):
    """Merge transfers a claim that contradicts a resolved conflict on the target.

    Setup:
    - Event A has two claims for 'location' from two sources that disagreed.
    - That conflict was resolved with source_a winning (value='London').
    - Event B has a claim for 'location' with a different value ('Paris').
    - After merge B->A the new 'Paris' claim should reopen the conflict, not be silently ignored.
    """
    src_a, src_b = await _setup_two_sources(uow)

    # Create event A with a resolved conflict.
    event_a = AccidentEvent(id=uuid4())
    await uow.events.add(event_a)

    # Two claims on event A, different values -> conflict.
    run_a1 = uuid4()
    await IngestSourceData(uow, make_settings()).execute(
        source_id=src_a.id,
        raw_payload={"loc": "London"},
        ingestion_run_id=run_a1,
        claims_data=[IngestionClaimDTO(field_name="location", field_value="London")],
        event_id=event_a.id,
    )
    run_a2 = uuid4()
    await IngestSourceData(uow, make_settings()).execute(
        source_id=src_b.id,
        raw_payload={"loc": "Rome"},
        ingestion_run_id=run_a2,
        claims_data=[IngestionClaimDTO(field_name="location", field_value="Rome")],
        event_id=event_a.id,
    )

    # Resolve the conflict: pick the London claim.
    conflicts_on_a = [
        c
        for c in uow.store.conflicts.values()
        if c.event_id == event_a.id and c.field_name == "location"
    ]
    assert len(conflicts_on_a) == 1, "Expected one location conflict on event A"
    conflict = conflicts_on_a[0]
    london_claim = next(
        c
        for c in uow.store.claims.values()
        if c.event_id == event_a.id and c.field_name == "location" and c.field_value == "London"
    )
    from atlas.domain.enums import ConflictStatus

    conflict.status = ConflictStatus.RESOLVED
    conflict.winning_claim_id = london_claim.id
    uow.store.conflicts[conflict.id] = conflict

    # Create event B with a Paris claim.
    event_b = AccidentEvent(id=uuid4())
    await uow.events.add(event_b)
    run_b = uuid4()
    await IngestSourceData(uow, make_settings()).execute(
        source_id=src_a.id,
        raw_payload={"loc": "Paris"},
        ingestion_run_id=run_b,
        claims_data=[IngestionClaimDTO(field_name="location", field_value="Paris")],
        event_id=event_b.id,
    )

    # Merge event_b -> event_a.
    curator = uuid4()
    result = await MergeDuplicateEvents(uow).execute(
        source_event_id=event_b.id, target_event_id=event_a.id, resolved_by=curator
    )
    assert result.claims_transferred >= 1

    # The previously resolved conflict should now be OPEN again.
    reopened = uow.store.conflicts.get(conflict.id)
    assert reopened is not None
    assert reopened.status == ConflictStatus.OPEN, (
        f"Expected conflict to be OPEN after merge, got {reopened.status}"
    )
    assert reopened.winning_claim_id is None, "Reopened conflict must not retain old winner"


async def test_merge_result_is_dataclass(uow):
    """MergeResult must be a frozen dataclass (not a plain class)."""
    import dataclasses

    from atlas.application.use_cases.merge_duplicate_events import MergeResult

    assert dataclasses.is_dataclass(MergeResult), "MergeResult should be a dataclass"
    _src, event_a, event_b = await _setup_two_events(uow)
    result = await MergeDuplicateEvents(uow).execute(
        source_event_id=event_b.id, target_event_id=event_a.id, resolved_by=uuid4()
    )
    assert dataclasses.is_dataclass(result)
    assert result.target_event_id == event_a.id
    assert result.source_event_id == event_b.id
    assert result.claims_transferred >= 0


async def test_merge_deletes_absorbed_event_projection(uow):
    from atlas.application.use_cases.reproject_event import ReProjectEvent

    _src, event_a, event_b = await _setup_two_events(uow)
    await ReProjectEvent(uow).execute(event_b.id, commit=False)
    assert event_b.id in uow.store.projections

    await MergeDuplicateEvents(uow).execute(
        source_event_id=event_b.id,
        target_event_id=event_a.id,
        resolved_by=uuid4(),
    )

    assert event_b.id not in uow.store.projections


async def test_merge_tombstones_source_event_conflicts(uow):
    src_a, src_b = await _setup_two_sources(uow)
    target = AccidentEvent(id=uuid4())
    source = AccidentEvent(id=uuid4())
    await uow.events.add(target)
    await uow.events.add(source)

    await IngestSourceData(uow, make_settings()).execute(
        source_id=src_a.id,
        raw_payload={"loc": "Amsterdam"},
        ingestion_run_id=uuid4(),
        claims_data=[IngestionClaimDTO(field_name="location", field_value="Amsterdam")],
        event_id=source.id,
    )
    await IngestSourceData(uow, make_settings()).execute(
        source_id=src_b.id,
        raw_payload={"loc": "Rotterdam"},
        ingestion_run_id=uuid4(),
        claims_data=[IngestionClaimDTO(field_name="location", field_value="Rotterdam")],
        event_id=source.id,
    )

    conflict = next(c for c in uow.store.conflicts.values() if c.event_id == source.id)
    assert conflict.status == ConflictStatus.OPEN

    await MergeDuplicateEvents(uow).execute(
        source_event_id=source.id,
        target_event_id=target.id,
        resolved_by=uuid4(),
        note="confirmed duplicate",
    )

    tombstoned = uow.store.conflicts[conflict.id]
    assert tombstoned.status == ConflictStatus.RESOLVED
    # Merge tombstones close an orphaned conflict; they do not pick an
    # arbitrary claim as the winner.
    assert tombstoned.winning_claim_id is None
    assert tombstoned.last_modified_reason == ConflictModifierReason.SYSTEM_AUTO_CLOSED
    assert tombstoned.last_modified_note is not None
    assert "Event merged into" in tombstoned.last_modified_note
    assert not [
        c
        for c in uow.store.conflicts.values()
        if c.event_id == source.id and c.status == ConflictStatus.OPEN
    ]
    activity = [a for a in uow.store.conflict_activity if a.conflict_id == conflict.id]
    assert activity[-1].modifier_type.value == "SYSTEM"
    assert activity[-1].to_status == ConflictStatus.RESOLVED
    assert activity[-1].version_at_moment == tombstoned.version


async def test_merge_leaves_already_resolved_source_conflicts_untouched(uow):
    src_a, src_b = await _setup_two_sources(uow)
    target = AccidentEvent(id=uuid4())
    source = AccidentEvent(id=uuid4())
    await uow.events.add(target)
    await uow.events.add(source)

    await IngestSourceData(uow, make_settings()).execute(
        source_id=src_a.id,
        raw_payload={"loc": "Amsterdam"},
        ingestion_run_id=uuid4(),
        claims_data=[IngestionClaimDTO(field_name="location", field_value="Amsterdam")],
        event_id=source.id,
    )
    await IngestSourceData(uow, make_settings()).execute(
        source_id=src_b.id,
        raw_payload={"loc": "Rotterdam"},
        ingestion_run_id=uuid4(),
        claims_data=[IngestionClaimDTO(field_name="location", field_value="Rotterdam")],
        event_id=source.id,
    )

    conflict = next(c for c in uow.store.conflicts.values() if c.event_id == source.id)
    winner = conflict.claim_ids[0]
    conflict.resolve(winner, resolved_by=uuid4(), reason="curator picked winner")
    original_version = conflict.version
    original_updated_at = conflict.updated_at
    original_reason = conflict.last_modified_reason
    original_note = conflict.last_modified_note

    await MergeDuplicateEvents(uow).execute(
        source_event_id=source.id,
        target_event_id=target.id,
        resolved_by=uuid4(),
        note="confirmed duplicate",
    )

    resolved = uow.store.conflicts[conflict.id]
    assert resolved.status == ConflictStatus.RESOLVED
    assert resolved.winning_claim_id == winner
    assert resolved.version == original_version
    assert resolved.updated_at == original_updated_at
    assert resolved.last_modified_reason == original_reason
    assert resolved.last_modified_note == original_note
    system_merge_logs = [
        entry
        for entry in uow.store.conflict_activity
        if entry.conflict_id == conflict.id and entry.modifier_type.value == "SYSTEM"
    ]
    assert system_merge_logs == []
