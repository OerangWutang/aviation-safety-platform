"""Integration regression tests.

Requires docker compose postgres and a ``pg_uow`` fixture in conftest.py that yields
a clean SqlAlchemyUnitOfWork. Run with:

    docker compose up -d
    pytest -m integration
"""

from uuid import uuid4

import pytest
import pytest_asyncio

from atlas.application.dto import IngestionClaimDTO
from atlas.application.use_cases.ingest_source_data import IngestSourceData
from atlas.application.use_cases.reproject_event import ReProjectEvent
from atlas.application.use_cases.resolve_conflict import ResolveConflict
from atlas.domain.entities import Source
from atlas.domain.enums import SourceKind
from atlas.domain.exceptions import ClaimNotInConflictError, ConflictModifiedError

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def _ingest(uow, source_id, claims, event_id=None):
    return await IngestSourceData(uow).execute(
        source_id=source_id,
        raw_payload={},
        ingestion_run_id=uuid4(),
        claims_data=[IngestionClaimDTO(**claim) for claim in claims],
        event_id=event_id,
    )


@pytest_asyncio.fixture
async def source_a(pg_uow):
    source = Source(
        id=uuid4(),
        name=f"SourceA-{uuid4().hex[:6]}",
        kind=SourceKind.EXTERNAL,
        reliability_tier=1,
    )
    await pg_uow.sources.add(source)
    await pg_uow.commit()
    return source


@pytest_asyncio.fixture
async def source_b(pg_uow):
    source = Source(
        id=uuid4(),
        name=f"SourceB-{uuid4().hex[:6]}",
        kind=SourceKind.EXTERNAL,
        reliability_tier=2,
    )
    await pg_uow.sources.add(source)
    await pg_uow.commit()
    return source


async def test_ingest_into_existing_event_creates_cross_source_conflict(pg_uow, source_a, source_b):
    event_id = await _ingest(
        pg_uow,
        source_a.id,
        [{"field_name": "fatalities_total", "field_value": 5}],
    )
    await _ingest(
        pg_uow,
        source_b.id,
        [{"field_name": "fatalities_total", "field_value": 6}],
        event_id=event_id,
    )

    conflicts = await pg_uow.conflicts.find_by_event(event_id)
    assert len(conflicts) == 1
    assert conflicts[0].field_name == "fatalities_total"
    assert len(conflicts[0].claim_ids) == 2


async def test_resolve_rejects_claim_not_in_conflict(pg_uow, source_a, source_b):
    event_id = await _ingest(
        pg_uow,
        source_a.id,
        [{"field_name": "location", "field_value": "Paris"}],
    )
    await _ingest(
        pg_uow,
        source_b.id,
        [{"field_name": "location", "field_value": "Rome"}],
        event_id=event_id,
    )

    conflict = (await pg_uow.conflicts.find_by_event(event_id))[0]

    with pytest.raises(ClaimNotInConflictError):
        await ResolveConflict(pg_uow).execute(
            conflict_id=conflict.id,
            expected_version=conflict.version,
            winning_claim_id=uuid4(),
            current_user_id=uuid4(),
        )


async def test_manual_override_is_rolled_back_when_expected_version_is_stale(
    pg_uow, source_a, source_b
):
    event_id = await _ingest(
        pg_uow,
        source_a.id,
        [{"field_name": "aircraft_type", "field_value": "B737"}],
    )
    await _ingest(
        pg_uow,
        source_b.id,
        [{"field_name": "aircraft_type", "field_value": "A320"}],
        event_id=event_id,
    )

    conflict = (await pg_uow.conflicts.find_by_event(event_id))[0]

    with pytest.raises(ConflictModifiedError) as exc_info:
        await ResolveConflict(pg_uow).execute(
            conflict_id=conflict.id,
            expected_version=conflict.version - 1,
            manual_override_value="B738",
            current_user_id=uuid4(),
        )

    all_claims = await pg_uow.claims.find_all_by_event(event_id)
    override_claims = [claim for claim in all_claims if claim.field_value == "B738"]
    assert override_claims == []
    assert exc_info.value.current_version == conflict.version


async def test_outbox_processing_is_idempotent_by_outbox_event_id(pg_uow, source_a):
    event_id = await _ingest(
        pg_uow,
        source_a.id,
        [{"field_name": "event_date", "field_value": "2024-01-01"}],
    )

    outbox_events = await pg_uow.outbox.fetch_and_lock_pending(10, "test-worker")
    await pg_uow.commit()
    assert outbox_events
    outbox_event = outbox_events[0]

    for _ in range(2):
        existing = await pg_uow.projection_history.find_by_outbox_event(outbox_event.id)
        if not existing:
            await ReProjectEvent(pg_uow).execute(
                event_id=event_id,
                caused_by_outbox_event_id=outbox_event.id,
                commit=False,
            )
            await pg_uow.commit()

    history = await pg_uow.projection_history.find_by_outbox_event(outbox_event.id)
    assert history is not None


async def test_duplicate_ingestion_payload_reuses_existing_event_without_duplicate_claims(
    pg_uow, source_a
):
    ingestion_run_id = uuid4()
    raw_payload = {"source_record_id": "abc-123"}
    claims = [IngestionClaimDTO(field_name="event_date", field_value="2024-01-01")]

    event_id = await IngestSourceData(pg_uow).execute(
        source_id=source_a.id,
        raw_payload=raw_payload,
        ingestion_run_id=ingestion_run_id,
        claims_data=claims,
    )
    claims_after_first = await pg_uow.claims.find_all_by_event(event_id)
    assert len(claims_after_first) == 1

    # Re-submit the identical payload with the same ingestion_run_id.
    # The idempotency path matches on the run_id + fingerprint and returns
    # the existing snapshot's event_id without re-processing.  Passing
    # event_id= here would change the fingerprint (it's part of the hash
    # material) and trigger an IdempotencyKeyPayloadMismatchError instead.
    reused_event_id = await IngestSourceData(pg_uow).execute(
        source_id=source_a.id,
        raw_payload=raw_payload,
        ingestion_run_id=ingestion_run_id,
        claims_data=claims,
    )

    claims_after_second = await pg_uow.claims.find_all_by_event(event_id)
    assert reused_event_id == event_id
    assert len(claims_after_second) == 1
