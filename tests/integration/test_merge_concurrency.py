"""Integration tests: merge concurrency correctness.

These tests prove the atomicity guarantee introduced in
``MergeDuplicateEvents``: only one of two concurrent merges of the same source
event can succeed, and the winner's claim transfers are not duplicated.

They require a live PostgreSQL instance and are skipped by default.  Run with:

    ATLAS_ALLOW_DB_TRUNCATE=1 pytest -m integration --run-integration \\
        tests/integration/test_merge_concurrency.py

Architecture of the race test
------------------------------
Two asyncio tasks each hold their own DB session so they issue independent
transactions.  We use an asyncio.Event as a "go" signal so both tasks start
the merge use-case call at the same time (within the same event-loop tick)
before either has committed.  Because asyncpg is fully async and issues real
network I/O, both transactions genuinely compete at the Postgres server level.

The critical invariant: after both tasks complete (one succeeds, one raises
``EventAlreadyMergedError``), we inspect the target event's claims and assert
there are no duplicate field entries - proving that only one claim-transfer
path executed.
"""

from __future__ import annotations

import asyncio
from uuid import UUID, uuid4

import pytest

from atlas.application.dto import IngestionClaimDTO
from atlas.application.use_cases.ingest_source_data import IngestSourceData
from atlas.application.use_cases.merge_duplicate_events import MergeDuplicateEvents
from atlas.domain.entities import Source
from atlas.domain.enums import SourceKind
from atlas.domain.exceptions import EventAlreadyMergedError
from atlas.infrastructure.db.unit_of_work import SqlAlchemyUnitOfWork

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


# ── helpers ─────────────────────────────────────────────────────────────────


async def _make_source(session_factory, name_prefix: str) -> Source:
    src = Source(
        id=uuid4(),
        name=f"{name_prefix}-{uuid4().hex[:8]}",
        kind=SourceKind.EXTERNAL,
        reliability_tier=1,
    )
    async with session_factory() as session:
        uow = SqlAlchemyUnitOfWork(session)
        await uow.sources.add(src)
        await uow.commit()
    return src


async def _ingest(session_factory, source_id, claims_data) -> UUID:
    """Run a complete ingestion in its own session and return the event_id."""
    async with session_factory() as session:
        uow = SqlAlchemyUnitOfWork(session)
        return await IngestSourceData(uow).execute(
            source_id=source_id,
            raw_payload={"r": uuid4().hex},
            ingestion_run_id=uuid4(),
            claims_data=[IngestionClaimDTO(**c) for c in claims_data],
        )


# ── test: concurrent merges of the same source ──────────────────────────────


async def test_concurrent_merges_same_source_only_one_succeeds(pg_uow, test_session_factory):
    """Two simultaneous merge requests for the same (source, target) pair must
    result in exactly one success and one EventAlreadyMergedError.

    After the race:
    - The source event is merged into the target exactly once.
    - The target event has no duplicate claim rows for any field.
    """
    src = await _make_source(test_session_factory, "merge-race-src")
    admin_id = uuid4()

    # Source gets a unique field (aircraft_serial_number) that target does not
    # have.  After exactly ONE successful merge: the field appears once on the
    # target.  If both concurrent merges transferred claims, it appears twice.
    # Using distinct registrations + dates prevents identity resolution from
    # routing both ingestions to the same event (CannotMergeIntoSelfError).
    source_event_id = await _ingest(
        test_session_factory,
        src.id,
        [
            {"field_name": "registration", "field_value": "N-SRC-RACE", "source_tier": 1},
            {"field_name": "event_date", "field_value": "2024-03-15", "source_tier": 1},
            {
                "field_name": "aircraft_serial_number",
                "field_value": "SN-UNIQUE-99",
                "source_tier": 1,
            },
        ],
    )

    # Ingest a separate target event with a different registration + date.
    target_event_id = await _ingest(
        test_session_factory,
        src.id,
        [
            {"field_name": "registration", "field_value": "N-TGT-RACE", "source_tier": 2},
            {"field_name": "event_date", "field_value": "2024-03-16", "source_tier": 2},
        ],
    )

    # ── Race ────────────────────────────────────────────────────────────────
    # Both coroutines will call MergeDuplicateEvents in their own session.
    # The asyncio.Event makes them start at approximately the same wall-clock
    # moment (same event-loop iteration) before either session has committed.
    go = asyncio.Event()
    results: list[Exception | None] = []

    async def _merge_task(label: str) -> None:
        async with test_session_factory() as session:
            uow = SqlAlchemyUnitOfWork(session)
            await go.wait()  # synchronise start
            try:
                await MergeDuplicateEvents(uow).execute(
                    source_event_id=source_event_id,
                    target_event_id=target_event_id,
                    resolved_by=admin_id,
                    note=f"concurrent merge {label}",
                )
                results.append(None)  # success sentinel
            except EventAlreadyMergedError as exc:
                results.append(exc)

    task_a = asyncio.create_task(_merge_task("A"))
    task_b = asyncio.create_task(_merge_task("B"))

    go.set()  # release both tasks simultaneously
    await asyncio.gather(task_a, task_b, return_exceptions=True)

    # ── Assertions ──────────────────────────────────────────────────────────
    # Exactly one success and one failure.
    successes = [r for r in results if r is None]
    failures = [r for r in results if isinstance(r, EventAlreadyMergedError)]

    assert len(successes) == 1, (
        f"Expected exactly one successful merge, got {len(successes)}: {results}"
    )
    assert len(failures) == 1, (
        f"Expected exactly one EventAlreadyMergedError, got {len(failures)}: {results}"
    )

    # Target event must not have duplicate (field_name) entries - no claim
    # was transferred twice.
    async with test_session_factory() as session:
        uow = SqlAlchemyUnitOfWork(session)
        target_claims = await uow.claims.find_active_by_event(target_event_id)

    # Check specifically for double-transfer of the source-unique field.
    # aircraft_serial_number only exists on the source event, so it appears
    # on the target only through claim transfer.  If both concurrent merges
    # transferred claims, it appears twice — that is the bug we are testing for.
    serial_claims = [c for c in target_claims if c.field_name == "aircraft_serial_number"]
    assert len(serial_claims) == 1, (
        f"Expected exactly 1 aircraft_serial_number claim on target after merge, "
        f"got {len(serial_claims)}.  try_atomic_merge did not prevent double-transfer."
    )

    # Source event must now be flagged as merged.
    async with test_session_factory() as session:
        uow = SqlAlchemyUnitOfWork(session)
        source = await uow.events.get(source_event_id)

    assert source is not None
    assert source.is_merged, "Source event should be marked merged after the race"
    assert source.merged_into_event_id == target_event_id


async def test_concurrent_merges_different_targets_only_one_succeeds(pg_uow, test_session_factory):
    """Race where two concurrent requests try to merge the same source into
    *different* targets.  The second attempt must fail even though the targets
    are distinct - the source can only be absorbed once.
    """
    src = await _make_source(test_session_factory, "split-race-src")
    admin_id = uuid4()

    source_event_id = await _ingest(
        test_session_factory,
        src.id,
        [{"field_name": "registration", "field_value": "N99999", "source_tier": 1}],
    )
    target_a_id = await _ingest(
        test_session_factory,
        src.id,
        [{"field_name": "registration", "field_value": "N99999", "source_tier": 2}],
    )
    target_b_id = await _ingest(
        test_session_factory,
        src.id,
        [{"field_name": "registration", "field_value": "N99999", "source_tier": 2}],
    )

    go = asyncio.Event()
    outcomes: list[tuple[str, Exception | None]] = []

    async def _merge(target_id, label: str) -> None:
        async with test_session_factory() as session:
            uow = SqlAlchemyUnitOfWork(session)
            await go.wait()
            try:
                await MergeDuplicateEvents(uow).execute(
                    source_event_id=source_event_id,
                    target_event_id=target_id,
                    resolved_by=admin_id,
                )
                outcomes.append((label, None))
            except EventAlreadyMergedError as exc:
                outcomes.append((label, exc))

    task_a = asyncio.create_task(_merge(target_a_id, "->A"))
    task_b = asyncio.create_task(_merge(target_b_id, "->B"))
    go.set()
    await asyncio.gather(task_a, task_b, return_exceptions=True)

    successes = [lbl for lbl, err in outcomes if err is None]
    failures = [lbl for lbl, err in outcomes if err is not None]

    assert len(successes) == 1, f"Expected 1 success, got {len(successes)}: {outcomes}"
    assert len(failures) == 1, f"Expected 1 failure, got {len(failures)}: {outcomes}"

    # Verify the source event was absorbed into exactly one target.
    async with test_session_factory() as session:
        uow = SqlAlchemyUnitOfWork(session)
        source = await uow.events.get(source_event_id)

    assert source is not None and source.is_merged
    # The surviving target is whichever label succeeded.
    winning_label = successes[0]
    expected_target = target_a_id if winning_label == "->A" else target_b_id
    assert source.merged_into_event_id == expected_target, (
        f"Source merged into unexpected target: {source.merged_into_event_id}"
    )
