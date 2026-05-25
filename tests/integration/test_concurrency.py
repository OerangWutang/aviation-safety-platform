"""Integration tests for concurrent-writer correctness.

These exercise the production code paths that unit tests with the in-memory
fake UoW cannot prove safe:

  - the partial unique index on OPEN conflicts (migration 008) and the
    ``try_add_open`` insert path that targets it,
  - the lease-fenced ``update_status`` on outbox events,
  - dead-lettering of stale PROCESSING rows whose attempts are exhausted,
  - the per-event reprojection advisory lock.

Each test runs two coroutines against the same Postgres connection pool and
verifies that the loser of the race produces a benign no-op rather than a
duplicate row, lost update, or wrong version.

The fixtures (``pg_uow``, ``test_session_factory``, ``test_engine``) live in
``conftest.py`` and require:

  ATLAS_ALLOW_DB_TRUNCATE=1 pytest -m integration --run-integration

The entire file is gated behind ``@pytest.mark.integration`` and is skipped
by default.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import text

from atlas.application.dto import IngestionClaimDTO
from atlas.application.use_cases.ingest_source_data import IngestSourceData
from atlas.application.use_cases.reproject_event import ReProjectEvent
from atlas.domain.entities import Source
from atlas.domain.enums import OutboxStatus, SourceKind
from atlas.infrastructure.db.unit_of_work import SqlAlchemyUnitOfWork

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


async def _make_source(session_factory, name_prefix: str, tier: int = 1) -> Source:
    """Insert a fresh source in its own transaction and return the domain entity."""
    src = Source(
        id=uuid4(),
        name=f"{name_prefix}-{uuid4().hex[:8]}",
        kind=SourceKind.EXTERNAL,
        reliability_tier=tier,
    )
    async with session_factory() as session:
        uow = SqlAlchemyUnitOfWork(session)
        await uow.sources.add(src)
        await uow.commit()
    return src


async def _ingest_in_own_session(session_factory, source_id, claims_data, event_id=None):
    """Run a complete ingestion in a dedicated session/transaction.

    Concurrency tests need each "worker" to own its own session so they are
    not implicitly serialized by sharing the same connection.
    """
    async with session_factory() as session:
        uow = SqlAlchemyUnitOfWork(session)
        return await IngestSourceData(uow).execute(
            source_id=source_id,
            raw_payload={"r": uuid4().hex},  # unique payload -> unique snapshot key
            ingestion_run_id=uuid4(),
            claims_data=[IngestionClaimDTO(**c) for c in claims_data],
            event_id=event_id,
        )


# --------------------------------------------------------------------------- #
# Conflict insertion race
# --------------------------------------------------------------------------- #


async def test_concurrent_ingestion_creates_at_most_one_open_conflict(pg_uow, test_session_factory):
    """Two parallel ingestions that produce contradicting claims must result in
    exactly one OPEN conflict for the (event_id, field_name) pair.

    The partial unique index ``uq_open_conflict_event_field`` (migration 008)
    is the safety net; ``try_add_open`` uses ``ON CONFLICT DO NOTHING`` against
    its index_elements so the loser of the race silently merges into the
    existing row instead of raising.

    Without the fix in repositories.py (using ``constraint=`` against an index
    rather than ``index_elements`` + ``index_where``), this race produced
    either a duplicate row or an IntegrityError.
    """
    source_a = await _make_source(test_session_factory, "A", tier=1)
    source_b = await _make_source(test_session_factory, "B", tier=2)

    # First ingestion seeds the event with one claim so both racing ingests
    # target the same event_id.
    event_id = await _ingest_in_own_session(
        test_session_factory,
        source_a.id,
        [{"field_name": "operator", "field_value": "AirlineX"}],
    )

    async def ingest_with_value(source_id, value):
        return await _ingest_in_own_session(
            test_session_factory,
            source_id,
            [{"field_name": "operator", "field_value": value}],
            event_id=event_id,
        )

    # Race two contradicting ingestions.
    await asyncio.gather(
        ingest_with_value(source_a.id, "AirlineY"),
        ingest_with_value(source_b.id, "AirlineZ"),
    )

    # Verify exactly one OPEN conflict for this (event, field).
    async with test_session_factory() as session:
        result = await session.execute(
            text(
                "SELECT count(*) FROM claim_conflicts "
                "WHERE event_id = :eid AND field_name = 'operator' AND status = 'OPEN'"
            ),
            {"eid": event_id},
        )
        open_count = result.scalar_one()
    assert open_count == 1, (
        f"Expected exactly one OPEN conflict but found {open_count}. "
        f"This usually means try_add_open's ON CONFLICT target failed to match "
        f"the partial unique index and the second writer inserted a duplicate."
    )


# --------------------------------------------------------------------------- #
# Outbox lease fencing
# --------------------------------------------------------------------------- #


async def test_stale_recovery_then_late_update_is_no_op(pg_uow, test_session_factory):
    """Worker A locks event, hangs, recovery requeues, worker B locks and
    processes - worker A's late ``update_status`` must NOT overwrite B's result.

    Reproduces the lost-update scenario the review called out. The fenced
    ``update_status`` requires ``locked_by = expected_worker_id`` and
    ``attempt_count = expected_attempt_count``; once recovery clears those
    fields and B re-locks (incrementing the counter), A's WHERE clause matches
    zero rows and ``applied`` returns False.
    """
    source = await _make_source(test_session_factory, "S")

    async with test_session_factory() as session:
        uow = SqlAlchemyUnitOfWork(session)
        event_id = await IngestSourceData(uow).execute(
            source_id=source.id,
            raw_payload={"r": 1},
            ingestion_run_id=uuid4(),
            claims_data=[IngestionClaimDTO(field_name="event_date", field_value="2024-01-01")],
        )

    # Worker A locks it.
    async with test_session_factory() as session_a:
        uow_a = SqlAlchemyUnitOfWork(session_a)
        a_events = await uow_a.outbox.fetch_and_lock_pending(10, "worker-A")
        await uow_a.commit()
    assert len(a_events) == 1
    a_event = a_events[0]
    assert a_event.attempt_count == 1

    # Backdate worker A's lock to make it stale.
    async with test_session_factory() as session:
        await session.execute(
            text("UPDATE outbox_events SET locked_at = :stale WHERE id = :id"),
            {"stale": datetime.now(UTC) - timedelta(minutes=20), "id": a_event.id},
        )
        await session.commit()

    # Stale recovery requeues it (budget remaining -> PENDING).
    async with test_session_factory() as session:
        uow = SqlAlchemyUnitOfWork(session)
        recovered = await uow.outbox.requeue_stale_locked(stale_after_minutes=10)
        await uow.commit()
    assert recovered == 1

    # Worker B locks and processes it.
    async with test_session_factory() as session_b:
        uow_b = SqlAlchemyUnitOfWork(session_b)
        b_events = await uow_b.outbox.fetch_and_lock_pending(10, "worker-B")
        assert len(b_events) == 1
        b_event = b_events[0]
        assert b_event.attempt_count == 2  # bumped on re-lock
        applied_b = await uow_b.outbox.update_status(
            b_event.id,
            OutboxStatus.PROCESSED,
            b_event.attempt_count,
            expected_worker_id="worker-B",
            expected_attempt_count=b_event.attempt_count,
        )
        await uow_b.commit()
    assert applied_b is True

    # Worker A finally wakes up and tries to mark it FAILED with stale
    # attempt_count. The fenced update must reject this.
    async with test_session_factory() as session_a2:
        uow_a2 = SqlAlchemyUnitOfWork(session_a2)
        applied_a = await uow_a2.outbox.update_status(
            a_event.id,
            OutboxStatus.FAILED,
            a_event.attempt_count,
            last_error="stale write attempt from preempted worker",
            expected_worker_id="worker-A",
            expected_attempt_count=a_event.attempt_count,
        )
        await uow_a2.commit()
    assert applied_a is False, (
        "Lease fencing failed: worker A overwrote worker B's result. "
        "Check that update_status's WHERE clause includes "
        "(status='PROCESSING', locked_by, attempt_count)."
    )

    # Final state: PROCESSED, attempt_count=2, no last_error from A.
    async with test_session_factory() as session:
        result = await session.execute(
            text("SELECT status, attempt_count, last_error FROM outbox_events WHERE id = :id"),
            {"id": event_id and a_event.id},
        )
        row = result.one()
    assert row.status == OutboxStatus.PROCESSED.value
    assert row.attempt_count == 2
    assert row.last_error is None


async def test_stale_processing_with_exhausted_attempts_dead_letters(pg_uow, test_session_factory):
    """A stale PROCESSING row whose ``attempt_count >= max_attempts`` must
    transition to DEAD_LETTER, not stay stuck.

    Reproduces the bug the review called out: the previous ``requeue_stale_locked``
    only handled rows under budget, leaving exhausted-but-stale rows in
    PROCESSING forever.
    """
    source = await _make_source(test_session_factory, "S")

    async with test_session_factory() as session:
        uow = SqlAlchemyUnitOfWork(session)
        event_id = await IngestSourceData(uow).execute(
            source_id=source.id,
            raw_payload={"r": 1},
            ingestion_run_id=uuid4(),
            claims_data=[IngestionClaimDTO(field_name="event_date", field_value="2024-01-01")],
        )

    # Force the row to look like "locked + exhausted attempts + stale".
    max_attempts = 5
    async with test_session_factory() as session:
        await session.execute(
            text(
                """
                UPDATE outbox_events
                SET status = 'PROCESSING',
                    locked_at = :stale,
                    locked_by = 'crashed-worker',
                    attempt_count = :max_attempts
                WHERE aggregate_id = :eid
                """
            ),
            {
                "stale": datetime.now(UTC) - timedelta(minutes=30),
                "max_attempts": max_attempts,
                "eid": event_id,
            },
        )
        await session.commit()

    async with test_session_factory() as session:
        uow = SqlAlchemyUnitOfWork(session)
        moved = await uow.outbox.requeue_stale_locked(
            stale_after_minutes=10, max_attempts=max_attempts
        )
        await uow.commit()

    assert moved == 1

    async with test_session_factory() as session:
        result = await session.execute(
            text("SELECT status FROM outbox_events WHERE aggregate_id = :eid"),
            {"eid": event_id},
        )
        status = result.scalar_one()
    assert status == OutboxStatus.DEAD_LETTER.value, (
        f"Expected DEAD_LETTER for exhausted stale row, got {status!r}. "
        f"Without the fix, exhausted stale rows stay stuck in PROCESSING."
    )


# --------------------------------------------------------------------------- #
# Per-event reprojection serialization
# --------------------------------------------------------------------------- #


async def test_concurrent_reprojections_do_not_collide_on_version(pg_uow, test_session_factory):
    """Two reprojections of the same event run concurrently -> both succeed,
    one waits for the other, no ``uq_projection_history_version`` violation.

    Without the advisory lock both workers compute the same
    ``current.projection_version + 1`` and the second commit fails. With the
    lock the second waits for the first to commit, then sees the bumped
    version and computes the next one.
    """
    source = await _make_source(test_session_factory, "S")

    async with test_session_factory() as session:
        uow = SqlAlchemyUnitOfWork(session)
        event_id = await IngestSourceData(uow).execute(
            source_id=source.id,
            raw_payload={"r": 1},
            ingestion_run_id=uuid4(),
            claims_data=[
                IngestionClaimDTO(field_name="event_date", field_value="2024-01-01"),
            ],
        )

    async def reproject_in_own_session():
        async with test_session_factory() as session:
            uow = SqlAlchemyUnitOfWork(session)
            await ReProjectEvent(uow).execute(event_id=event_id)

    # Run two reprojections in parallel. The advisory lock should serialize
    # them transparently. Either both succeed (different versions) or one
    # raises - we assert no exception.
    results = await asyncio.gather(
        reproject_in_own_session(),
        reproject_in_own_session(),
        return_exceptions=True,
    )
    failures = [r for r in results if isinstance(r, BaseException)]
    assert not failures, (
        f"Concurrent reprojections collided on projection_version: {failures}. "
        f"This means the advisory lock in lock_for_reprojection is missing or "
        f"keyed on something other than event_id."
    )

    # Both reprojections must have produced distinct history rows.
    async with test_session_factory() as session:
        result = await session.execute(
            text(
                "SELECT count(*), count(distinct projection_version) "
                "FROM accident_projection_history WHERE accident_event_id = :eid"
            ),
            {"eid": event_id},
        )
        total, distinct = result.one()
    assert total == distinct, (
        f"Got {total} history rows but only {distinct} distinct versions; "
        f"the version sequence collided."
    )
