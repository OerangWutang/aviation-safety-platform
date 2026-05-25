"""Tests for ingestion identity: idempotency, source_record_id, and event matching."""

from __future__ import annotations

from uuid import uuid4

import pytest

from atlas.application.dto import IngestionClaimDTO
from atlas.application.use_cases.ingest_source_data import IngestSourceData
from atlas.application.use_cases.merge_duplicate_events import MergeDuplicateEvents
from atlas.application.use_cases.query_accident import QueryAccidentPublicView
from atlas.domain.entities import AccidentEvent, EventIdentityIndex, ProjectedAccidentRecord, Source
from atlas.domain.enums import (
    ClaimType,
    ConflictModifierReason,
    ConflictStatus,
    DuplicateReviewStatus,
    SourceKind,
)
from atlas.domain.exceptions import (
    EventAlreadyMergedError,
    IdempotencyKeyPayloadMismatchError,
    SourceRecordEventMismatchError,
)
from tests.domain._fake_uow import InMemoryUnitOfWork, make_settings

pytestmark = pytest.mark.asyncio


@pytest.fixture
def uow() -> InMemoryUnitOfWork:
    return InMemoryUnitOfWork()


async def _add_source(uow: InMemoryUnitOfWork, tier: int = 1) -> Source:
    src = Source(
        id=uuid4(), name=f"S-{uuid4().hex[:6]}", kind=SourceKind.EXTERNAL, reliability_tier=tier
    )
    await uow.sources.add(src)
    return src


def _claims(date="2024-06-01", reg="N123AB", op="AirlineX"):
    return [
        IngestionClaimDTO(field_name="event_date", field_value=date),
        IngestionClaimDTO(field_name="registration", field_value=reg),
        IngestionClaimDTO(field_name="operator", field_value=op),
    ]


async def _ingest(
    uow,
    source,
    claims=None,
    *,
    run_id=None,
    event_id=None,
    source_record_id=None,
    idempotency_key=None,
):
    if run_id is None:
        run_id = uuid4()
    if claims is None:
        claims = _claims()
    return await IngestSourceData(uow, settings=make_settings()).execute(
        source_id=source.id,
        raw_payload={"r": uuid4().hex},
        ingestion_run_id=run_id,
        claims_data=claims,
        event_id=event_id,
        source_record_id=source_record_id,
    )


# ── idempotency_key derivation ────────────────────────────────────────────────


async def test_derive_ingestion_run_id_is_deterministic():
    source_id = uuid4()
    run1 = IngestSourceData.derive_ingestion_run_id(source_id, "my-key")
    run2 = IngestSourceData.derive_ingestion_run_id(source_id, "my-key")
    assert run1 == run2


async def test_derive_ingestion_run_id_differs_for_different_keys():
    source_id = uuid4()
    r1 = IngestSourceData.derive_ingestion_run_id(source_id, "key-A")
    r2 = IngestSourceData.derive_ingestion_run_id(source_id, "key-B")
    assert r1 != r2


async def test_derive_ingestion_run_id_differs_for_different_sources():
    key = "same-key"
    r1 = IngestSourceData.derive_ingestion_run_id(uuid4(), key)
    r2 = IngestSourceData.derive_ingestion_run_id(uuid4(), key)
    assert r1 != r2


# ── same request twice -> one event, one claim set ────────────────────────────


async def test_same_idempotency_key_and_payload_returns_same_event(uow):
    """Two calls with the same (source, idempotency_key, payload) -> one event."""
    source = await _add_source(uow)
    payload = {"incident": "abc"}
    claims = [IngestionClaimDTO(field_name="event_date", field_value="2024-01-01")]
    key = "idem-key-001"
    run_id = IngestSourceData.derive_ingestion_run_id(source.id, key)

    first = await IngestSourceData(uow, make_settings()).execute(
        source_id=source.id,
        raw_payload=payload,
        ingestion_run_id=run_id,
        claims_data=claims,
    )
    second = await IngestSourceData(uow, make_settings()).execute(
        source_id=source.id,
        raw_payload=payload,
        ingestion_run_id=run_id,
        claims_data=claims,
    )

    assert first == second, "Idempotent call must return the same event_id"
    # Only one event should exist
    assert len(uow.store.events) == 1
    # Only one snapshot (the dedup prevented a second insert)
    assert len(uow.store.snapshots) == 1
    # Claims are from the first ingestion only
    active_claims = [c for c in uow.store.claims.values() if c.event_id == first]
    assert len(active_claims) == 1


async def test_same_run_id_same_payload_returns_same_event(uow):
    """Same run_id AND same payload -> idempotent return of the first event_id."""
    source = await _add_source(uow)
    claims = [IngestionClaimDTO(field_name="event_date", field_value="2024-01-01")]
    run_id = uuid4()
    payload = {"v": 1}

    first = await IngestSourceData(uow, make_settings()).execute(
        source_id=source.id, raw_payload=payload, ingestion_run_id=run_id, claims_data=claims
    )
    second = await IngestSourceData(uow, make_settings()).execute(
        source_id=source.id, raw_payload=payload, ingestion_run_id=run_id, claims_data=claims
    )
    assert first == second
    assert len(uow.store.snapshots) == 1  # dedup fired


async def test_same_run_id_different_payload_is_rejected(uow):
    """Same source/run identity with different payload is a 409-style mismatch, not evidence."""
    source = await _add_source(uow)
    claims = [IngestionClaimDTO(field_name="event_date", field_value="2024-01-01")]
    run_id = uuid4()

    await IngestSourceData(uow, make_settings()).execute(
        source_id=source.id,
        raw_payload={"v": 1},
        ingestion_run_id=run_id,
        claims_data=claims,
    )

    with pytest.raises(IdempotencyKeyPayloadMismatchError):
        await IngestSourceData(uow, make_settings()).execute(
            source_id=source.id,
            raw_payload={"v": 2},
            ingestion_run_id=run_id,
            claims_data=claims,
        )

    assert len(uow.store.snapshots) == 1
    assert len(uow.store.claims) == 1


# ── source_record_id: re-ingestion attaches to original event ────────────────


async def test_source_record_id_reingestion_attaches_to_existing_event(uow):
    """Second ingestion of the same source_record_id attaches to the original event."""
    source = await _add_source(uow)
    record_id = "NTSB-2024-001"

    # First submission
    first_run = uuid4()
    first_event = await IngestSourceData(uow, make_settings()).execute(
        source_id=source.id,
        raw_payload={"version": 1},
        ingestion_run_id=first_run,
        claims_data=[IngestionClaimDTO(field_name="operator", field_value="AirlineX")],
        source_record_id=record_id,
    )

    # Updated submission - different payload, same source_record_id
    second_run = uuid4()
    second_event = await IngestSourceData(uow, make_settings()).execute(
        source_id=source.id,
        raw_payload={"version": 2},
        ingestion_run_id=second_run,
        claims_data=[IngestionClaimDTO(field_name="operator", field_value="AirlineY")],
        source_record_id=record_id,
    )

    assert first_event == second_event, (
        "Re-ingestion with the same source_record_id must attach to the original event"
    )

    event_claims = [c for c in uow.store.claims.values() if c.event_id == first_event]
    active = [c for c in event_claims if c.is_active]
    superseded = [c for c in event_claims if c.claim_type == ClaimType.SUPERSEDED]

    assert len(event_claims) == 2
    assert len(active) == 1
    assert active[0].field_name == "operator"
    assert active[0].field_value == "AirlineY"
    assert len(superseded) == 1
    assert superseded[0].field_value == "AirlineX"
    assert superseded[0].superseded_by_claim_id == active[0].id
    assert len(uow.store.conflicts) == 0


async def test_source_record_explicit_event_mismatch_is_rejected(uow):
    """A source-record correction must not be moved to a different explicit event."""
    source = await _add_source(uow)
    record_id = "REC-OWNER-1"

    original_event = await IngestSourceData(uow, make_settings()).execute(
        source_id=source.id,
        raw_payload={"record": record_id, "version": 1},
        ingestion_run_id=uuid4(),
        claims_data=[IngestionClaimDTO(field_name="operator", field_value="AirlineA")],
        source_record_id=record_id,
    )
    other_event = await IngestSourceData(uow, make_settings()).execute(
        source_id=source.id,
        raw_payload={"other": True},
        ingestion_run_id=uuid4(),
        claims_data=[IngestionClaimDTO(field_name="operator", field_value="AirlineB")],
    )

    with pytest.raises(SourceRecordEventMismatchError):
        await IngestSourceData(uow, make_settings()).execute(
            source_id=source.id,
            raw_payload={"record": record_id, "version": 2},
            ingestion_run_id=uuid4(),
            claims_data=[IngestionClaimDTO(field_name="operator", field_value="AirlineC")],
            event_id=other_event,
            source_record_id=record_id,
        )

    original_claims = [
        c
        for c in uow.store.claims.values()
        if c.event_id == original_event and c.field_name == "operator"
    ]
    assert [(c.field_value, c.claim_type) for c in original_claims] == [("AirlineA", ClaimType.RAW)]
    other_claims = [
        c.field_value
        for c in uow.store.claims.values()
        if c.event_id == other_event and c.field_name == "operator"
    ]
    assert other_claims == ["AirlineB"]


async def test_source_record_explicit_event_matches_owner_is_allowed(uow):
    """Supplying the correct explicit event_id is compatible with source-record updates."""
    source = await _add_source(uow)
    record_id = "REC-OWNER-2"

    event_id = await IngestSourceData(uow, make_settings()).execute(
        source_id=source.id,
        raw_payload={"record": record_id, "version": 1},
        ingestion_run_id=uuid4(),
        claims_data=[IngestionClaimDTO(field_name="operator", field_value="AirlineA")],
        source_record_id=record_id,
    )

    returned = await IngestSourceData(uow, make_settings()).execute(
        source_id=source.id,
        raw_payload={"record": record_id, "version": 2},
        ingestion_run_id=uuid4(),
        claims_data=[IngestionClaimDTO(field_name="operator", field_value="AirlineC")],
        event_id=event_id,
        source_record_id=record_id,
    )

    assert returned == event_id
    active_ops = [
        c.field_value
        for c in uow.store.claims.values()
        if c.event_id == event_id and c.field_name == "operator" and c.is_active
    ]
    assert active_ops == ["AirlineC"]


async def test_different_source_record_ids_create_different_events(uow):
    """Two source records with genuinely different identity fields -> two events.

    ``source_record_id`` is the source's own stable identifier for their record
    (re-ingestion detection), not a cross-source uniqueness key.  When the
    actual claim fields differ, the identity matcher correctly creates separate
    events regardless of whether source_record_ids differ.
    """
    source = await _add_source(uow)
    # Deliberately different registration + date -> no identity match possible.
    ev1 = await _ingest(
        uow,
        source,
        claims=[
            IngestionClaimDTO(field_name="event_date", field_value="2024-06-01"),
            IngestionClaimDTO(field_name="registration", field_value="N123AB"),
        ],
        source_record_id="REC-001",
    )
    ev2 = await _ingest(
        uow,
        source,
        claims=[
            IngestionClaimDTO(field_name="event_date", field_value="2024-07-15"),
            IngestionClaimDTO(field_name="registration", field_value="N456CD"),
        ],
        source_record_id="REC-002",
    )
    assert ev1 != ev2
    assert len(uow.store.events) == 2


# ── event matching: high-confidence -> attach ─────────────────────────────────


async def _seed_existing_event(uow, date="2024-06-01", reg="N123AB", op="AirlineX"):
    """Seed an existing event with an identity index entry so the matcher can find it.

    Previously seeded a projection, but projections are populated asynchronously
    and the matcher now queries the synchronous identity index.  This helper
    uses the actual IngestSourceData path so both the event and its identity
    index entry are created exactly as they would be in production.
    """
    source = Source(
        id=uuid4(), name=f"S-{uuid4().hex[:4]}", kind=SourceKind.EXTERNAL, reliability_tier=1
    )
    await uow.sources.add(source)
    claims = [
        IngestionClaimDTO(field_name="event_date", field_value=date),
        IngestionClaimDTO(field_name="registration", field_value=reg),
        IngestionClaimDTO(field_name="operator", field_value=op),
    ]
    event_id = await IngestSourceData(uow, make_settings()).execute(
        source_id=source.id,
        raw_payload={"seed": True},
        ingestion_run_id=uuid4(),
        claims_data=claims,
    )
    event = uow.store.events[event_id]
    return source, event


async def test_high_confidence_match_reuses_existing_event(uow):
    """Identical (date, registration, operator) -> high-confidence -> same event."""
    _existing_src, existing_event = await _seed_existing_event(
        uow, date="2024-06-01", reg="N123AB", op="AirlineX"
    )
    new_source = await _add_source(uow, tier=2)

    new_event_id = await _ingest(
        uow,
        new_source,
        claims=_claims(date="2024-06-01", reg="N123AB", op="AirlineX"),
    )

    assert new_event_id == existing_event.id, (
        "High-confidence match must reuse the existing event rather than create a new one"
    )
    # No new event in the store beyond the pre-seeded one
    assert len(uow.store.events) == 1


async def test_medium_confidence_match_creates_review(uow):
    """Same date but different registration -> medium confidence -> new event + review."""
    _existing_src, existing_event = await _seed_existing_event(
        uow, date="2024-06-01", reg="N123AB", op="AirlineX"
    )
    new_source = await _add_source(uow, tier=2)

    # Same date, same operator, but different registration - medium confidence
    new_event_id = await _ingest(
        uow,
        new_source,
        claims=_claims(date="2024-06-01", reg="N999ZZ", op="AirlineX"),
    )

    assert new_event_id != existing_event.id, (
        "Medium-confidence match must still create a new event"
    )
    # A PendingDuplicateReview should exist for the pair
    reviews = list(uow.store.duplicate_reviews.values())
    assert len(reviews) == 1
    review = reviews[0]
    assert review.status == DuplicateReviewStatus.PENDING
    assert existing_event.id in (review.event_id_a, review.event_id_b)
    assert new_event_id in (review.event_id_a, review.event_id_b)


async def test_no_match_creates_new_event_without_review(uow):
    """Completely different accident -> no match -> new event, no review."""
    _existing_src, _existing = await _seed_existing_event(
        uow, date="2024-06-01", reg="N123AB", op="AirlineX"
    )
    new_source = await _add_source(uow, tier=2)

    # Different date, different registration, different operator
    new_event_id = await _ingest(
        uow,
        new_source,
        claims=_claims(date="2023-01-15", reg="G-ABCD", op="EuroAir"),
    )

    assert new_event_id != _existing.id
    assert len(uow.store.duplicate_reviews) == 0


async def test_missing_event_date_creates_new_event_no_match(uow):
    """Claims without event_date cannot be matched -> new event, no review."""
    _existing_src, _existing = await _seed_existing_event(uow)
    new_source = await _add_source(uow)

    new_event_id = await _ingest(
        uow,
        new_source,
        claims=[IngestionClaimDTO(field_name="operator", field_value="AirlineX")],
    )

    assert new_event_id != _existing.id
    assert len(uow.store.duplicate_reviews) == 0


async def test_idempotent_retry_does_not_create_duplicate_review(uow):
    """Retrying an idempotent ingestion must not produce a second review record."""
    _existing_src, _existing = await _seed_existing_event(
        uow, date="2024-06-01", reg="N123AB", op="AirlineX"
    )
    new_source = await _add_source(uow)
    payload = {"v": 1}
    run_id = IngestSourceData.derive_ingestion_run_id(new_source.id, "key-r")
    claims = _claims(date="2024-06-01", reg="N999ZZ", op="AirlineX")

    await IngestSourceData(uow, make_settings()).execute(
        source_id=new_source.id, raw_payload=payload, ingestion_run_id=run_id, claims_data=claims
    )
    await IngestSourceData(uow, make_settings()).execute(
        source_id=new_source.id, raw_payload=payload, ingestion_run_id=run_id, claims_data=claims
    )

    # Only one review should exist despite two calls
    assert len(uow.store.duplicate_reviews) == 1


async def test_ingestion_to_merged_event_is_rejected(uow):
    """A stale explicit event_id must not write fresh claims to an absorbed event."""
    source = await _add_source(uow)

    target_event = await IngestSourceData(uow, make_settings()).execute(
        source_id=source.id,
        raw_payload={"target": True},
        ingestion_run_id=uuid4(),
        claims_data=_claims(date="2024-06-01", reg="N123AB", op="AirlineX"),
    )
    source_event = await IngestSourceData(uow, make_settings()).execute(
        source_id=source.id,
        raw_payload={"source": True},
        ingestion_run_id=uuid4(),
        claims_data=_claims(date="2024-07-01", reg="N999ZZ", op="AirlineY"),
    )

    await MergeDuplicateEvents(uow).execute(
        source_event_id=source_event,
        target_event_id=target_event,
        resolved_by=uuid4(),
    )

    with pytest.raises(EventAlreadyMergedError):
        await IngestSourceData(uow, make_settings()).execute(
            source_id=source.id,
            raw_payload={"stale": True},
            ingestion_run_id=uuid4(),
            claims_data=[IngestionClaimDTO(field_name="operator", field_value="AirlineZ")],
            event_id=source_event,
        )

    assert [
        c for c in uow.store.claims.values() if c.event_id == source_event and c.is_active
    ] == []


async def test_source_record_reingestion_after_merge_uses_canonical_event(uow):
    """A source-record update after merge should replace target-side transferred claims."""
    source = await _add_source(uow)
    record_id = "REC-MERGED-1"

    target_event = await IngestSourceData(uow, make_settings()).execute(
        source_id=source.id,
        raw_payload={"target": True},
        ingestion_run_id=uuid4(),
        claims_data=_claims(date="2024-06-01", reg="N123AB", op="AirlineX"),
    )
    source_event = await IngestSourceData(uow, make_settings()).execute(
        source_id=source.id,
        raw_payload={"source": True},
        ingestion_run_id=uuid4(),
        claims_data=[IngestionClaimDTO(field_name="operator", field_value="AirlineY")],
        source_record_id=record_id,
    )

    await MergeDuplicateEvents(uow).execute(
        source_event_id=source_event,
        target_event_id=target_event,
        resolved_by=uuid4(),
    )

    updated_event = await IngestSourceData(uow, make_settings()).execute(
        source_id=source.id,
        raw_payload={"source": "updated"},
        ingestion_run_id=uuid4(),
        claims_data=[IngestionClaimDTO(field_name="operator", field_value="AirlineZ")],
        source_record_id=record_id,
    )

    assert updated_event == target_event
    assert [
        c for c in uow.store.claims.values() if c.event_id == source_event and c.is_active
    ] == []
    active_ops = [
        c
        for c in uow.store.claims.values()
        if c.event_id == target_event and c.field_name == "operator" and c.is_active
    ]
    assert [c.field_value for c in active_ops] == ["AirlineX", "AirlineZ"]


async def test_anonymous_ingestion_matching_merged_identity_alias_uses_canonical_event(uow):
    """A merged event's identity row remains a searchable alias for the target.

    Curators can merge two events whose identity fields differ enough that they
    were not automatically matched.  Future anonymous ingestion that looks like
    the absorbed event must resolve to the canonical event, not create a third
    duplicate just because the absorbed event is excluded from normal writes.
    """
    source = await _add_source(uow)
    target_event = await IngestSourceData(uow, make_settings()).execute(
        source_id=source.id,
        raw_payload={"target": True},
        ingestion_run_id=uuid4(),
        claims_data=_claims(date="2024-06-01", reg="N123AB", op="AirlineX"),
    )
    absorbed_event = await IngestSourceData(uow, make_settings()).execute(
        source_id=source.id,
        raw_payload={"absorbed": True},
        ingestion_run_id=uuid4(),
        claims_data=_claims(date="2024-07-01", reg="N999ZZ", op="AirlineY"),
    )

    await MergeDuplicateEvents(uow).execute(
        source_event_id=absorbed_event,
        target_event_id=target_event,
        resolved_by=uuid4(),
    )

    new_source = await _add_source(uow)
    routed_event = await IngestSourceData(uow, make_settings()).execute(
        source_id=new_source.id,
        raw_payload={"looks_like_absorbed": True},
        ingestion_run_id=uuid4(),
        claims_data=_claims(date="2024-07-01", reg="N999ZZ", op="AirlineY"),
    )

    assert routed_event == target_event
    assert len(uow.store.events) == 2
    assert [
        c for c in uow.store.claims.values() if c.event_id == absorbed_event and c.is_active
    ] == []


async def test_source_record_update_auto_resolves_stale_open_conflict(uow):
    """If a source correction removes the active disagreement, close the old conflict."""
    source_a = await _add_source(uow)
    source_b = await _add_source(uow)
    record_id = "REC-CONFLICT-1"

    event_id = await IngestSourceData(uow, make_settings()).execute(
        source_id=source_a.id,
        raw_payload={"source": "a", "version": 1},
        ingestion_run_id=uuid4(),
        claims_data=[IngestionClaimDTO(field_name="operator", field_value="AirlineX")],
        source_record_id=record_id,
    )
    await IngestSourceData(uow, make_settings()).execute(
        source_id=source_b.id,
        raw_payload={"source": "b"},
        ingestion_run_id=uuid4(),
        claims_data=[IngestionClaimDTO(field_name="operator", field_value="AirlineY")],
        event_id=event_id,
    )

    open_conflict = await uow.conflicts.find_open_by_event_field(event_id, "operator")
    assert open_conflict is not None

    await IngestSourceData(uow, make_settings()).execute(
        source_id=source_a.id,
        raw_payload={"source": "a", "version": 2},
        ingestion_run_id=uuid4(),
        claims_data=[IngestionClaimDTO(field_name="operator", field_value="AirlineY")],
        source_record_id=record_id,
    )

    conflict = await uow.conflicts.get(open_conflict.id)
    assert conflict is not None
    assert conflict.status == ConflictStatus.RESOLVED
    assert conflict.last_modified_reason == ConflictModifierReason.SYSTEM_AUTO_CLOSED
    winning_claim = await uow.claims.get(conflict.winning_claim_id)
    assert winning_claim is not None
    assert winning_claim.field_value == "AirlineY"

    active_ops = [
        c.field_value
        for c in uow.store.claims.values()
        if c.event_id == event_id and c.field_name == "operator" and c.is_active
    ]
    assert active_ops == ["AirlineY", "AirlineY"]


# ── Projection-lag regression (the core timing bug) ──────────────────────────


async def test_two_immediate_ingestions_same_identity_reuse_or_review_without_projection(uow):
    """Two back-to-back ingestions of the same accident must not silently create
    two clean events.  This was the original projection-lag bug:

    - Source A ingests accident 2024-06-01 / N123AB.
    - IngestSourceData creates a new AccidentEvent.
    - Projection does not exist yet (outbox worker hasn't run).
    - Source B immediately ingests the same accident.
    - The OLD matcher queried projected_accident_records -> found nothing -> created
      a second event with no review.

    With the identity index (written in the same transaction as ingestion), the
    second ingestion finds the first event's entry and either reuses the event
    (high-confidence) or creates a review (medium-confidence).

    The test claims score 0.85 (registration 0.45 + date 0.30 + operator 0.10)
    which is >= HIGH_CONFIDENCE (0.75), so ev2 == ev1.
    """
    source_a = await _add_source(uow)
    source_b = await _add_source(uow)
    claims = [
        IngestionClaimDTO(field_name="event_date", field_value="2024-06-01"),
        IngestionClaimDTO(field_name="registration", field_value="N123AB"),
        IngestionClaimDTO(field_name="operator", field_value="AirlineX"),
    ]
    ev1 = await IngestSourceData(uow, make_settings()).execute(
        source_id=source_a.id,
        raw_payload={"source": "a"},
        ingestion_run_id=uuid4(),
        claims_data=claims,
    )
    ev2 = await IngestSourceData(uow, make_settings()).execute(
        source_id=source_b.id,
        raw_payload={"source": "b"},
        ingestion_run_id=uuid4(),
        claims_data=claims,
    )
    # The identity index (not projections) must bridge the gap.  No projection
    # may exist at this point - if the test fails here, the fix has regressed.
    assert len(uow.store.projections) == 0, (
        "Projection should not exist yet (outbox worker hasn't run); "
        "the identity index must have resolved identity without it."
    )
    assert ev2 == ev1 or len(uow.store.duplicate_reviews) == 1, (
        f"same? {ev1 == ev2}, events={len(uow.store.events)}, "
        f"reviews={len(uow.store.duplicate_reviews)}, "
        f"projections={len(uow.store.projections)}\n"
        "Two rapid ingestions of the same accident created separate events "
        "without any review.  The identity index must be queried, not projections."
    )


async def test_identity_index_is_written_before_projection_exists(uow):
    """After ingestion, the identity index must contain an entry even though
    the outbox worker has not run and no projection exists."""
    src = await _add_source(uow)
    claims = _claims(date="2024-06-01", reg="N123AB", op="AirlineX")
    event_id = await IngestSourceData(uow, make_settings()).execute(
        source_id=src.id,
        raw_payload={"x": 1},
        ingestion_run_id=uuid4(),
        claims_data=claims,
    )
    assert len(uow.store.projections) == 0, "projection should not exist yet"
    assert event_id in uow.store.identity_index, (
        "identity_index must be populated by the ingestion transaction itself"
    )
    entry = uow.store.identity_index[event_id]
    assert entry.event_date_norm == "2024-06-01"
    assert entry.registration_norm == "n123ab"  # normalised: lowercase, no hyphens


async def test_date_only_ingestion_still_indexes_without_registration(uow):
    """An ingestion with only event_date writes an identity index entry.

    Date-alone scores 0.30 which is below UNCERTAIN_LOW (0.40), so no review is
    triggered and a second date-only ingestion creates a separate event.  This is
    correct behaviour: a shared date with no other corroborating fields is too
    weak a signal to assert probable duplication.  The identity index is still
    written so richer subsequent ingestions for either event can find it.
    """
    src = await _add_source(uow)
    date_only = [IngestionClaimDTO(field_name="event_date", field_value="2024-07-15")]

    ev1 = await IngestSourceData(uow, make_settings()).execute(
        source_id=src.id,
        raw_payload={"x": 1},
        ingestion_run_id=uuid4(),
        claims_data=date_only,
    )
    # Identity index must be written even without registration.
    assert ev1 in uow.store.identity_index
    entry = uow.store.identity_index[ev1]
    assert entry.event_date_norm == "2024-07-15"
    assert entry.registration_norm is None


async def test_explicit_event_ingestion_updates_identity_index(uow):
    """Explicit event_id writes must still maintain searchable identity.

    Otherwise a caller can correctly attach claims to an existing event, but a
    later anonymous ingestion with the same accident identity will miss it and
    create a duplicate.
    """
    source_a = await _add_source(uow)
    source_b = await _add_source(uow)
    event = AccidentEvent(id=uuid4())
    await uow.events.add(event)

    explicit_event = await IngestSourceData(uow, make_settings()).execute(
        source_id=source_a.id,
        raw_payload={"explicit": True},
        ingestion_run_id=uuid4(),
        claims_data=_claims(date="2024-08-01", reg="N555AA", op="AirlineX"),
        event_id=event.id,
    )

    assert explicit_event == event.id
    assert event.id in uow.store.identity_index
    entry = uow.store.identity_index[event.id]
    assert entry.event_date_norm == "2024-08-01"
    assert entry.registration_norm == "n555aa"

    matched_event = await IngestSourceData(uow, make_settings()).execute(
        source_id=source_b.id,
        raw_payload={"anonymous": True},
        ingestion_run_id=uuid4(),
        claims_data=_claims(date="2024-08-01", reg="N555AA", op="AirlineX"),
    )

    assert matched_event == event.id
    assert len(uow.store.events) == 1


async def test_source_record_correction_refreshes_identity_index(uow):
    """Corrected source-record identity should be searchable immediately."""
    source_a = await _add_source(uow)
    source_b = await _add_source(uow)
    record_id = "REC-IDENTITY-REFRESH"

    event_id = await IngestSourceData(uow, make_settings()).execute(
        source_id=source_a.id,
        raw_payload={"record": record_id, "version": 1},
        ingestion_run_id=uuid4(),
        claims_data=_claims(date="2024-09-01", reg="OLD123", op="AirlineX"),
        source_record_id=record_id,
    )

    corrected_event = await IngestSourceData(uow, make_settings()).execute(
        source_id=source_a.id,
        raw_payload={"record": record_id, "version": 2},
        ingestion_run_id=uuid4(),
        claims_data=_claims(date="2024-09-01", reg="NEW123", op="AirlineX"),
        source_record_id=record_id,
    )

    assert corrected_event == event_id
    assert uow.store.identity_index[event_id].registration_norm == "new123"

    matched_event = await IngestSourceData(uow, make_settings()).execute(
        source_id=source_b.id,
        raw_payload={"source": "b"},
        ingestion_run_id=uuid4(),
        claims_data=_claims(date="2024-09-01", reg="NEW123", op="AirlineX"),
    )

    assert matched_event == event_id
    assert len(uow.store.events) == 1


async def test_execute_with_result_reports_existing_identity_match_not_created(uow):
    source_a = await _add_source(uow)
    source_b = await _add_source(uow)
    use_case = IngestSourceData(uow, make_settings())

    first = await use_case.execute_with_result(
        source_id=source_a.id,
        raw_payload={"source": "a"},
        ingestion_run_id=uuid4(),
        claims_data=_claims(date="2024-10-01", reg="N777AA", op="AirlineX"),
    )
    second = await use_case.execute_with_result(
        source_id=source_b.id,
        raw_payload={"source": "b"},
        ingestion_run_id=uuid4(),
        claims_data=_claims(date="2024-10-01", reg="N777AA", op="AirlineX"),
    )

    assert first.event_created is True
    assert first.attached_by == "new_event"
    assert second.event_id == first.event_id
    assert second.event_created is False
    assert second.attached_by == "identity_match"


async def test_query_public_view_resolves_merged_event_to_canonical_projection(uow):
    """Reads should not expose a stale projection for an absorbed event."""
    source = await _add_source(uow)
    target_event = await IngestSourceData(uow, make_settings()).execute(
        source_id=source.id,
        raw_payload={"target": True},
        ingestion_run_id=uuid4(),
        claims_data=_claims(date="2024-06-01", reg="N123AB", op="AirlineX"),
    )
    absorbed_event = await IngestSourceData(uow, make_settings()).execute(
        source_id=source.id,
        raw_payload={"absorbed": True},
        ingestion_run_id=uuid4(),
        claims_data=_claims(date="2024-07-01", reg="N999ZZ", op="AirlineY"),
    )

    await uow.projections.upsert(
        ProjectedAccidentRecord(
            event_id=target_event,
            projection_version=1,
            fields={"registration": "N123AB", "operator": "AirlineX"},
        )
    )
    await uow.projections.upsert(
        ProjectedAccidentRecord(
            event_id=absorbed_event,
            projection_version=1,
            fields={"registration": "N999ZZ", "operator": "AirlineY"},
        )
    )

    await MergeDuplicateEvents(uow).execute(
        source_event_id=absorbed_event,
        target_event_id=target_event,
        resolved_by=uuid4(),
    )

    projection = await QueryAccidentPublicView(uow).execute(absorbed_event)

    assert projection is not None
    assert projection.event_id == target_event
    assert projection.fields["registration"] == "N123AB"


async def test_query_public_view_fails_closed_on_merge_cycle(uow):
    """Invalid merge cycles must not return a stale projection."""
    event_a = AccidentEvent(id=uuid4(), is_merged=True, merged_into_event_id=None)
    event_b = AccidentEvent(id=uuid4(), is_merged=True, merged_into_event_id=event_a.id)
    event_a.merged_into_event_id = event_b.id

    await uow.events.add(event_a)
    await uow.events.add(event_b)
    await uow.projections.upsert(
        ProjectedAccidentRecord(
            event_id=event_a.id,
            projection_version=1,
            fields={"registration": "STALE-A"},
        )
    )
    await uow.projections.upsert(
        ProjectedAccidentRecord(
            event_id=event_b.id,
            projection_version=1,
            fields={"registration": "STALE-B"},
        )
    )

    projection = await QueryAccidentPublicView(uow).execute(event_a.id)

    assert projection is None


async def test_high_confidence_identity_tie_queues_review_for_each_tied_candidate(uow):
    """Equally strong candidates should all be visible to curators."""
    existing_a = AccidentEvent(id=uuid4())
    existing_b = AccidentEvent(id=uuid4())
    await uow.events.add(existing_a)
    await uow.events.add(existing_b)
    uow.store.identity_index[existing_a.id] = EventIdentityIndex(
        event_id=existing_a.id,
        event_date_norm="2024-06-01",
        registration_norm="n123ab",
        operator_norm="airlinex",
    )
    uow.store.identity_index[existing_b.id] = EventIdentityIndex(
        event_id=existing_b.id,
        event_date_norm="2024-06-01",
        registration_norm="n123ab",
        operator_norm="airlinex",
    )
    new_source = await _add_source(uow)

    new_event_id = await _ingest(
        uow,
        new_source,
        claims=_claims(date="2024-06-01", reg="N123AB", op="AirlineX"),
    )

    assert new_event_id not in {existing_a.id, existing_b.id}
    pending_pairs = {
        frozenset((review.event_id_a, review.event_id_b))
        for review in uow.store.duplicate_reviews.values()
        if review.status == DuplicateReviewStatus.PENDING
    }
    assert pending_pairs == {
        frozenset((existing_a.id, new_event_id)),
        frozenset((existing_b.id, new_event_id)),
    }


async def test_ambiguous_identity_result_exposes_all_review_ids_and_replay_preserves_them(uow):
    """The use-case/API contract should expose every review created for a tie."""
    existing_a = AccidentEvent(id=uuid4())
    existing_b = AccidentEvent(id=uuid4())
    await uow.events.add(existing_a)
    await uow.events.add(existing_b)
    for event in (existing_a, existing_b):
        uow.store.identity_index[event.id] = EventIdentityIndex(
            event_id=event.id,
            event_date_norm="2024-06-01",
            registration_norm="n123ab",
            operator_norm="airlinex",
        )
    source = await _add_source(uow)
    run_id = uuid4()
    claims = _claims(date="2024-06-01", reg="N123AB", op="AirlineX")

    first = await IngestSourceData(uow, settings=make_settings()).execute_with_result(
        source_id=source.id,
        raw_payload={"id": "tie-contract"},
        ingestion_run_id=run_id,
        claims_data=claims,
    )

    assert first.pending_review_id is not None
    assert len(first.pending_review_ids) == 2
    assert first.pending_review_id == first.pending_review_ids[0]
    assert set(first.pending_review_ids) == set(uow.store.duplicate_reviews.keys())

    snapshot = await uow.snapshots.find_by_source_run(source.id, run_id)
    assert snapshot is not None
    assert snapshot.ingestion_result_json is not None
    assert snapshot.ingestion_result_json["pending_review_id"] == str(first.pending_review_id)
    assert snapshot.ingestion_result_json["pending_review_ids"] == [
        str(review_id) for review_id in first.pending_review_ids
    ]

    replay = await IngestSourceData(uow, settings=make_settings()).execute_with_result(
        source_id=source.id,
        raw_payload={"id": "tie-contract"},
        ingestion_run_id=run_id,
        claims_data=claims,
    )

    assert replay.idempotent_replay is True
    assert replay.pending_review_id == first.pending_review_id
    assert replay.pending_review_ids == first.pending_review_ids
    assert replay.snapshot_created is False


async def test_ambiguous_identity_review_fanout_is_capped(uow):
    """Sparse/noisy ties should not be able to flood the curator queue."""
    existing_events = [AccidentEvent(id=uuid4()) for _ in range(4)]
    for event in existing_events:
        await uow.events.add(event)
        uow.store.identity_index[event.id] = EventIdentityIndex(
            event_id=event.id,
            event_date_norm="2024-06-01",
            registration_norm="n123ab",
            operator_norm="airlinex",
        )
    source = await _add_source(uow)
    settings = make_settings()
    settings.max_duplicate_reviews_per_ingestion = 2

    result = await IngestSourceData(uow, settings=settings).execute_with_result(
        source_id=source.id,
        raw_payload={"id": "tie-cap"},
        ingestion_run_id=uuid4(),
        claims_data=_claims(date="2024-06-01", reg="N123AB", op="AirlineX"),
    )

    assert len(result.pending_review_ids) == 2
    assert len(uow.store.duplicate_reviews) == 2
    reviewed_candidate_ids = {review.event_id_a for review in uow.store.duplicate_reviews.values()}
    assert reviewed_candidate_ids <= {event.id for event in existing_events}


async def test_secondary_aircraft_registration_indexes_as_duplicate_review_candidate(uow):
    source = await _add_source(uow)

    # 1. Ingest an event with N111AA as primary and N222BB as secondary registration
    claims1 = [
        IngestionClaimDTO(field_name="event_date", field_value="2024-01-01"),
        IngestionClaimDTO(field_name="registration", field_value="N111AA"),
        IngestionClaimDTO(
            field_name="aircraft_registration_numbers", field_value=["N111AA", "N222BB"]
        ),
    ]
    event_id1 = await IngestSourceData(uow, settings=make_settings()).execute(
        source_id=source.id,
        raw_payload={"id": "event-1"},
        ingestion_run_id=uuid4(),
        claims_data=claims1,
    )

    # Verify that it created the event and populated identity index correctly
    entry1 = uow.store.identity_index.get(event_id1)
    assert entry1 is not None
    assert entry1.registration_norm == "n111aa"
    assert entry1.registration_norms == ["n111aa", "n222bb"]

    # 2. Ingest an event from a different source (or same source) with N222BB as primary registration
    claims2 = [
        IngestionClaimDTO(field_name="event_date", field_value="2024-01-01"),
        IngestionClaimDTO(field_name="registration", field_value="N222BB"),
    ]
    result2 = await IngestSourceData(uow, settings=make_settings()).execute_with_result(
        source_id=source.id,
        raw_payload={"id": "event-2"},
        ingestion_run_id=uuid4(),
        claims_data=claims2,
    )

    # Expected outcome:
    # Since the registration matches N222BB (which is a secondary alias for event 1) and the date is identical,
    # the matcher should find event 1 as a candidate. Since it's a secondary alias, the matcher assigns half-weight.
    # The score should be enough to flag it as a duplicate review candidate (since date matches exactly),
    # but not high enough for auto-merge.
    # Therefore, a new event is created and a duplicate review is registered.
    assert result2.event_id != event_id1
    assert result2.event_created is True
    assert len(result2.pending_review_ids) == 1

    review = next(iter(uow.store.duplicate_reviews.values()))
    # The review should associate the newly created event with the existing event
    assert {review.event_id_a, review.event_id_b} == {event_id1, result2.event_id}
