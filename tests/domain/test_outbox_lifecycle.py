"""Outbox lifecycle tests: state machine, backoff, dead-letter, stale locks.

Tests run against the in-memory UoW so they don't require PostgreSQL. The
fake repository mirrors the real state-machine logic faithfully enough for
unit-level assertions.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from atlas.application.dto import IngestionClaimDTO
from atlas.application.use_cases.ingest_source_data import IngestSourceData
from atlas.domain.entities import Source
from atlas.domain.enums import OutboxStatus, SourceKind
from atlas.infrastructure.event_bus.outbox_worker import _next_attempt_at
from tests.domain._fake_uow import InMemoryUnitOfWork, make_settings


@pytest.fixture
async def uow_with_ingested_event():
    uow = InMemoryUnitOfWork()
    settings = make_settings()
    src = Source(id=uuid4(), name="S", kind=SourceKind.EXTERNAL, reliability_tier=1)
    await uow.sources.add(src)
    event_id = await IngestSourceData(uow, settings=settings).execute(
        source_id=src.id,
        raw_payload={"r": 1},
        ingestion_run_id=uuid4(),
        claims_data=[IngestionClaimDTO(field_name="event_date", field_value="2024-01-01")],
    )
    return uow, event_id


# ---------------------------------------------------------------------------
# PENDING -> PROCESSING -> PROCESSED
# ---------------------------------------------------------------------------


async def test_outbox_happy_path_pending_to_processed(uow_with_ingested_event):
    uow, _ = uow_with_ingested_event
    assert len(uow.store.outbox) == 1
    assert uow.store.outbox[0].status == OutboxStatus.PENDING

    events = await uow.outbox.fetch_and_lock_pending(10, "worker-1")
    assert len(events) == 1
    assert events[0].status == OutboxStatus.PROCESSING
    assert events[0].attempt_count == 1

    await uow.outbox.update_status(events[0].id, OutboxStatus.PROCESSED, events[0].attempt_count)
    assert uow.store.outbox[0].status == OutboxStatus.PROCESSED
    assert uow.store.outbox[0].processed_at is not None


# ---------------------------------------------------------------------------
# PENDING -> PROCESSING -> FAILED -> (retry) PENDING -> PROCESSING -> PROCESSED
# ---------------------------------------------------------------------------


async def test_outbox_failed_event_retried_when_next_attempt_at_is_due(uow_with_ingested_event):
    uow, _ = uow_with_ingested_event
    events = await uow.outbox.fetch_and_lock_pending(10, "worker-1")
    event = events[0]

    # Simulate a failure with a backoff that's already in the past.
    past_time = datetime.now(UTC) - timedelta(seconds=1)
    await uow.outbox.update_status(
        event.id,
        OutboxStatus.FAILED,
        event.attempt_count,
        last_error="transient DB error",
        next_attempt_at=past_time,
    )
    assert uow.store.outbox[0].status == OutboxStatus.FAILED

    # The FAILED event is due for retry - fetch_and_lock_pending should pick it up.
    retried = await uow.outbox.fetch_and_lock_pending(10, "worker-1", max_attempts=5)
    assert len(retried) == 1
    assert retried[0].status == OutboxStatus.PROCESSING
    assert retried[0].attempt_count == 2

    await uow.outbox.update_status(retried[0].id, OutboxStatus.PROCESSED, retried[0].attempt_count)
    assert uow.store.outbox[0].status == OutboxStatus.PROCESSED


async def test_outbox_failed_event_not_picked_up_before_next_attempt_at(uow_with_ingested_event):
    uow, _ = uow_with_ingested_event
    events = await uow.outbox.fetch_and_lock_pending(10, "worker-1")
    event = events[0]

    future_time = datetime.now(UTC) + timedelta(hours=1)
    await uow.outbox.update_status(
        event.id,
        OutboxStatus.FAILED,
        event.attempt_count,
        last_error="error",
        next_attempt_at=future_time,
    )

    not_retried = await uow.outbox.fetch_and_lock_pending(10, "worker-1", max_attempts=5)
    assert not_retried == [], "Event not due for retry should not be fetched"


# ---------------------------------------------------------------------------
# PENDING -> PROCESSING -> DEAD_LETTER (after max_attempts)
# ---------------------------------------------------------------------------


async def test_outbox_dead_lettered_after_max_attempts(uow_with_ingested_event):
    uow, _ = uow_with_ingested_event
    event = uow.store.outbox[0]

    # Simulate exhausted attempts: attempt_count >= max_attempts.
    max_attempts = 5
    for i in range(1, max_attempts):
        event.attempt_count = i
        event.status = OutboxStatus.PENDING
        event.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)

    # On the final attempt, mark dead-letter.
    event.attempt_count = max_attempts
    await uow.outbox.update_status(
        event.id,
        OutboxStatus.DEAD_LETTER,
        max_attempts,
        last_error="persistent failure",
    )

    # Dead-lettered events must NOT be picked up by the worker.
    picked_up = await uow.outbox.fetch_and_lock_pending(10, "worker-1", max_attempts=max_attempts)
    assert picked_up == [], "Dead-lettered event should not be retried"
    assert uow.store.outbox[0].status == OutboxStatus.DEAD_LETTER


async def test_outbox_failed_event_not_retried_if_attempt_count_equals_max(uow_with_ingested_event):
    uow, _ = uow_with_ingested_event
    event = uow.store.outbox[0]

    max_attempts = 3
    await uow.outbox.update_status(
        event.id,
        OutboxStatus.FAILED,
        attempt_count=max_attempts,  # == max, not retried
        last_error="error",
        next_attempt_at=datetime.now(UTC) - timedelta(seconds=1),
    )

    fetched = await uow.outbox.fetch_and_lock_pending(10, "worker-1", max_attempts=max_attempts)
    assert fetched == []


# ---------------------------------------------------------------------------
# Stale PROCESSING -> PENDING recovery
# ---------------------------------------------------------------------------


async def test_stale_processing_events_are_requeued(uow_with_ingested_event):
    uow, _ = uow_with_ingested_event
    events = await uow.outbox.fetch_and_lock_pending(10, "worker-1")
    assert events[0].status == OutboxStatus.PROCESSING

    # Backdate the lock so it's well past the stale threshold.
    uow.store.outbox[0].locked_at = datetime.now(UTC) - timedelta(minutes=15)

    recovered = await uow.outbox.requeue_stale_locked(stale_after_minutes=10, max_attempts=5)
    assert recovered == 1
    assert uow.store.outbox[0].status == OutboxStatus.PENDING
    assert uow.store.outbox[0].locked_at is None
    assert uow.store.outbox[0].next_attempt_at is None


async def test_fresh_processing_events_are_not_requeued(uow_with_ingested_event):
    uow, _ = uow_with_ingested_event
    await uow.outbox.fetch_and_lock_pending(10, "worker-1")
    # Lock is recent - should NOT be requeued.
    recovered = await uow.outbox.requeue_stale_locked(stale_after_minutes=10, max_attempts=5)
    assert recovered == 0
    assert uow.store.outbox[0].status == OutboxStatus.PROCESSING


async def test_stale_processing_dead_letters_return_deadlettered_events(uow_with_ingested_event):
    uow, _ = uow_with_ingested_event
    await uow.outbox.fetch_and_lock_pending(10, "worker-1")
    uow.store.outbox[0].locked_at = datetime.now(UTC) - timedelta(minutes=15)
    uow.store.outbox[0].attempt_count = 5
    uow.store.outbox[0].event_type = "ECHO_CROSSREF_REQUESTED"

    recovered, deadlettered = await uow.outbox.requeue_stale_locked_with_dead_letters(
        stale_after_minutes=10,
        max_attempts=5,
    )

    assert recovered == 1
    assert len(deadlettered) == 1
    assert deadlettered[0].id == uow.store.outbox[0].id
    assert uow.store.outbox[0].status == OutboxStatus.DEAD_LETTER


# ---------------------------------------------------------------------------
# Backoff helper
# ---------------------------------------------------------------------------


def test_next_attempt_at_uses_exponential_backoff():
    before = datetime.now(UTC)
    t0 = _next_attempt_at(0)
    t1 = _next_attempt_at(1)
    t2 = _next_attempt_at(2)
    t3 = _next_attempt_at(3)

    # Backoff: 1s, 2s, 4s, 8s (2^N)
    assert (t0 - before).total_seconds() >= 1
    assert (t1 - before).total_seconds() >= 2
    assert (t2 - before).total_seconds() >= 4
    assert (t3 - before).total_seconds() >= 8


def test_next_attempt_at_is_capped():
    from atlas.infrastructure.event_bus.outbox_worker import _MAX_BACKOFF_SECONDS

    # At attempt 30+, 2^30 >> cap - should be capped.
    t = _next_attempt_at(30)
    delay = (t - datetime.now(UTC)).total_seconds()
    assert delay <= _MAX_BACKOFF_SECONDS + 2  # +2 for test execution time


# ---------------------------------------------------------------------------
# Outbox event stored with correct initial state
# ---------------------------------------------------------------------------


async def test_outbox_event_initial_state(uow_with_ingested_event):
    uow, event_id = uow_with_ingested_event
    outbox = uow.store.outbox[0]
    assert outbox.status == OutboxStatus.PENDING
    assert outbox.attempt_count == 0
    assert outbox.locked_at is None
    assert outbox.locked_by is None
    assert outbox.next_attempt_at is None
    assert outbox.last_error is None
    assert outbox.processed_at is None
    assert outbox.event_type == "CLAIMS_UPDATED"
    assert outbox.aggregate_id == event_id
