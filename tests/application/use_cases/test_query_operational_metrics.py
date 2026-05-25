"""Tests for QueryOperationalMetrics use case.

Covers:
- Full snapshot (include_expensive_totals=True, default)
- Cheap Prometheus scrape path (include_expensive_totals=False)
- as_dict() output shape
- Outbox age and worker heartbeat staleness signals
- Empty-state sentinels (None when no data)
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from atlas.application.use_cases.query_operational_metrics import (
    QueryOperationalMetrics,
)
from atlas.domain.entities import OutboxEvent
from atlas.domain.enums import OutboxStatus
from tests.domain._fake_uow import InMemoryUnitOfWork

# ── helpers ──────────────────────────────────────────────────────────────────


def _make_outbox_event(status: OutboxStatus = OutboxStatus.PENDING) -> OutboxEvent:
    return OutboxEvent(
        event_type="test.event",
        aggregate_id=uuid4(),
        payload={},
        status=status,
    )


async def _add_outbox(uow: InMemoryUnitOfWork, *statuses: OutboxStatus) -> None:
    for s in statuses:
        await uow.outbox.add(_make_outbox_event(s))


# ── empty-state ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_empty_uow_returns_zero_counts() -> None:
    uow = InMemoryUnitOfWork()
    result = await QueryOperationalMetrics(uow).execute()

    assert result.outbox_pending == 0
    assert result.outbox_processing == 0
    assert result.outbox_failed == 0
    assert result.outbox_dead_letter == 0
    assert result.outbox_processed == 0
    assert result.conflicts_open == 0
    assert result.conflicts_resolved == 0
    assert result.total_claims == 0
    assert result.total_projected_events == 0


@pytest.mark.asyncio
async def test_empty_uow_age_sentinels_are_none() -> None:
    uow = InMemoryUnitOfWork()
    result = await QueryOperationalMetrics(uow).execute()

    assert result.outbox_oldest_unprocessed_age_seconds is None
    assert result.worker_heartbeat_age_seconds is None
    assert result.worker_successful_batch_age_seconds is None


# ── outbox counts ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_outbox_counts_reflect_stored_events() -> None:
    uow = InMemoryUnitOfWork()
    await _add_outbox(
        uow,
        OutboxStatus.PENDING,
        OutboxStatus.PENDING,
        OutboxStatus.PROCESSING,
        OutboxStatus.FAILED,
        OutboxStatus.DEAD_LETTER,
        OutboxStatus.PROCESSED,
        OutboxStatus.PROCESSED,
        OutboxStatus.PROCESSED,
    )

    result = await QueryOperationalMetrics(uow).execute()

    assert result.outbox_pending == 2
    assert result.outbox_processing == 1
    assert result.outbox_failed == 1
    assert result.outbox_dead_letter == 1
    assert result.outbox_processed == 3


# ── include_expensive_totals=False (Prometheus scrape path) ───────────────────


@pytest.mark.asyncio
async def test_cheap_path_skips_expensive_counts() -> None:
    """Prometheus-facing path must not compute historical totals."""
    uow = InMemoryUnitOfWork()
    await _add_outbox(uow, OutboxStatus.PENDING, OutboxStatus.PROCESSED)

    result = await QueryOperationalMetrics(uow).execute(include_expensive_totals=False)

    # Hot operational signals are still present.
    assert result.outbox_pending == 1
    # Historical totals are zeroed — not fetched.
    assert result.outbox_processed == 0
    assert result.conflicts_resolved == 0
    assert result.total_claims == 0
    assert result.total_projected_events == 0


@pytest.mark.asyncio
async def test_cheap_path_still_returns_age_signals() -> None:
    """Worker heartbeat and age signals are cheap; must still appear when scraping."""
    uow = InMemoryUnitOfWork()
    await _add_outbox(uow, OutboxStatus.PENDING)
    await uow.outbox.record_worker_heartbeat("w1", successful_batch=True)

    result = await QueryOperationalMetrics(uow).execute(include_expensive_totals=False)

    assert result.outbox_oldest_unprocessed_age_seconds is not None
    assert result.outbox_oldest_unprocessed_age_seconds >= 0.0
    assert result.worker_heartbeat_age_seconds is not None
    assert result.worker_successful_batch_age_seconds is not None


# ── age signals ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_oldest_unprocessed_age_is_nonnegative() -> None:
    uow = InMemoryUnitOfWork()
    await _add_outbox(uow, OutboxStatus.PENDING)

    result = await QueryOperationalMetrics(uow).execute()

    assert result.outbox_oldest_unprocessed_age_seconds is not None
    assert result.outbox_oldest_unprocessed_age_seconds >= 0.0


@pytest.mark.asyncio
async def test_processed_only_events_yield_no_age() -> None:
    """Only PENDING/PROCESSING/FAILED events count as unprocessed."""
    uow = InMemoryUnitOfWork()
    await _add_outbox(uow, OutboxStatus.PROCESSED, OutboxStatus.DEAD_LETTER)

    # DEAD_LETTER is not in the unprocessed set per fake implementation.
    result = await QueryOperationalMetrics(uow).execute()
    # dead_letter is not in {PENDING, PROCESSING, FAILED} so no age.
    assert result.outbox_oldest_unprocessed_age_seconds is None


@pytest.mark.asyncio
async def test_worker_heartbeat_age_reflects_most_recent_loop() -> None:
    uow = InMemoryUnitOfWork()
    await uow.outbox.record_worker_heartbeat("w1")
    await uow.outbox.record_worker_heartbeat("w2")  # two workers; newest wins

    result = await QueryOperationalMetrics(uow).execute()

    assert result.worker_heartbeat_age_seconds is not None
    # Should be very recent (sub-second in test execution).
    assert result.worker_heartbeat_age_seconds < 5.0


@pytest.mark.asyncio
async def test_worker_heartbeat_age_none_when_no_workers() -> None:
    uow = InMemoryUnitOfWork()
    result = await QueryOperationalMetrics(uow).execute()
    assert result.worker_heartbeat_age_seconds is None


@pytest.mark.asyncio
async def test_worker_successful_batch_age_none_without_successful_batch() -> None:
    uow = InMemoryUnitOfWork()
    # Heartbeat without a successful batch.
    await uow.outbox.record_worker_heartbeat("w1", successful_batch=False)

    result = await QueryOperationalMetrics(uow).execute()

    assert result.worker_heartbeat_age_seconds is not None
    assert result.worker_successful_batch_age_seconds is None


@pytest.mark.asyncio
async def test_worker_successful_batch_age_set_after_successful_batch() -> None:
    uow = InMemoryUnitOfWork()
    await uow.outbox.record_worker_heartbeat("w1", successful_batch=True)

    result = await QueryOperationalMetrics(uow).execute()

    assert result.worker_successful_batch_age_seconds is not None
    assert result.worker_successful_batch_age_seconds >= 0.0


# ── as_dict() shape ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_as_dict_has_expected_top_level_keys() -> None:
    uow = InMemoryUnitOfWork()
    result = await QueryOperationalMetrics(uow).execute()
    d = result.as_dict()

    assert set(d.keys()) == {"outbox", "conflicts", "claims", "events"}


@pytest.mark.asyncio
async def test_as_dict_outbox_section_has_all_keys() -> None:
    uow = InMemoryUnitOfWork()
    result = await QueryOperationalMetrics(uow).execute()
    outbox = result.as_dict()["outbox"]

    assert set(outbox.keys()) == {
        "pending",
        "processing",
        "failed",
        "dead_letter",
        "processed",
        "oldest_unprocessed_age_seconds",
        "worker_heartbeat_age_seconds",
        "worker_successful_batch_age_seconds",
    }


@pytest.mark.asyncio
async def test_as_dict_values_match_dataclass_fields() -> None:
    uow = InMemoryUnitOfWork()
    await _add_outbox(uow, OutboxStatus.PENDING, OutboxStatus.FAILED)
    await uow.outbox.record_worker_heartbeat("w1", successful_batch=True)

    result = await QueryOperationalMetrics(uow).execute()
    d = result.as_dict()

    assert d["outbox"]["pending"] == result.outbox_pending
    assert d["outbox"]["failed"] == result.outbox_failed
    assert d["outbox"]["worker_heartbeat_age_seconds"] == result.worker_heartbeat_age_seconds
    assert d["conflicts"]["open"] == result.conflicts_open
    assert d["claims"]["total"] == result.total_claims
    assert d["events"]["total_projected"] == result.total_projected_events


# ── provenance schema ─────────────────────────────────────────────────────────


def test_provenance_pagination_model() -> None:
    from atlas.presentation.api.schemas.provenance import ProvenancePagination

    cursor = uuid4()
    p = ProvenancePagination(
        limit=50,
        next_cursor=cursor,
        next_cursors={"claims": cursor, "conflicts": None},
        has_more=True,
    )

    assert p.limit == 50
    assert p.next_cursor == cursor
    assert p.has_more is True
    assert p.next_cursors["conflicts"] is None


def test_provenance_pagination_defaults() -> None:
    from atlas.presentation.api.schemas.provenance import ProvenancePagination

    p = ProvenancePagination(limit=25, next_cursors={}, has_more=False)

    assert p.next_cursor is None
    assert p.has_more is False


def test_provenance_response_model() -> None:
    from atlas.presentation.api.schemas.provenance import (
        ProvenancePagination,
        ProvenanceResponse,
    )

    event_id = uuid4()
    pagination = ProvenancePagination(limit=10, next_cursors={}, has_more=False)

    resp = ProvenanceResponse(
        event_id=event_id,
        projection={"tail_number": "N123AB"},
        claims=[{"id": str(uuid4())}],
        claim_histories=[],
        conflicts=[],
        conflict_activity_logs=[],
        projection_history=[],
        pagination=pagination,
        archive_available=False,
    )

    assert resp.event_id == event_id
    assert resp.absorbed_event_id is None
    assert resp.canonicalized is False
    assert resp.archive_available is False
    assert resp.pagination.limit == 10


def test_provenance_response_with_absorbed_event() -> None:
    from atlas.presentation.api.schemas.provenance import (
        ProvenancePagination,
        ProvenanceResponse,
    )

    absorbed = uuid4()
    pagination = ProvenancePagination(limit=5, next_cursors={}, has_more=False)

    resp = ProvenanceResponse(
        event_id=uuid4(),
        absorbed_event_id=absorbed,
        canonicalized=True,
        projection=None,
        claims=[],
        claim_histories=[],
        conflicts=[],
        conflict_activity_logs=[],
        projection_history=[],
        pagination=pagination,
        archive_available=True,
    )

    assert resp.absorbed_event_id == absorbed
    assert resp.canonicalized is True
    assert resp.projection is None
    assert resp.archive_available is True
