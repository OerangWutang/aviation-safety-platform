"""Tests for ReProjectEvent and outbox-driven idempotency at the use-case layer."""

from __future__ import annotations

from uuid import uuid4

import pytest

from atlas.application.dto import IngestionClaimDTO
from atlas.application.use_cases.ingest_source_data import IngestSourceData
from atlas.application.use_cases.reproject_event import ReProjectEvent
from atlas.application.use_cases.resolve_conflict import ResolveConflict
from atlas.domain.entities import (
    AccidentEvent,
    AccidentProjectionHistory,
    ProjectedAccidentRecord,
    Source,
)
from atlas.domain.enums import OutboxStatus, SourceKind
from atlas.domain.exceptions import EventNotFoundError
from tests.domain._fake_uow import InMemoryUnitOfWork, make_settings


@pytest.fixture
async def two_source_uow():
    uow = InMemoryUnitOfWork()
    settings = make_settings()
    override = Source(
        id=settings.curator_override_source_id,
        name=settings.curator_override_source_name,
        kind=SourceKind.INTERNAL,
        reliability_tier=1,
    )
    await uow.sources.add(override)
    src_a = Source(id=uuid4(), name="A", kind=SourceKind.EXTERNAL, reliability_tier=1)
    src_b = Source(id=uuid4(), name="B", kind=SourceKind.EXTERNAL, reliability_tier=2)
    await uow.sources.add(src_a)
    await uow.sources.add(src_b)
    return uow, settings, src_a, src_b


async def test_reproject_is_idempotent_for_same_outbox_event_id(two_source_uow):
    uow, settings, src_a, _ = two_source_uow
    event_id = await IngestSourceData(uow, settings=settings).execute(
        source_id=src_a.id,
        raw_payload={},
        ingestion_run_id=uuid4(),
        claims_data=[IngestionClaimDTO(field_name="event_date", field_value="2024-01-01")],
    )
    outbox_event_id = uow.store.outbox[0].id

    # First projection driven by the outbox event.
    p1 = await ReProjectEvent(uow).execute(
        event_id=event_id,
        caused_by_outbox_event_id=outbox_event_id,
        commit=False,
    )
    history_after_first = list(uow.store.projection_history)
    assert len(history_after_first) == 1

    # Re-running with the same outbox event id MUST be a no-op (return current
    # projection, no new history rows). This matches the production behavior
    # of the outbox worker on retry.
    p2 = await ReProjectEvent(uow).execute(
        event_id=event_id,
        caused_by_outbox_event_id=outbox_event_id,
        commit=False,
    )
    assert p2.projection_version == p1.projection_version
    assert len(uow.store.projection_history) == 1


async def test_changed_fields_includes_unresolved_conflict_fields_when_state_changes(
    two_source_uow,
):
    """Resolving a conflict moves a field from DISPUTED to a concrete value;
    that transition must be reflected in ``changed_fields``."""
    uow, settings, src_a, src_b = two_source_uow
    event_id = await IngestSourceData(uow, settings=settings).execute(
        source_id=src_a.id,
        raw_payload={"r": 1},
        ingestion_run_id=uuid4(),
        claims_data=[IngestionClaimDTO(field_name="fatalities_total", field_value=5)],
    )
    await IngestSourceData(uow, settings=settings).execute(
        source_id=src_b.id,
        raw_payload={"r": 2},
        ingestion_run_id=uuid4(),
        claims_data=[IngestionClaimDTO(field_name="fatalities_total", field_value=6)],
        event_id=event_id,
    )

    # Build the initial DISPUTED projection.
    await ReProjectEvent(uow).execute(event_id=event_id, commit=False)
    initial_history = list(uow.store.projection_history)
    assert (
        "fatalities_total"
        in initial_history[-1].projected_record_snapshot["unresolved_conflict_fields"]
    )

    # Resolve, which internally reprojects.
    conflict = next(iter(uow.store.conflicts.values()))
    winner_id = uow.store.conflict_claim_links[conflict.id][0]
    await ResolveConflict(uow, settings=settings).execute(
        conflict_id=conflict.id,
        expected_version=conflict.version,
        winning_claim_id=winner_id,
        current_user_id=uuid4(),
        reason="picking source A",
    )

    # The reproject driven by resolve_conflict added a new history row.
    final_history = list(uow.store.projection_history)
    assert len(final_history) > len(initial_history)
    last = final_history[-1]
    # Both the now-concrete field AND the unresolved_conflict_fields list
    # changed -> both must appear in changed_fields.
    assert last.changed_fields is not None
    assert "fatalities_total" in last.changed_fields
    assert "unresolved_conflict_fields" in last.changed_fields


async def test_first_projection_changed_fields_is_all_field_names(two_source_uow):
    uow, settings, src_a, _ = two_source_uow
    event_id = await IngestSourceData(uow, settings=settings).execute(
        source_id=src_a.id,
        raw_payload={},
        ingestion_run_id=uuid4(),
        claims_data=[
            IngestionClaimDTO(field_name="event_date", field_value="2024-01-01"),
            IngestionClaimDTO(field_name="location", field_value="Amsterdam"),
        ],
    )
    proj = await ReProjectEvent(uow).execute(event_id=event_id, commit=False)

    history = uow.store.projection_history[-1]
    assert sorted(history.changed_fields) == ["event_date", "location"]
    assert proj.completeness_score == pytest.approx(2 / 9)


async def test_outbox_pending_status_after_ingestion(two_source_uow):
    uow, settings, src_a, _ = two_source_uow
    await IngestSourceData(uow, settings=settings).execute(
        source_id=src_a.id,
        raw_payload={},
        ingestion_run_id=uuid4(),
        claims_data=[IngestionClaimDTO(field_name="event_date", field_value="2024-01-01")],
    )
    assert len(uow.store.outbox) == 1
    assert uow.store.outbox[0].status == OutboxStatus.PENDING


async def test_outbox_fetch_and_lock_marks_processing_and_increments_attempts(two_source_uow):
    uow, settings, src_a, _ = two_source_uow
    await IngestSourceData(uow, settings=settings).execute(
        source_id=src_a.id,
        raw_payload={},
        ingestion_run_id=uuid4(),
        claims_data=[IngestionClaimDTO(field_name="event_date", field_value="2024-01-01")],
    )

    locked = await uow.outbox.fetch_and_lock_pending(10, "worker-1")
    assert len(locked) == 1
    assert locked[0].status == OutboxStatus.PROCESSING
    assert locked[0].attempt_count == 1
    assert locked[0].locked_by == "worker-1"


async def test_reproject_merged_event_writes_tombstone(two_source_uow):
    uow, settings, src_a, _ = two_source_uow
    source_id = await IngestSourceData(uow, settings=settings).execute(
        source_id=src_a.id,
        raw_payload={"event_date": "2024-01-01"},
        ingestion_run_id=uuid4(),
        claims_data=[IngestionClaimDTO(field_name="event_date", field_value="2024-01-01")],
    )
    target_id = await IngestSourceData(uow, settings=settings).execute(
        source_id=src_a.id,
        raw_payload={"event_date": "2024-01-02"},
        ingestion_run_id=uuid4(),
        claims_data=[IngestionClaimDTO(field_name="event_date", field_value="2024-01-02")],
    )

    uow.store.events[source_id].merged_into_event_id = target_id
    projection = await ReProjectEvent(uow).execute(source_id, commit=False)

    assert projection.fields == {
        "is_merged": True,
        "merged_into_event_id": str(target_id),
    }
    assert projection.unresolved_conflict_fields == []
    assert projection.completeness_score == 0.0
    assert uow.store.projections[source_id].fields["is_merged"] is True


async def test_reproject_existing_outbox_history_for_merged_event_does_not_restore_stale_projection(
    two_source_uow,
):
    uow, settings, src_a, _ = two_source_uow
    event_id = await IngestSourceData(uow, settings=settings).execute(
        source_id=src_a.id,
        raw_payload={"event_date": "2024-01-01"},
        ingestion_run_id=uuid4(),
        claims_data=[IngestionClaimDTO(field_name="event_date", field_value="2024-01-01")],
    )
    outbox_event_id = uow.store.outbox[0].id
    first = await ReProjectEvent(uow).execute(
        event_id=event_id,
        caused_by_outbox_event_id=outbox_event_id,
        commit=False,
    )
    assert first.fields == {"event_date": "2024-01-01"}

    target_id = uuid4()
    await uow.events.add(AccidentEvent(id=target_id))
    uow.store.events[event_id].merged_into_event_id = target_id
    del uow.store.projections[event_id]

    tombstone = await ReProjectEvent(uow).execute(
        event_id=event_id,
        caused_by_outbox_event_id=outbox_event_id,
        commit=False,
    )

    assert tombstone.fields == {
        "is_merged": True,
        "merged_into_event_id": str(target_id),
    }
    assert uow.store.projections[event_id].fields == tombstone.fields
    assert [
        history.caused_by_outbox_event_id
        for history in uow.store.projection_history
        if history.caused_by_outbox_event_id == outbox_event_id
    ] == [outbox_event_id]


# ── Missing-event / outbox-history behaviour (pinning r4 invariants) ──────────


@pytest.mark.asyncio
async def test_reproject_missing_event_no_claims_raises_event_not_found_error():
    """Normal reprojection of a non-existent event must raise EventNotFoundError
    (a typed 404) rather than silently building an empty projection.

    This is the contract documented in ReProjectEvent: if neither the event row
    nor any orphan claims exist, there is nothing to project and the caller
    should receive a clean 404-style error.
    """
    uow = InMemoryUnitOfWork()
    missing_event_id = uuid4()

    with pytest.raises(EventNotFoundError, match=str(missing_event_id)):
        await ReProjectEvent(uow).execute(missing_event_id, commit=False)


@pytest.mark.asyncio
async def test_reproject_missing_event_with_outbox_history_returns_existing_projection():
    """When an outbox worker fires for an event that has been deleted or is
    otherwise missing, but projection history already exists for the same
    outbox event id, the use case must return idempotently rather than raising.

    This is the deliberate repair flow documented in ReProjectEvent: the outbox
    worker needs a successful acknowledgement to stop retrying, so we honour
    the existing history snapshot and return its projection.

    The test also pins the warning-level logging path (no assert on the log
    text, but the use case must not error).
    """
    uow = InMemoryUnitOfWork()
    event_id = uuid4()
    outbox_event_id = uuid4()

    # Seed projection history for the outbox event — simulates a prior
    # successful run before the event was deleted.
    history = AccidentProjectionHistory(
        accident_event_id=event_id,
        projection_version=1,
        caused_by_outbox_event_id=outbox_event_id,
        projected_record_snapshot={"tail_number": "N12345"},
        projected_record_hash="abc123",
    )
    await uow.projection_history.add(history)

    # Also seed the current projection row (what the history corresponds to).
    projection = ProjectedAccidentRecord(
        event_id=event_id,
        projection_version=1,
        fields={"tail_number": "N12345"},
    )
    uow.store.projections[event_id] = projection

    # No AccidentEvent row — simulates a deleted event.
    result = await ReProjectEvent(uow).execute(
        event_id,
        caused_by_outbox_event_id=outbox_event_id,
        commit=False,
    )

    # Must return the existing projection, not raise.
    assert result.fields == {"tail_number": "N12345"}
    assert result.projection_version == 1


@pytest.mark.asyncio
async def test_reproject_missing_event_with_outbox_history_but_no_projection_restores_from_history():
    """Edge case: outbox history exists but the current projection row was lost
    (e.g. deleted during a manual repair).  The use case must restore the
    projection from the history snapshot instead of raising.
    """
    uow = InMemoryUnitOfWork()
    event_id = uuid4()
    outbox_event_id = uuid4()

    history = AccidentProjectionHistory(
        accident_event_id=event_id,
        projection_version=2,
        caused_by_outbox_event_id=outbox_event_id,
        projected_record_snapshot={"tail_number": "N99999"},
        projected_record_hash="def456",
    )
    await uow.projection_history.add(history)
    # No current projection row seeded — simulate the lost row.

    result = await ReProjectEvent(uow).execute(
        event_id,
        caused_by_outbox_event_id=outbox_event_id,
        commit=False,
    )

    # Should have restored the projection from history.
    assert result.event_id == event_id
    assert result.projection_version == 2


@pytest.mark.asyncio
async def test_reproject_missing_event_no_outbox_raises_immediately():
    """When there is no outbox event id (direct reprojection call) and the
    event does not exist, we must raise EventNotFoundError without attempting
    to build an empty projection.
    """
    uow = InMemoryUnitOfWork()
    with pytest.raises(EventNotFoundError):
        await ReProjectEvent(uow).execute(
            uuid4(),
            caused_by_outbox_event_id=None,
            commit=False,
        )
