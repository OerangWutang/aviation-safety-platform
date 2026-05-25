"""Use-case-level tests for IngestSourceData using an in-memory UoW.

These cover the behaviors flagged in the review:
- Source validation happens before ingestion run is created.
- Event-not-found is surfaced as a typed domain error.
- Duplicate ingestion payloads stay idempotent.
- Normalization integration produces the expected claim values.
- Conflicts, conflict-activity, claim-history, and outbox events are written.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from atlas.application.dto import IngestionClaimDTO
from atlas.application.use_cases.ingest_source_data import IngestSourceData
from atlas.domain.entities import AccidentEvent, Source
from atlas.domain.enums import ClaimType, ConflictStatus, OutboxStatus, SourceKind
from atlas.domain.exceptions import (
    DomainValidationError,
    DuplicateClaimFieldError,
    EventNotFoundError,
    IdempotencyKeyPayloadMismatchError,
    IngestionInProgressError,
    PersistenceCorruptionError,
    SourceNotFoundError,
)
from atlas.domain.services.ingestion import NormalizationError
from tests.domain._fake_uow import InMemoryUnitOfWork, make_settings


@pytest.fixture
def uow() -> InMemoryUnitOfWork:
    return InMemoryUnitOfWork()


async def _add_source(uow: InMemoryUnitOfWork, *, tier: int = 1) -> Source:
    src = Source(
        id=uuid4(), name=f"S-{uuid4().hex[:6]}", kind=SourceKind.EXTERNAL, reliability_tier=tier
    )
    await uow.sources.add(src)
    return src


async def test_unknown_source_raises_before_ingestion_run_is_created(uow):
    settings = make_settings()
    bad_source_id = uuid4()
    run_id = uuid4()

    with pytest.raises(SourceNotFoundError):
        await IngestSourceData(uow, settings=settings).execute(
            source_id=bad_source_id,
            raw_payload={"x": 1},
            ingestion_run_id=run_id,
            claims_data=[IngestionClaimDTO(field_name="event_date", field_value="2024-01-01")],
        )

    # The ingestion run must NOT have been written. This is the regression
    # we flagged: ``ensure_started`` used to fire before source validation.
    assert run_id not in uow.store.ingestion_runs


async def test_unknown_event_id_raises_before_ingestion_run_is_created(uow):
    src = await _add_source(uow)
    run_id = uuid4()
    bad_event_id = uuid4()

    with pytest.raises(EventNotFoundError):
        await IngestSourceData(uow, settings=make_settings()).execute(
            source_id=src.id,
            raw_payload={},
            ingestion_run_id=run_id,
            claims_data=[IngestionClaimDTO(field_name="event_date", field_value="2024-01-01")],
            event_id=bad_event_id,
        )
    assert run_id not in uow.store.ingestion_runs


async def test_reusing_ingestion_run_id_across_sources_is_rejected(uow):
    """An ``ingestion_run_id`` already owned by source A must not be reusable
    by source B.  The deterministic ``derive_ingestion_run_id`` mixes both
    ``source_id`` and the idempotency key so API callers can't trigger this
    cross-source collision in practice, but internal callers (CLI, tests,
    background jobs) can pass arbitrary run ids - and the use case must still
    refuse to attach a new snapshot to a run row owned by a different source.

    Regression guard for risk #3 from the review.
    """
    src_a = await _add_source(uow)
    src_b = await _add_source(uow)
    shared_run_id = uuid4()
    settings = make_settings()

    # First ingestion under src_a creates the ingestion_run row.
    await IngestSourceData(uow, settings=settings).execute(
        source_id=src_a.id,
        raw_payload={"r": "a"},
        ingestion_run_id=shared_run_id,
        claims_data=[IngestionClaimDTO(field_name="event_date", field_value="2024-01-01")],
    )
    assert uow.store.ingestion_runs[shared_run_id].source_id == src_a.id

    # Second ingestion under src_b reusing the same run_id must reject.
    with pytest.raises(DomainValidationError, match=r"already exists for source"):
        await IngestSourceData(uow, settings=settings).execute(
            source_id=src_b.id,
            raw_payload={"r": "b"},
            ingestion_run_id=shared_run_id,
            claims_data=[IngestionClaimDTO(field_name="event_date", field_value="2024-01-02")],
        )

    # The run row must still belong to src_a - src_b must not have been able
    # to mutate it via ``ensure_started``'s ON CONFLICT DO NOTHING path.
    assert uow.store.ingestion_runs[shared_run_id].source_id == src_a.id
    # No snapshot for src_b should exist.
    assert all(snap.source_id != src_b.id for snap in uow.store.snapshots.values())


async def test_ingest_creates_event_claim_history_outbox_and_normalizes(uow):
    src = await _add_source(uow)
    run_id = uuid4()
    event_id = await IngestSourceData(uow, settings=make_settings()).execute(
        source_id=src.id,
        raw_payload={"hello": "world"},
        ingestion_run_id=run_id,
        # External source normalizer should:
        #   - coerce "5" -> 5 for fatalities_total
        #   - parse unambiguous year-first date strings
        #   - upper-case registration
        claims_data=[
            IngestionClaimDTO(field_name="fatalities_total", field_value="5"),
            IngestionClaimDTO(field_name="event_date", field_value="2024/01/15"),
            IngestionClaimDTO(field_name="registration", field_value="ph-bxo"),
        ],
    )

    assert event_id in uow.store.events
    claims = list(uow.store.claims.values())
    assert len(claims) == 3
    by_field = {c.field_name: c for c in claims}
    assert by_field["fatalities_total"].field_value == 5
    assert by_field["event_date"].field_value == "2024-01-15"
    assert by_field["registration"].field_value == "PH-BXO"

    # Each claim got a "created" history row.
    assert len(uow.store.claim_history) == 3
    assert {h.action for h in uow.store.claim_history} == {"created"}
    assert {h.to_claim_type for h in uow.store.claim_history} == {ClaimType.RAW}

    # Outbox event was queued.
    assert len(uow.store.outbox) == 1
    outbox = uow.store.outbox[0]
    assert outbox.event_type == "CLAIMS_UPDATED"
    assert outbox.aggregate_id == event_id
    assert outbox.status == OutboxStatus.PENDING

    # Ingestion run finished cleanly.
    assert uow.store.ingestion_runs[run_id].status == "finished"

    # Snapshot stores both raw-payload and full-submission hashes plus the
    # durable replay result.
    snapshot = next(iter(uow.store.snapshots.values()))
    assert snapshot.raw_payload_hash is not None
    assert snapshot.submission_hash == snapshot.payload_hash
    assert snapshot.submission_fingerprint_json is not None
    assert snapshot.ingestion_result_json is not None
    assert snapshot.ingestion_result_json["event_id"] == str(event_id)


async def test_same_source_without_source_record_id_supersedes_prior_active_claim(uow):
    """Repeated same-source claims without source_record_id must not both stay active.

    This preserves compatibility for callers that do not yet have stable source
    record IDs while still preventing same-source contradictions from being
    hidden from conflict detection/projection by leaving multiple active claims.
    """
    src_a = await _add_source(uow, tier=1)
    src_b = await _add_source(uow, tier=2)

    event_id = await IngestSourceData(uow, settings=make_settings()).execute(
        source_id=src_a.id,
        raw_payload={"submission": "a1"},
        ingestion_run_id=uuid4(),
        claims_data=[IngestionClaimDTO(field_name="operator", field_value="Old Air")],
    )
    await IngestSourceData(uow, settings=make_settings()).execute(
        source_id=src_b.id,
        raw_payload={"submission": "b1"},
        ingestion_run_id=uuid4(),
        claims_data=[IngestionClaimDTO(field_name="operator", field_value="Other Air")],
        event_id=event_id,
    )
    await IngestSourceData(uow, settings=make_settings()).execute(
        source_id=src_a.id,
        raw_payload={"submission": "a2"},
        ingestion_run_id=uuid4(),
        claims_data=[IngestionClaimDTO(field_name="operator", field_value="New Air")],
        event_id=event_id,
    )

    active_operator_claims = [
        claim
        for claim in uow.store.claims.values()
        if claim.event_id == event_id and claim.field_name == "operator" and claim.is_active
    ]
    assert {claim.field_value for claim in active_operator_claims} == {
        "New Air",
        "Other Air",
    }
    assert all(claim.field_value != "Old Air" for claim in active_operator_claims)
    old_claim = next(claim for claim in uow.store.claims.values() if claim.field_value == "Old Air")
    assert old_claim.claim_type == ClaimType.SUPERSEDED


async def test_duplicate_ingestion_payload_returns_same_event(uow):
    src = await _add_source(uow)
    run_id = uuid4()
    payload = {"id": "abc"}
    claims = [IngestionClaimDTO(field_name="event_date", field_value="2024-01-01")]

    first_event_id = await IngestSourceData(uow, settings=make_settings()).execute(
        source_id=src.id,
        raw_payload=payload,
        ingestion_run_id=run_id,
        claims_data=claims,
    )

    # Second call with the same (source, run, submission hash) tuple must:
    #  (a) return the same event_id, AND
    #  (b) NOT add new claims.
    second_event_id = await IngestSourceData(uow, settings=make_settings()).execute(
        source_id=src.id,
        raw_payload=payload,
        ingestion_run_id=run_id,
        claims_data=claims,
    )
    assert second_event_id == first_event_id
    assert len(uow.store.claims) == 1


async def test_duplicate_payload_for_in_progress_run_raises_in_progress(uow):
    """If the snapshot row exists but no claims yet, the original ingestion is
    considered still running; the duplicate must surface ``IngestionInProgressError``."""
    src = await _add_source(uow)
    run_id = uuid4()
    payload = {"id": "abc"}

    # Pre-seed a snapshot row WITHOUT corresponding claims, simulating a
    # concurrent in-flight ingestion.
    import hashlib

    from atlas.application.use_cases.ingest_source_data import _canonical_ingestion_submission
    from atlas.domain.entities import RawSnapshot

    claims = [IngestionClaimDTO(field_name="event_date", field_value="2024-01-01")]
    payload_hash = hashlib.sha256(
        _canonical_ingestion_submission(
            raw_payload=payload,
            claims_data=claims,
            source_record_id=None,
            event_id=None,
            captured_at=None,
        )
    ).hexdigest()
    await uow.snapshots.add(
        RawSnapshot(
            id=uuid4(),
            source_id=src.id,
            ingestion_run_id=run_id,
            payload_hash=payload_hash,
            payload_json=payload,
            captured_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
        )
    )

    with pytest.raises(IngestionInProgressError):
        await IngestSourceData(uow, settings=make_settings()).execute(
            source_id=src.id,
            raw_payload=payload,
            ingestion_run_id=run_id,
            claims_data=claims,
        )


async def test_cross_source_disagreement_creates_open_conflict_and_logs_activity(uow):
    src_a = await _add_source(uow, tier=1)
    src_b = await _add_source(uow, tier=2)

    # First ingestion creates the event.
    event_id = await IngestSourceData(uow, settings=make_settings()).execute(
        source_id=src_a.id,
        raw_payload={"r": 1},
        ingestion_run_id=uuid4(),
        claims_data=[IngestionClaimDTO(field_name="fatalities_total", field_value=5)],
    )

    # Second ingestion into the same event with a contradicting value.
    await IngestSourceData(uow, settings=make_settings()).execute(
        source_id=src_b.id,
        raw_payload={"r": 2},
        ingestion_run_id=uuid4(),
        claims_data=[IngestionClaimDTO(field_name="fatalities_total", field_value=6)],
        event_id=event_id,
    )

    conflicts = list(uow.store.conflicts.values())
    assert len(conflicts) == 1
    conflict = conflicts[0]
    assert conflict.field_name == "fatalities_total"
    assert conflict.status == ConflictStatus.OPEN
    assert len(uow.store.conflict_claim_links[conflict.id]) == 2

    activity = [e for e in uow.store.conflict_activity if e.conflict_id == conflict.id]
    # One INITIAL detection log entry (the second ingestion's evidence simply
    # lands inside the same OPEN conflict - there is no separate "evidence
    # added" entry because at first-detection time the conflict didn't exist
    # yet for the prior ingestion.) Behavior verified: at least one entry exists.
    assert activity, "Expected at least one conflict activity log entry"
    assert activity[0].to_status == ConflictStatus.OPEN


async def test_normalization_invalid_identity_date_rejects_before_writes(uow):
    """Invalid identity-critical dates reject the payload before state is written."""
    src = await _add_source(uow)
    run_id = uuid4()

    with pytest.raises(NormalizationError):
        await IngestSourceData(uow, settings=make_settings()).execute(
            source_id=src.id,
            raw_payload={},
            ingestion_run_id=run_id,
            claims_data=[IngestionClaimDTO(field_name="event_date", field_value="not-a-date")],
        )

    assert run_id not in uow.store.ingestion_runs
    assert not uow.store.snapshots
    assert not uow.store.claims


async def test_non_json_raw_payload_rejects_before_writes(uow):
    """Use-case callers must submit real JSON payloads, not Python-only objects."""
    src = await _add_source(uow)
    run_id = uuid4()

    with pytest.raises(DomainValidationError, match="raw_payload must be JSON-serializable"):
        await IngestSourceData(uow, settings=make_settings()).execute(
            source_id=src.id,
            raw_payload={"bad": uuid4()},
            ingestion_run_id=run_id,
            claims_data=[IngestionClaimDTO(field_name="event_date", field_value="2024-01-01")],
        )

    assert run_id not in uow.store.ingestion_runs
    assert not uow.store.snapshots
    assert not uow.store.claims


async def test_normalization_unknown_field_passes_through(uow):
    src = await _add_source(uow)
    event_id = await IngestSourceData(uow, settings=make_settings()).execute(
        source_id=src.id,
        raw_payload={},
        ingestion_run_id=uuid4(),
        claims_data=[IngestionClaimDTO(field_name="custom_field", field_value="anything")],
    )
    claims = [c for c in uow.store.claims.values() if c.event_id == event_id]
    assert claims[0].field_value == "anything"


async def test_reused_ingestion_run_id_with_different_source_raises(uow):
    """If ingestion_run_id is reused with a different source_id, we must
    raise rather than silently corrupt the audit trail."""
    src_a = await _add_source(uow)
    src_b = await _add_source(uow)
    run_id = uuid4()

    # First ingestion with src_a completes.
    await IngestSourceData(uow, settings=make_settings()).execute(
        source_id=src_a.id,
        raw_payload={"r": 1},
        ingestion_run_id=run_id,
        claims_data=[IngestionClaimDTO(field_name="event_date", field_value="2024-01-01")],
    )

    # Attempt to reuse the same run_id with src_b must fail.
    from atlas.domain.exceptions import DomainValidationError

    with pytest.raises(DomainValidationError, match="already exists for source"):
        await IngestSourceData(uow, settings=make_settings()).execute(
            source_id=src_b.id,
            raw_payload={"r": 2},
            ingestion_run_id=run_id,
            claims_data=[IngestionClaimDTO(field_name="event_date", field_value="2024-01-01")],
        )


async def test_ingest_into_existing_event_does_not_create_new_event(uow):
    src = await _add_source(uow)
    pre_existing = AccidentEvent(id=uuid4())
    await uow.events.add(pre_existing)

    returned_event_id = await IngestSourceData(uow, settings=make_settings()).execute(
        source_id=src.id,
        raw_payload={"a": 1},
        ingestion_run_id=uuid4(),
        claims_data=[IngestionClaimDTO(field_name="event_date", field_value="2024-01-01")],
        event_id=pre_existing.id,
    )
    assert returned_event_id == pre_existing.id
    # No new event was added.
    assert len(uow.store.events) == 1


async def test_reused_idempotency_key_with_different_claims_raises_mismatch(uow):
    """Idempotency fingerprint must cover extracted claims, not only raw_payload."""
    src = await _add_source(uow)
    run_id = uuid4()
    payload = {"id": "same-raw-object"}

    first_event_id = await IngestSourceData(uow, settings=make_settings()).execute(
        source_id=src.id,
        raw_payload=payload,
        ingestion_run_id=run_id,
        claims_data=[IngestionClaimDTO(field_name="event_date", field_value="2024-01-01")],
    )
    assert first_event_id in uow.store.events

    with pytest.raises(IdempotencyKeyPayloadMismatchError):
        await IngestSourceData(uow, settings=make_settings()).execute(
            source_id=src.id,
            raw_payload=payload,
            ingestion_run_id=run_id,
            claims_data=[IngestionClaimDTO(field_name="event_date", field_value="2024-01-02")],
        )


async def test_explicit_event_identity_index_uses_normalised_fields(uow):
    """Explicit-event path must not write raw date strings into identity_index."""
    src = await _add_source(uow)
    event = AccidentEvent(id=uuid4())
    await uow.events.add(event)

    returned = await IngestSourceData(uow, settings=make_settings()).execute(
        source_id=src.id,
        raw_payload={"record": "explicit"},
        ingestion_run_id=uuid4(),
        event_id=event.id,
        claims_data=[
            IngestionClaimDTO(field_name="event_date", field_value="2024/1/5"),
            IngestionClaimDTO(field_name="registration", field_value="ph-bxo"),
        ],
    )

    assert returned == event.id
    entry = uow.store.identity_index[event.id]
    assert entry.event_date_norm == "2024-01-05"
    assert entry.registration_norm == "phbxo"


async def test_source_record_continuity_identity_index_uses_normalised_fields(uow):
    """Source-record correction path should update identity_index with normalized fields."""
    src = await _add_source(uow)
    source_record_id = "SRC-REC-1"

    event_id = await IngestSourceData(uow, settings=make_settings()).execute(
        source_id=src.id,
        raw_payload={"version": 1},
        ingestion_run_id=uuid4(),
        source_record_id=source_record_id,
        claims_data=[
            IngestionClaimDTO(field_name="event_date", field_value="2024-01-05"),
            IngestionClaimDTO(field_name="registration", field_value="PH-BXO"),
        ],
    )

    await IngestSourceData(uow, settings=make_settings()).execute(
        source_id=src.id,
        raw_payload={"version": 2},
        ingestion_run_id=uuid4(),
        source_record_id=source_record_id,
        claims_data=[
            IngestionClaimDTO(field_name="event_date", field_value="2024/1/5"),
            IngestionClaimDTO(field_name="registration", field_value="ph-bxo"),
        ],
    )

    entry = uow.store.identity_index[event_id]
    assert entry.event_date_norm == "2024-01-05"
    assert "SRC-REC-1" in entry.source_record_ids


async def test_orphan_source_record_snapshot_does_not_hide_older_valid_owner(uow):
    """Continuity should skip newer orphan snapshots and use latest snapshot with claims."""
    from datetime import UTC, datetime, timedelta

    from atlas.domain.entities import RawSnapshot

    src = await _add_source(uow)
    source_record_id = "SRC-ORPHAN"

    event_id = await IngestSourceData(uow, settings=make_settings()).execute(
        source_id=src.id,
        raw_payload={"version": 1},
        ingestion_run_id=uuid4(),
        source_record_id=source_record_id,
        claims_data=[IngestionClaimDTO(field_name="event_date", field_value="2024-01-05")],
    )

    await uow.snapshots.add(
        RawSnapshot(
            id=uuid4(),
            source_id=src.id,
            ingestion_run_id=uuid4(),
            payload_hash="orphan",
            payload_json={"orphan": True},
            captured_at=datetime.now(UTC),
            created_at=datetime.now(UTC) + timedelta(minutes=5),
            source_record_id=source_record_id,
        )
    )

    returned = await IngestSourceData(uow, settings=make_settings()).execute(
        source_id=src.id,
        raw_payload={"version": 3},
        ingestion_run_id=uuid4(),
        source_record_id=source_record_id,
        claims_data=[IngestionClaimDTO(field_name="event_date", field_value="2024-01-06")],
    )
    assert returned == event_id


async def test_source_field_alias_tail_number_maps_to_registration(uow):
    src = await _add_source(uow)
    event_id = await IngestSourceData(uow, settings=make_settings()).execute(
        source_id=src.id,
        raw_payload={"source": "alias"},
        ingestion_run_id=uuid4(),
        claims_data=[
            IngestionClaimDTO(field_name="event_date", field_value="2024-01-05"),
            IngestionClaimDTO(field_name="tail_number", field_value="ph-bxo"),
        ],
    )

    claims = [claim for claim in uow.store.claims.values() if claim.event_id == event_id]
    by_field = {claim.field_name: claim for claim in claims}
    assert "registration" in by_field
    assert by_field["registration"].field_value == "PH-BXO"
    assert uow.store.identity_index[event_id].registration_norm == "phbxo"


async def test_source_field_alias_duplicate_canonical_field_rejected(uow):
    src = await _add_source(uow)

    with pytest.raises(DuplicateClaimFieldError):
        await IngestSourceData(uow, settings=make_settings()).execute(
            source_id=src.id,
            raw_payload={"source": "alias"},
            ingestion_run_id=uuid4(),
            claims_data=[
                IngestionClaimDTO(field_name="registration", field_value="PH-BXO"),
                IngestionClaimDTO(field_name="tail_number", field_value="PH-BXO"),
            ],
        )


async def test_exact_idempotent_replay_returns_even_if_explicit_event_was_merged(uow):
    """Replay should short-circuit before mutable current event-state validation."""
    src = await _add_source(uow)
    event = AccidentEvent(id=uuid4())
    canonical = AccidentEvent(id=uuid4())
    await uow.events.add(event)
    await uow.events.add(canonical)
    run_id = uuid4()
    payload = {"id": "stable"}
    claims = [IngestionClaimDTO(field_name="event_date", field_value="2024-01-01")]

    first = await IngestSourceData(uow, settings=make_settings()).execute(
        source_id=src.id,
        raw_payload=payload,
        ingestion_run_id=run_id,
        claims_data=claims,
        event_id=event.id,
    )
    assert first == event.id

    # The event is absorbed after the original success.  An exact retry should
    # still short-circuit through idempotency instead of re-validating the
    # explicit event and raising EventAlreadyMergedError.  Replays return the
    # current canonical event id so clients do not keep following absorbed ids.
    uow.store.events[event.id].merged_into_event_id = canonical.id

    second = await IngestSourceData(uow, settings=make_settings()).execute(
        source_id=src.id,
        raw_payload=payload,
        ingestion_run_id=run_id,
        claims_data=claims,
        event_id=event.id,
    )
    assert second == canonical.id
    assert len(uow.store.claims) == 1


async def test_idempotent_replay_preserves_pending_duplicate_review_id(uow):
    """Replay must use persisted result metadata, not reconstruct from claims.

    Ingestions that queue PendingDuplicateReview return a review id. A retry
    must return that same review id so clients can safely retry after a network
    failure without losing the review handle.
    """
    src = await _add_source(uow)
    settings = make_settings()

    # Seed an existing event with just enough matching identity to put the next
    # ingestion in the review band: event_date (0.30) + operator (0.10) = 0.40.
    await IngestSourceData(uow, settings=settings).execute_with_result(
        source_id=src.id,
        raw_payload={"id": "seed"},
        ingestion_run_id=uuid4(),
        claims_data=[
            IngestionClaimDTO(field_name="event_date", field_value="2024-04-01"),
            IngestionClaimDTO(field_name="operator", field_value="Alpha Air"),
        ],
    )

    run_id = uuid4()
    claims = [
        IngestionClaimDTO(field_name="event_date", field_value="2024-04-01"),
        IngestionClaimDTO(field_name="operator", field_value="Alpha Air"),
        IngestionClaimDTO(field_name="registration", field_value="N-REVIEW"),
    ]
    first = await IngestSourceData(uow, settings=settings).execute_with_result(
        source_id=src.id,
        raw_payload={"id": "review-candidate"},
        ingestion_run_id=run_id,
        claims_data=claims,
    )
    assert first.pending_review_id is not None
    assert first.attached_by == "duplicate_review"

    replay = await IngestSourceData(uow, settings=settings).execute_with_result(
        source_id=src.id,
        raw_payload={"id": "review-candidate"},
        ingestion_run_id=run_id,
        claims_data=claims,
    )

    assert replay.idempotent_replay is True
    assert replay.event_id == first.event_id
    assert replay.pending_review_id == first.pending_review_id
    assert replay.attached_by == "duplicate_review"
    assert replay.snapshot_created is False


async def test_idempotent_replay_after_merge_returns_canonical_from_persisted_result(uow):
    """Replay should not scan copied/superseded claims after a merge.

    MergeDuplicateEvents copies source claims to the target and supersedes the
    old claims.  Claim-scanning replay can therefore choose the absorbed event
    nondeterministically.  Stored result metadata plus canonicalization returns
    the current surviving event.
    """
    src = await _add_source(uow)
    settings = make_settings()
    run_id = uuid4()
    claims = [IngestionClaimDTO(field_name="event_date", field_value="2024-06-01")]

    first = await IngestSourceData(uow, settings=settings).execute_with_result(
        source_id=src.id,
        raw_payload={"id": "merge-replay"},
        ingestion_run_id=run_id,
        claims_data=claims,
    )
    target = AccidentEvent(id=uuid4())
    await uow.events.add(target)

    from atlas.application.use_cases.merge_duplicate_events import MergeDuplicateEvents

    await MergeDuplicateEvents(uow).execute(
        source_event_id=first.event_id,
        target_event_id=target.id,
        resolved_by=uuid4(),
    )

    replay = await IngestSourceData(uow, settings=settings).execute_with_result(
        source_id=src.id,
        raw_payload={"id": "merge-replay"},
        ingestion_run_id=run_id,
        claims_data=claims,
    )

    assert replay.idempotent_replay is True
    assert replay.event_id == target.id
    assert replay.pending_review_id == first.pending_review_id
    assert (
        len(uow.store.claims) == 2
    )  # original superseded + copied target claim; no new ingestion claims


async def test_snapshot_insert_race_with_different_submission_reports_mismatch(uow):
    """The unique-insert race path should compare by source/run, not by hash."""
    from datetime import UTC, datetime

    from atlas.domain.entities import RawSnapshot

    src = await _add_source(uow)
    run_id = uuid4()
    payload = {"id": "current"}
    claims = [IngestionClaimDTO(field_name="event_date", field_value="2024-01-01")]
    original_try_add_unique = uow.snapshots.try_add_unique

    async def racing_try_add_unique(snapshot):
        await uow.snapshots.add(
            RawSnapshot(
                id=uuid4(),
                source_id=src.id,
                ingestion_run_id=run_id,
                payload_hash="different-submission-hash",
                payload_json={"id": "winner"},
                captured_at=datetime.now(UTC),
            )
        )
        return False

    uow.snapshots.try_add_unique = racing_try_add_unique  # type: ignore[method-assign]
    try:
        with pytest.raises(IdempotencyKeyPayloadMismatchError):
            await IngestSourceData(uow, settings=make_settings()).execute(
                source_id=src.id,
                raw_payload=payload,
                ingestion_run_id=run_id,
                claims_data=claims,
            )
    finally:
        uow.snapshots.try_add_unique = original_try_add_unique  # type: ignore[method-assign]

    assert not uow.store.claims


async def test_blank_event_date_rejects_before_writes(uow):
    src = await _add_source(uow)
    run_id = uuid4()

    with pytest.raises(NormalizationError, match="event_date was present but blank"):
        await IngestSourceData(uow, settings=make_settings()).execute(
            source_id=src.id,
            raw_payload={},
            ingestion_run_id=run_id,
            claims_data=[IngestionClaimDTO(field_name="event_date", field_value="   ")],
        )

    assert run_id not in uow.store.ingestion_runs
    assert not uow.store.snapshots
    assert not uow.store.claims


async def test_legacy_raw_payload_hash_snapshot_replays_without_false_mismatch(uow):
    """Rows from the oldest schema may have payload_hash=hash(raw_payload) only."""
    import hashlib
    import json
    from datetime import UTC, datetime

    from atlas.domain.entities import Claim, RawSnapshot

    src = await _add_source(uow)
    event = AccidentEvent(id=uuid4())
    await uow.events.add(event)
    run_id = uuid4()
    raw_payload = {"legacy": "payload"}
    raw_payload_hash = hashlib.sha256(
        json.dumps(raw_payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()
    snapshot = RawSnapshot(
        id=uuid4(),
        source_id=src.id,
        ingestion_run_id=run_id,
        payload_hash=raw_payload_hash,
        payload_json=raw_payload,
        captured_at=datetime.now(UTC),
        raw_payload_hash=None,
        submission_hash=raw_payload_hash,  # migration 016 backfills this from payload_hash
        submission_fingerprint_json=None,
        ingestion_result_json=None,
    )
    await uow.snapshots.add(snapshot)
    await uow.claims.add(
        Claim(
            id=uuid4(),
            event_id=event.id,
            source_id=src.id,
            raw_snapshot_id=snapshot.id,
            field_name="event_date",
            field_value="2024-01-01",
        )
    )

    replay = await IngestSourceData(uow, settings=make_settings()).execute_with_result(
        source_id=src.id,
        raw_payload=raw_payload,
        ingestion_run_id=run_id,
        claims_data=[IngestionClaimDTO(field_name="event_date", field_value="2024-01-01")],
    )

    assert replay.idempotent_replay is True
    assert replay.event_id == event.id
    assert replay.attached_by == "idempotent_replay_legacy_claim_lookup"
    assert len(uow.store.claims) == 1


async def test_update_ingestion_result_missing_snapshot_raises(uow):
    with pytest.raises(RuntimeError, match="Failed to persist ingestion result"):
        await uow.snapshots.update_ingestion_result(uuid4(), {"event_id": str(uuid4())})


async def test_submission_fingerprint_does_not_duplicate_raw_payload(uow):
    src = await _add_source(uow)
    result = await IngestSourceData(uow, settings=make_settings()).execute_with_result(
        source_id=src.id,
        raw_payload={"large": "payload"},
        ingestion_run_id=uuid4(),
        claims_data=[IngestionClaimDTO(field_name="event_date", field_value="2024-01-01")],
    )

    snapshot = next(s for s in uow.store.snapshots.values() if s.ingestion_result_json)
    assert snapshot.submission_fingerprint_json is not None
    assert "raw_payload" not in snapshot.submission_fingerprint_json
    assert snapshot.submission_fingerprint_json["raw_payload_hash"] == snapshot.raw_payload_hash
    assert snapshot.ingestion_result_json["schema_version"] == 1
    assert snapshot.ingestion_result_json["event_id_at_completion"] == str(result.event_id)


async def test_update_ingestion_run_status_missing_run_raises(uow):
    with pytest.raises(RuntimeError, match="Failed to update ingestion run"):
        await uow.ingestion_runs.update_status(uuid4(), "finished")


async def test_source_record_id_is_trimmed_for_fingerprint_and_continuity(uow):
    src = await _add_source(uow)
    run_id = uuid4()
    result = await IngestSourceData(uow, settings=make_settings()).execute_with_result(
        source_id=src.id,
        raw_payload={"record": 1},
        ingestion_run_id=run_id,
        claims_data=[IngestionClaimDTO(field_name="event_date", field_value="2024-01-01")],
        source_record_id="  REC-123  ",
    )

    snapshot = uow.store.snapshots_by_run[(src.id, run_id)]
    assert snapshot.source_record_id == "REC-123"
    assert snapshot.submission_fingerprint_json["source_record_id"] == "REC-123"

    second = await IngestSourceData(uow, settings=make_settings()).execute_with_result(
        source_id=src.id,
        raw_payload={"record": 2},
        ingestion_run_id=uuid4(),
        claims_data=[IngestionClaimDTO(field_name="event_date", field_value="2024-01-02")],
        source_record_id="REC-123",
    )
    assert second.event_id == result.event_id
    assert second.attached_by == "source_record_id"


async def test_stored_ingestion_result_rejects_loose_boolean_strings(uow):
    """Persisted ingestion_result_json with loose JSON types must be rejected.

    The validator raises ``PersistenceCorruptionError`` (5xx-mapped) rather
    than a generic ``ValueError``, because malformed *persisted* data is a
    server-side data-integrity problem, not a client input error.
    """
    from atlas.application.ingestion._idempotency import IngestionIdempotencyService

    event = AccidentEvent(id=uuid4())
    await uow.events.add(event)

    with pytest.raises(PersistenceCorruptionError, match="Malformed stored ingestion_result_json"):
        await IngestionIdempotencyService(uow)._result_from_json(
            {
                "schema_version": 1,
                "event_id_at_completion": str(event.id),
                "event_created": "false",
                "snapshot_created": True,
                "attached_by": "identity_match",
            }
        )


async def test_stored_ingestion_result_rejects_unknown_schema_version(uow):
    """Persisted ingestion_result_json with an unknown schema_version must be rejected.

    Same reasoning as the loose-boolean case: a future-schema row is a
    server-side rollback hazard, not a client error, so it surfaces as
    ``PersistenceCorruptionError`` (5xx) rather than ``ValueError`` (4xx).
    """
    from atlas.application.ingestion._idempotency import IngestionIdempotencyService

    event = AccidentEvent(id=uuid4())
    await uow.events.add(event)

    with pytest.raises(
        PersistenceCorruptionError, match="Unsupported ingestion_result_json schema_version"
    ):
        await IngestionIdempotencyService(uow)._result_from_json(
            {
                "schema_version": 999,
                "event_id_at_completion": str(event.id),
                "event_created": False,
                "snapshot_created": True,
                "attached_by": "identity_match",
            }
        )


async def test_durable_source_field_mapping_maps_plain_date(uow):
    src = Source(
        id=uuid4(),
        name="MappedDateSource",
        kind=SourceKind.EXTERNAL,
        reliability_tier=1,
        field_mapping_json={"date": "event_date"},
    )
    await uow.sources.add(src)
    run_id = uuid4()

    event_id = await IngestSourceData(uow, settings=make_settings()).execute(
        source_id=src.id,
        raw_payload={"id": "mapped-date"},
        ingestion_run_id=run_id,
        claims_data=[
            IngestionClaimDTO(field_name="date", field_value="2024/01/05"),
            IngestionClaimDTO(field_name="tailNumber", field_value="ph-bxo"),
        ],
    )

    by_field = {claim.field_name: claim for claim in uow.store.claims.values()}
    assert by_field["event_date"].field_value == "2024-01-05"
    assert by_field["registration"].field_value == "PH-BXO"
    assert uow.store.identity_index[event_id].event_date_norm == "2024-01-05"

    snapshot = next(iter(uow.store.snapshots.values()))
    assert snapshot.submission_fingerprint_json is not None
    assert snapshot.submission_fingerprint_json["source_mapping_hash"] is not None
    assert snapshot.submission_fingerprint_json["normalizer_version"] == "external-v1"


async def test_invalid_durable_source_field_mapping_target_rejects_before_writes(uow):
    src = Source(
        id=uuid4(),
        name="BadMappingSource",
        kind=SourceKind.EXTERNAL,
        reliability_tier=1,
        field_mapping_json={"date": "event_dat"},
    )
    await uow.sources.add(src)
    run_id = uuid4()

    # The use case now wraps the underlying ``ValueError`` from
    # ``SourceFieldMapper.__init__`` as a typed ``DomainValidationError`` so
    # the API surfaces a 400 (and operators get a structured error code) rather
    # than a generic 500 ``ValueError`` leaking from infrastructure.  The
    # original error message is preserved as the ``__cause__`` of the wrapper.
    with pytest.raises(DomainValidationError, match=r"Invalid Source\.field_mapping_json"):
        await IngestSourceData(uow, settings=make_settings()).execute(
            source_id=src.id,
            raw_payload={"id": "bad-map"},
            ingestion_run_id=run_id,
            claims_data=[IngestionClaimDTO(field_name="date", field_value="2024/01/05")],
        )

    assert not uow.store.snapshots
    assert run_id not in uow.store.ingestion_runs
