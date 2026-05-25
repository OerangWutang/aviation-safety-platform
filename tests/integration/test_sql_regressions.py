"""SQL-backed regression tests for identity matching, merge provenance,
canonicalization, and concurrent source-record corrections.

These tests run against a real PostgreSQL instance and exercise behaviours that
the in-memory fake UoW cannot guarantee:

  - JSONB GIN index lookup for registration aliases
  - Direct registration lookup that bypasses the 50-row date cap
  - Concurrent corrections to the same source_record_id (advisory lock)
  - Merge provenance preservation through SQL-backed repositories
  - Merged event canonicalization in QueryProvenance
  - Merge direction correctness via SQL UoW

Requires:
    ATLAS_ALLOW_DB_TRUNCATE=1 pytest -m integration --run-integration
"""

from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest

from atlas.application.dto import IngestionClaimDTO
from atlas.application.use_cases.ingest_source_data import IngestSourceData
from atlas.application.use_cases.merge_duplicate_events import MergeDuplicateEvents
from atlas.application.use_cases.query_provenance import QueryProvenance
from atlas.domain.entities import Source
from atlas.domain.enums import ClaimType, SourceKind
from atlas.infrastructure.db.unit_of_work import SqlAlchemyUnitOfWork

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


# ── helpers ──────────────────────────────────────────────────────────────────


async def _make_source(session_factory, tier: int = 1) -> Source:
    src = Source(
        id=uuid4(),
        name=f"test-source-{uuid4().hex[:8]}",
        kind=SourceKind.EXTERNAL,
        reliability_tier=tier,
    )
    async with session_factory() as session:
        uow = SqlAlchemyUnitOfWork(session)
        await uow.sources.add(src)
        await uow.commit()
    return src


async def _ingest(session_factory, source_id, claims, source_record_id=None, event_id=None):
    async with session_factory() as session:
        uow = SqlAlchemyUnitOfWork(session)
        return await IngestSourceData(uow).execute(
            source_id=source_id,
            raw_payload={"r": uuid4().hex},
            ingestion_run_id=uuid4(),
            claims_data=[IngestionClaimDTO(**c) for c in claims],
            source_record_id=source_record_id,
            event_id=event_id,
        )


# ── registration alias lookup (GIN index) ────────────────────────────────────


async def test_registration_alias_lookup_via_gin_index(pg_uow, test_session_factory):
    """After an event is ingested once with registration N-OLD and then updated
    to N-NEW via a second ingestion, a third ingestion carrying N-OLD must
    still resolve to the *same* event through the registration_norms alias array
    (backed by the GIN index from migration 013).

    This proves that find_by_registration() uses the JSONB @> operator against
    registration_norms, not just the primary registration_norm column.
    """
    src = await _make_source(test_session_factory)

    # First ingestion - registration N-OLD
    event_id = await _ingest(
        test_session_factory,
        src.id,
        [
            {"field_name": "registration", "field_value": "N-OLD", "source_tier": 1},
            {"field_name": "event_date", "field_value": "2024-06-01", "source_tier": 1},
        ],
    )

    # Second ingestion - same source_record_id, registration updated to N-NEW.
    # This triggers the source-record continuity path which should update
    # identity index and accumulate N-OLD in registration_norms.
    event_id_2 = await _ingest(
        test_session_factory,
        src.id,
        [
            {"field_name": "registration", "field_value": "N-NEW", "source_tier": 1},
            {"field_name": "event_date", "field_value": "2024-06-01", "source_tier": 1},
        ],
        source_record_id="NTSB-2024-001",
        event_id=event_id,  # explicit routing to same event
    )
    assert event_id_2 == event_id, "Second ingestion should attach to existing event"

    # Third ingestion - new submission carrying the OLD registration.
    # Must resolve to the same event (via registration_norms alias) rather than
    # creating a new one.
    async with test_session_factory() as session:
        uow = SqlAlchemyUnitOfWork(session)
        idx_entries = await uow.identity_index.find_by_registration(
            registration_norm="N-OLD",
            event_date_norm="2024-06-01",
        )

    assert idx_entries, "find_by_registration must find event via registration alias"
    resolved_ids = {e.event_id for e in idx_entries}
    assert event_id in resolved_ids, (
        f"Expected event {event_id} in results via alias lookup; got {resolved_ids}"
    )


async def test_direct_registration_lookup_bypasses_date_cap(pg_uow, test_session_factory):
    """find_by_registration must return results even when more than 50 events
    share the same date (bypassing find_candidates' LIMIT 50).

    We create 55 events on the same date then verify find_by_registration
    returns the specific event identified by registration without being
    truncated by the date cap.
    """
    src = await _make_source(test_session_factory)
    target_reg = f"N-TARGET-{uuid4().hex[:6]}"
    shared_date = "2024-07-04"

    # Create 52 filler events on the same date (beyond the 50-row cap).
    filler_ids = []
    for i in range(52):
        eid = await _ingest(
            test_session_factory,
            src.id,
            [
                {
                    "field_name": "registration",
                    "field_value": f"N-FILLER-{i:03d}",
                    "source_tier": 1,
                },
                {"field_name": "event_date", "field_value": shared_date, "source_tier": 1},
            ],
        )
        filler_ids.append(eid)

    # Create the specific target event.
    target_event_id = await _ingest(
        test_session_factory,
        src.id,
        [
            {"field_name": "registration", "field_value": target_reg, "source_tier": 1},
            {"field_name": "event_date", "field_value": shared_date, "source_tier": 1},
        ],
    )

    # find_candidates (date window, LIMIT 50) would miss it.
    async with test_session_factory() as session:
        uow = SqlAlchemyUnitOfWork(session)
        candidates = await uow.identity_index.find_candidates(shared_date, limit=50)
        by_reg = await uow.identity_index.find_by_registration(
            registration_norm=target_reg,
            event_date_norm=shared_date,
        )

    candidate_ids = {e.event_id for e in candidates}
    by_reg_ids = {e.event_id for e in by_reg}

    # find_candidates may or may not include target (depends on ordering),
    # but find_by_registration MUST include it.
    assert target_event_id in by_reg_ids, (
        f"find_by_registration must find the target event; got {by_reg_ids}"
    )
    # If find_candidates missed it (likely since we have 53 events), assert
    # that the targeted lookup overcomes the date cap.
    if target_event_id not in candidate_ids:
        assert target_event_id in by_reg_ids, (
            "Direct registration lookup must bypass the date-cap limitation"
        )


# ── concurrent corrections to same source_record_id ─────────────────────────


async def test_concurrent_corrections_same_source_record_serialize(pg_uow, test_session_factory):
    """Two concurrent re-ingestions of the same source_record_id must not
    create two new events or corrupt the identity index.

    The advisory lock in SourceRecordContinuityService serialises the pair; the
    second transaction should find the event already resolved by the first and
    attach to it (or be idempotent) rather than creating a duplicate.
    """
    src = await _make_source(test_session_factory)
    record_id = f"NTSB-{uuid4().hex[:12]}"

    # Seed an initial ingestion so there is an existing event with this record.
    base_event_id = await _ingest(
        test_session_factory,
        src.id,
        [
            {"field_name": "event_date", "field_value": "2024-08-10", "source_tier": 1},
            {"field_name": "registration", "field_value": "N-RACE-0", "source_tier": 1},
        ],
        source_record_id=record_id,
        event_id=None,
    )

    # Two concurrent corrections arriving at the same time.
    go = asyncio.Event()
    resolved_ids: list = []

    async def _correct(tail: str) -> None:
        async with test_session_factory() as session:
            uow = SqlAlchemyUnitOfWork(session)
            await go.wait()
            eid = await IngestSourceData(uow).execute(
                source_id=src.id,
                raw_payload={"r": uuid4().hex, "tail": tail},
                ingestion_run_id=uuid4(),
                claims_data=[
                    IngestionClaimDTO(
                        field_name="registration",
                        field_value=tail,
                        source_tier=1,
                    ),
                    IngestionClaimDTO(
                        field_name="event_date",
                        field_value="2024-08-10",
                        source_tier=1,
                    ),
                ],
                source_record_id=record_id,
            )
            resolved_ids.append(eid)

    t1 = asyncio.create_task(_correct("N-RACE-1"))
    t2 = asyncio.create_task(_correct("N-RACE-2"))
    go.set()
    await asyncio.gather(t1, t2, return_exceptions=True)

    # Both corrections must resolve to the same base event - no new events.
    assert len(resolved_ids) == 2, f"Expected 2 results, got {resolved_ids}"
    unique_ids = set(resolved_ids)
    assert len(unique_ids) == 1, (
        f"Concurrent corrections created multiple events: {unique_ids}. "
        f"The advisory lock should serialize them to the same event."
    )
    assert base_event_id in unique_ids, (
        f"Corrections should resolve to base event {base_event_id}; got {unique_ids}"
    )


# ── merge provenance preservation ────────────────────────────────────────────


async def test_merge_provenance_preserved_through_sql_repos(pg_uow, test_session_factory):
    """After a merge, the target event's active claims include the transferred
    claims from the source, and QueryProvenance returns them with their original
    claim_type and created_by intact.

    This test exercises the full SQL repository stack - not the in-memory fake.
    """
    src = await _make_source(test_session_factory, tier=1)
    admin_id = uuid4()

    source_event_id = await _ingest(
        test_session_factory,
        src.id,
        [
            {"field_name": "registration", "field_value": "N-SOURCE", "source_tier": 1},
            {"field_name": "location", "field_value": "KLAX", "source_tier": 1},
        ],
    )
    target_event_id = await _ingest(
        test_session_factory,
        src.id,
        [{"field_name": "event_date", "field_value": "2024-09-01", "source_tier": 1}],
    )

    # Perform the merge.
    async with test_session_factory() as session:
        uow = SqlAlchemyUnitOfWork(session)
        result = await MergeDuplicateEvents(uow).execute(
            source_event_id=source_event_id,
            target_event_id=target_event_id,
            resolved_by=admin_id,
            note="provenance test merge",
        )
    assert result.claims_transferred == 2

    # Verify via QueryProvenance that the target now has all three fields and
    # that the transferred claims have claim_type = RAW (not downgraded).
    async with test_session_factory() as session:
        uow = SqlAlchemyUnitOfWork(session)
        prov = await QueryProvenance(uow).execute(target_event_id, canonicalize=False)

    active_claims = [c for c in prov["claims"] if c["claim_type"] in (ClaimType.RAW.value, "RAW")]
    field_names = {c["field_name"] for c in active_claims}
    assert "registration" in field_names, "Transferred registration claim must appear in provenance"
    assert "location" in field_names, "Transferred location claim must appear in provenance"
    assert "event_date" in field_names, "Original target claim must still be present"

    # Confirm the claim_histories include the 'merged' action entries.
    merge_histories = [h for h in prov["claim_histories"] if h["action"] == "merged"]
    assert len(merge_histories) >= 2, (
        f"Expected >= 2 'merged' history entries; got {len(merge_histories)}"
    )


# ── canonicalization in QueryProvenance ──────────────────────────────────────


async def test_query_provenance_canonicalizes_absorbed_event(pg_uow, test_session_factory):
    """When an absorbed event's id is queried with canonicalize=True (the
    default), QueryProvenance must transparently return the surviving event's
    provenance and set absorbed_event_id in the response.

    When canonicalize=False, the absorbed event's own (pre-merge) provenance
    is returned.
    """
    src = await _make_source(test_session_factory)
    admin_id = uuid4()

    # Use distinct registration + date pairs so identity resolution creates two
    # separate events.  If registration and date are identical, the second
    # ingestion is routed to the existing event and merge raises
    # CannotMergeIntoSelfError.
    source_event_id = await _ingest(
        test_session_factory,
        src.id,
        [
            {"field_name": "registration", "field_value": "N-ABS-SRC", "source_tier": 1},
            {"field_name": "event_date", "field_value": "2024-10-04", "source_tier": 1},
        ],
    )
    target_event_id = await _ingest(
        test_session_factory,
        src.id,
        [
            {"field_name": "registration", "field_value": "N-ABS-TGT", "source_tier": 2},
            {"field_name": "event_date", "field_value": "2024-10-05", "source_tier": 2},
        ],
    )

    async with test_session_factory() as session:
        uow = SqlAlchemyUnitOfWork(session)
        await MergeDuplicateEvents(uow).execute(
            source_event_id=source_event_id,
            target_event_id=target_event_id,
            resolved_by=admin_id,
        )

    # canonicalize=True (default): asking for the absorbed event returns the survivor.
    async with test_session_factory() as session:
        uow = SqlAlchemyUnitOfWork(session)
        canon = await QueryProvenance(uow).execute(source_event_id, canonicalize=True)

    assert canon["event_id"] == target_event_id, (
        f"Canonicalized response must carry surviving event id {target_event_id}; "
        f"got {canon['event_id']}"
    )
    assert canon["absorbed_event_id"] == source_event_id, (
        "absorbed_event_id must be set when canonicalization redirected the query"
    )
    assert canon["canonicalized"] is True

    # canonicalize=False: asking for the absorbed event returns its own (sparse) provenance.
    async with test_session_factory() as session:
        uow = SqlAlchemyUnitOfWork(session)
        raw = await QueryProvenance(uow).execute(source_event_id, canonicalize=False)

    assert raw["event_id"] == source_event_id, (
        "Non-canonicalized response must use the requested event id"
    )
    assert raw["canonicalized"] is False
    assert raw["absorbed_event_id"] is None


# ── merge direction via SQL UoW ──────────────────────────────────────────────


async def test_merge_direction_source_absorbed_target_survives(pg_uow, test_session_factory):
    """Confirm that ``MergeDuplicateEvents`` puts claims on the *target*,
    sets ``merged_into_event_id`` on the *source*, and leaves the target
    as the canonical surviving event.

    This test is SQL-backed (no fake UoW) and verifies the database state
    directly after the commit.
    """
    src = await _make_source(test_session_factory)
    admin_id = uuid4()

    source_event_id = await _ingest(
        test_session_factory,
        src.id,
        [
            {"field_name": "registration", "field_value": "N-SRC-DIR", "source_tier": 1},
        ],
    )
    target_event_id = await _ingest(
        test_session_factory,
        src.id,
        [
            {"field_name": "event_date", "field_value": "2024-11-01", "source_tier": 1},
        ],
    )

    async with test_session_factory() as session:
        uow = SqlAlchemyUnitOfWork(session)
        result = await MergeDuplicateEvents(uow).execute(
            source_event_id=source_event_id,
            target_event_id=target_event_id,
            resolved_by=admin_id,
        )

    assert result.source_event_id == source_event_id
    assert result.target_event_id == target_event_id
    assert result.claims_transferred >= 1

    async with test_session_factory() as session:
        uow = SqlAlchemyUnitOfWork(session)
        source = await uow.events.get(source_event_id)
        target = await uow.events.get(target_event_id)
        target_active = await uow.claims.find_active_by_event(target_event_id)
        source_active = await uow.claims.find_active_by_event(source_event_id)

    # Source must be absorbed, target must be live.
    assert source is not None and source.is_merged
    assert source.merged_into_event_id == target_event_id
    assert target is not None and not target.is_merged

    # Source's original claims must be SUPERSEDED, not active.
    assert source_active == [], "Source event should have no active claims after merge"

    # Target must carry the transferred claim.
    target_fields = {c.field_name for c in target_active}
    assert "registration" in target_fields, (
        "registration claim transferred from source must be active on target"
    )
