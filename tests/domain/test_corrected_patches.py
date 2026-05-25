from __future__ import annotations

from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from atlas.application.dto import IngestionClaimDTO
from atlas.application.ingestion._claim_writer import ClaimWriter
from atlas.application.ingestion._conflict_reconciler import ConflictReconciler
from atlas.application.use_cases.ingest_source_data import IngestSourceData
from atlas.application.use_cases.reproject_event import ReProjectEvent
from atlas.domain.entities import AccidentEvent, Claim, ClaimConflict, Source
from atlas.domain.enums import ClaimType, ConflictStatus, SourceKind
from atlas.domain.exceptions import ConflictReconciliationError, IngestionRunSourceMismatchError
from tests.domain._fake_uow import InMemoryUnitOfWork, make_settings


async def test_claimwriter_write_forwards_source_field_mapping() -> None:
    uow = InMemoryUnitOfWork()
    source = Source(id=uuid4(), name="mapped", kind=SourceKind.EXTERNAL, reliability_tier=1)
    event = AccidentEvent(id=uuid4())
    await uow.sources.add(source)
    await uow.events.add(event)

    writer = ClaimWriter(uow)
    result = await writer.write(
        event_id=event.id,
        source_id=source.id,
        snapshot_id=uuid4(),
        source_kind=SourceKind.EXTERNAL,
        claims_data=[{"field_name": "tail_no", "field_value": "n-123-ab"}],
        ingestion_run_id=uuid4(),
        source_record_id=None,
        source_field_mapping={"tail_no": "registration"},
    )

    assert [claim.field_name for claim in result.new_claims] == ["registration"]
    assert result.new_claims[0].field_value == "N-123-AB"


def test_extract_normalised_fields_respects_source_field_mapping() -> None:
    writer = ClaimWriter(InMemoryUnitOfWork())

    fields = writer.extract_normalised_fields(
        SourceKind.EXTERNAL,
        [{"field_name": "tail_no", "field_value": "n-123-ab"}],
        source_field_mapping={"tail_no": "registration"},
    )

    assert fields == {"registration": "N-123-AB"}


async def test_ensure_started_rejects_same_run_for_different_source() -> None:
    uow = InMemoryUnitOfWork()
    run_id = uuid4()
    source_a = uuid4()
    source_b = uuid4()

    await uow.ingestion_runs.ensure_started(run_id, source_a)
    await uow.ingestion_runs.ensure_started(run_id, source_a)
    with pytest.raises(IngestionRunSourceMismatchError) as exc_info:
        await uow.ingestion_runs.ensure_started(run_id, source_b)

    assert exc_info.value.expected_source_id == source_b
    assert exc_info.value.actual_source_id == source_a


async def test_reproject_restores_missing_projection_from_existing_outbox_history() -> None:
    uow = InMemoryUnitOfWork()
    settings = make_settings()
    source = Source(id=uuid4(), name="S", kind=SourceKind.EXTERNAL, reliability_tier=1)
    await uow.sources.add(source)

    event_id = await IngestSourceData(uow, settings=settings).execute(
        source_id=source.id,
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
    assert len(uow.store.projection_history) == 1

    del uow.store.projections[event_id]
    restored = await ReProjectEvent(uow).execute(
        event_id=event_id,
        caused_by_outbox_event_id=outbox_event_id,
        commit=False,
    )

    assert restored.event_id == event_id
    assert restored.projection_version == first.projection_version
    assert restored.fields == first.fields
    assert uow.store.projections[event_id].fields == first.fields
    assert len(uow.store.projection_history) == 1


async def test_merge_evidence_raises_after_retry_exhaustion() -> None:
    uow = InMemoryUnitOfWork()
    event_id = uuid4()
    claim_a = Claim(
        id=uuid4(),
        event_id=event_id,
        source_id=uuid4(),
        field_name="location",
        field_value="Paris",
        claim_type=ClaimType.RAW,
    )
    claim_b = Claim(
        id=uuid4(),
        event_id=event_id,
        source_id=uuid4(),
        field_name="location",
        field_value="Lyon",
        claim_type=ClaimType.RAW,
    )
    uow.store.claims[claim_a.id] = claim_a
    uow.store.claims[claim_b.id] = claim_b
    conflict = ClaimConflict(
        id=uuid4(),
        event_id=event_id,
        field_name="location",
        status=ConflictStatus.OPEN,
    )
    await uow.conflicts.add(conflict)
    await uow.conflicts.add_claim_to_conflict(conflict.id, claim_a.id)
    open_conflict = await uow.conflicts.get(conflict.id)
    assert open_conflict is not None

    uow.conflicts.update_with_version_check = AsyncMock(return_value=None)  # type: ignore[method-assign]
    uow.conflicts.get = AsyncMock(return_value=open_conflict)  # type: ignore[method-assign]

    with pytest.raises(ConflictReconciliationError) as exc_info:
        await ConflictReconciler(uow)._merge_evidence_into_open(
            open_conflict,
            [claim_b.id],
            uuid4(),
            "test retry exhaustion",
        )

    assert exc_info.value.operation == "merge_evidence_into_open"


async def test_auto_resolve_raises_after_retry_exhaustion() -> None:
    uow = InMemoryUnitOfWork()
    event_id = uuid4()
    claim = Claim(
        id=uuid4(),
        event_id=event_id,
        source_id=uuid4(),
        field_name="operator",
        field_value="AirX",
        claim_type=ClaimType.RAW,
    )
    uow.store.claims[claim.id] = claim
    conflict = ClaimConflict(
        id=uuid4(),
        event_id=event_id,
        field_name="operator",
        status=ConflictStatus.OPEN,
    )
    await uow.conflicts.add(conflict)
    await uow.conflicts.add_claim_to_conflict(conflict.id, claim.id)
    open_conflict = await uow.conflicts.get(conflict.id)
    assert open_conflict is not None

    uow.conflicts.update_with_version_check = AsyncMock(return_value=None)  # type: ignore[method-assign]
    uow.conflicts.get = AsyncMock(return_value=open_conflict)  # type: ignore[method-assign]
    uow.conflicts.find_open_by_event_field = AsyncMock(return_value=open_conflict)  # type: ignore[method-assign]
    uow.claims.find_active_by_event_field = AsyncMock(return_value=[claim])  # type: ignore[method-assign]

    with pytest.raises(ConflictReconciliationError) as exc_info:
        await ConflictReconciler(uow)._auto_resolve_stale_open_conflict(
            event_id,
            "operator",
            uuid4(),
        )

    assert exc_info.value.operation == "auto_resolve_stale_open"


async def test_reopen_resolved_raises_after_retry_exhaustion() -> None:
    uow = InMemoryUnitOfWork()
    event_id = uuid4()
    claim = Claim(
        id=uuid4(),
        event_id=event_id,
        source_id=uuid4(),
        field_name="operator",
        field_value="AirX",
        claim_type=ClaimType.RAW,
    )
    uow.store.claims[claim.id] = claim
    conflict = ClaimConflict(
        id=uuid4(),
        event_id=event_id,
        field_name="operator",
        status=ConflictStatus.RESOLVED,
        winning_claim_id=claim.id,
    )
    await uow.conflicts.add(conflict)
    resolved_conflict = await uow.conflicts.get(conflict.id)
    assert resolved_conflict is not None

    uow.conflicts.update_with_version_check = AsyncMock(return_value=None)  # type: ignore[method-assign]
    uow.conflicts.get = AsyncMock(return_value=resolved_conflict)  # type: ignore[method-assign]

    with pytest.raises(ConflictReconciliationError) as exc_info:
        await ConflictReconciler(uow)._reopen_resolved_for_evidence(
            resolved_conflict,
            [claim.id],
            uuid4(),
        )

    assert exc_info.value.operation == "reopen_resolved_for_evidence"
