"""Domain tests for ListArgusSignals — keyset pagination over Argus signals."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from atlas.application.use_cases.list_argus_signals import (
    DEFAULT_ARGUS_SIGNALS_PAGE_SIZE,
    MAX_ARGUS_SIGNALS_PAGE_SIZE,
    ListArgusSignals,
)
from atlas.domain.entities import ArgusSignal
from atlas.domain.enums import (
    ArgusSeverity,
    ArgusSignalStatus,
    ArgusSignalType,
)
from atlas.domain.exceptions import DomainValidationError
from tests.domain._fake_uow import InMemoryUnitOfWork


def _ts(minutes_ago: int) -> datetime:
    """Generate a UTC datetime that's ``minutes_ago`` minutes earlier than now.

    Using offsets rather than wall-clock fixed times keeps the test stable
    across runs and avoids leap-second weirdness in the rare future where
    Python adds support for it.
    """
    return datetime.now(UTC) - timedelta(minutes=minutes_ago)


def _make_signal(
    *,
    last_detected_at: datetime,
    signal_type: ArgusSignalType = ArgusSignalType.NEW_SOURCE_CHANGE,
    status: ArgusSignalStatus = ArgusSignalStatus.OPEN,
    severity: ArgusSeverity = ArgusSeverity.MEDIUM,
    signal_id: UUID | None = None,
) -> ArgusSignal:
    return ArgusSignal(
        id=signal_id or uuid4(),
        signal_type=signal_type,
        status=status,
        severity=severity,
        confidence=0.9,
        title="Signal",
        source_engine="hermes",
        dedupe_key=f"ARGUS::{uuid4()}",
        first_detected_at=last_detected_at,
        last_detected_at=last_detected_at,
    )


async def _seed(uow: InMemoryUnitOfWork, n: int) -> list[ArgusSignal]:
    """Seed ``n`` signals with strictly-decreasing ``last_detected_at`` so the
    page boundary is unambiguous in the simple-case tests.  Returns the
    seeded signals in newest-first order (matches the repo's ORDER BY).
    """
    signals = [_make_signal(last_detected_at=_ts(minutes_ago=i)) for i in range(n)]
    for s in signals:
        await uow.argus_signals.add(s)
    # Sorted newest-first to match what list_page returns.
    signals.sort(key=lambda s: (s.last_detected_at, s.id), reverse=True)
    return signals


# ── Basics ───────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_first_page_returns_limit_items_and_a_cursor():
    uow = InMemoryUnitOfWork()
    seeded = await _seed(uow, n=12)

    page = await ListArgusSignals(uow).execute_page(limit=5)

    assert len(page.items) == 5
    assert page.limit == 5
    assert page.next_cursor == page.items[-1].id
    # First page is the 5 newest, in order.
    assert [s.id for s in page.items] == [s.id for s in seeded[:5]]


@pytest.mark.asyncio
async def test_second_page_skips_first_page_exactly():
    uow = InMemoryUnitOfWork()
    seeded = await _seed(uow, n=12)

    page1 = await ListArgusSignals(uow).execute_page(limit=5)
    page2 = await ListArgusSignals(uow).execute_page(limit=5, cursor=page1.next_cursor)

    assert len(page2.items) == 5
    # No overlap between pages — the entire concern of keyset pagination.
    ids1 = {s.id for s in page1.items}
    ids2 = {s.id for s in page2.items}
    assert ids1.isdisjoint(ids2)
    assert [s.id for s in page2.items] == [s.id for s in seeded[5:10]]


@pytest.mark.asyncio
async def test_last_page_has_no_next_cursor():
    """``next_cursor`` is None iff there are no more rows after this page."""
    uow = InMemoryUnitOfWork()
    seeded = await _seed(uow, n=12)

    # Page through to the end.
    page1 = await ListArgusSignals(uow).execute_page(limit=5)
    page2 = await ListArgusSignals(uow).execute_page(limit=5, cursor=page1.next_cursor)
    page3 = await ListArgusSignals(uow).execute_page(limit=5, cursor=page2.next_cursor)

    # 12 rows / page size 5 → 3 pages of (5, 5, 2).
    assert len(page3.items) == 2
    assert page3.next_cursor is None
    # Cumulative result equals the full seeded set, no duplicates, no skips.
    all_returned = [s.id for s in page1.items + page2.items + page3.items]
    assert all_returned == [s.id for s in seeded]
    assert len(set(all_returned)) == 12


@pytest.mark.asyncio
async def test_empty_table_returns_empty_page_and_no_cursor():
    uow = InMemoryUnitOfWork()
    page = await ListArgusSignals(uow).execute_page(limit=10)
    assert page.items == []
    assert page.next_cursor is None


@pytest.mark.asyncio
async def test_exact_page_size_yields_no_next_cursor():
    """Boundary case: when the result count exactly equals ``limit``, there's
    no next page even though we fetched ``limit + 1`` rows."""
    uow = InMemoryUnitOfWork()
    await _seed(uow, n=5)
    page = await ListArgusSignals(uow).execute_page(limit=5)
    assert len(page.items) == 5
    assert page.next_cursor is None


# ── Filters compose with pagination ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_filters_apply_before_pagination():
    """``status`` / ``signal_type`` / ``severity`` filters are AND-combined
    with the keyset predicate; the next_cursor still refers to a row in the
    filtered set."""
    uow = InMemoryUnitOfWork()
    # Mix two signal types so the filter actually narrows.
    for i in range(6):
        await uow.argus_signals.add(
            _make_signal(
                last_detected_at=_ts(i),
                signal_type=ArgusSignalType.NEW_SOURCE_CHANGE,
            )
        )
        await uow.argus_signals.add(
            _make_signal(
                last_detected_at=_ts(i + 100),
                signal_type=ArgusSignalType.SOURCE_FETCH_FAILURE_SPIKE,
            )
        )

    page = await ListArgusSignals(uow).execute_page(
        limit=3, signal_type=ArgusSignalType.NEW_SOURCE_CHANGE
    )
    assert len(page.items) == 3
    assert all(s.signal_type == ArgusSignalType.NEW_SOURCE_CHANGE for s in page.items)


# ── Ties on (last_detected_at) ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_tied_timestamps_paginate_without_skips_or_duplicates():
    """All signals share ``last_detected_at`` — the regression case the round-1
    composite index was built for.  Offset pagination silently misbehaves
    here; keyset must not."""
    uow = InMemoryUnitOfWork()
    shared_ts = _ts(minutes_ago=0)
    for _ in range(10):
        await uow.argus_signals.add(_make_signal(last_detected_at=shared_ts))

    page1 = await ListArgusSignals(uow).execute_page(limit=3)
    page2 = await ListArgusSignals(uow).execute_page(limit=3, cursor=page1.next_cursor)
    page3 = await ListArgusSignals(uow).execute_page(limit=3, cursor=page2.next_cursor)
    page4 = await ListArgusSignals(uow).execute_page(limit=3, cursor=page3.next_cursor)

    all_ids = [s.id for s in page1.items + page2.items + page3.items + page4.items]
    # All 10 returned exactly once.
    assert len(all_ids) == 10
    assert len(set(all_ids)) == 10
    assert page4.next_cursor is None


# ── Cursor edge cases ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_stale_cursor_falls_back_to_first_page():
    """A cursor that no longer refers to a real row must not crash the API;
    it falls back to ``no cursor``.  Matches the documented contract on the
    helper (``invalid/stale cursors are treated as absent``)."""
    uow = InMemoryUnitOfWork()
    seeded = await _seed(uow, n=5)

    page = await ListArgusSignals(uow).execute_page(limit=10, cursor=uuid4())
    assert [s.id for s in page.items] == [s.id for s in seeded]
    assert page.next_cursor is None


# ── Limit validation ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_limit_below_one_raises_domain_validation_error():
    uow = InMemoryUnitOfWork()
    with pytest.raises(DomainValidationError, match="limit"):
        await ListArgusSignals(uow).execute_page(limit=0)


@pytest.mark.asyncio
async def test_limit_above_cap_is_silently_clamped():
    """The constant ``MAX_ARGUS_SIGNALS_PAGE_SIZE`` mirrors the cap on the
    router's ``Query(le=500)``; the use case clamps as defence-in-depth so
    callers that bypass the router still get bounded queries."""
    uow = InMemoryUnitOfWork()
    page = await ListArgusSignals(uow).execute_page(limit=MAX_ARGUS_SIGNALS_PAGE_SIZE + 10_000)
    # ``page.limit`` is the *effective* clamped limit.
    assert page.limit == MAX_ARGUS_SIGNALS_PAGE_SIZE


@pytest.mark.asyncio
async def test_default_page_size_constant_is_reasonable():
    """Smoke-check the default isn't accidentally 0 or absurdly large.

    A drift here would silently change API behaviour for callers that don't
    pass ``limit``.  Pinning the value makes that change loud.
    """
    assert DEFAULT_ARGUS_SIGNALS_PAGE_SIZE == 50
    assert MAX_ARGUS_SIGNALS_PAGE_SIZE == 500
