"""Domain tests for ReviewArgusSignal use case.

Covers the optimistic-concurrency contract added in migration 033:
- Happy paths require ``expected_version`` and bump it on success.
- Stale ``expected_version`` yields ``ArgusSignalModifiedError`` with the
  current state attached.
- A concurrent reviewer that wins the race between pre-check and update
  also yields ``ArgusSignalModifiedError``.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from atlas.application.use_cases.review_argus_signal import (
    ReviewArgusSignal,
    ReviewArgusSignalInput,
)
from atlas.domain.entities import ArgusSignal
from atlas.domain.enums import (
    ArgusReviewDecision,
    ArgusSeverity,
    ArgusSignalStatus,
    ArgusSignalType,
)
from atlas.domain.exceptions import ArgusSignalModifiedError, ArgusSignalNotFoundError
from tests.domain._fake_uow import InMemoryUnitOfWork


def _make_signal(**kwargs) -> ArgusSignal:
    defaults = dict(
        signal_type=ArgusSignalType.TIMELINE_SEQUENCE_CONFLICT,
        severity=ArgusSeverity.MEDIUM,
        confidence=0.9,
        title="Test signal",
        source_engine="chronos",
        dedupe_key=f"ARGUS::TEST::{uuid4()}",
    )
    defaults.update(kwargs)
    return ArgusSignal(**defaults)


# ── Happy paths ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reviewer_can_confirm_signal():
    uow = InMemoryUnitOfWork()
    signal = _make_signal()
    await uow.argus_signals.add(signal)
    updated = await ReviewArgusSignal(uow).execute(
        ReviewArgusSignalInput(
            signal_id=signal.id,
            decision=ArgusReviewDecision.CONFIRMED,
            expected_version=signal.version,
        )
    )
    assert updated.status == ArgusSignalStatus.CONFIRMED
    # Successful update bumps the version so the *next* reviewer sees fresh
    # state via the response or a re-GET.
    assert updated.version == signal.version + 1


@pytest.mark.asyncio
async def test_reviewer_can_dismiss_signal():
    uow = InMemoryUnitOfWork()
    signal = _make_signal()
    await uow.argus_signals.add(signal)
    updated = await ReviewArgusSignal(uow).execute(
        ReviewArgusSignalInput(
            signal_id=signal.id,
            decision=ArgusReviewDecision.DISMISSED,
            expected_version=signal.version,
        )
    )
    assert updated.status == ArgusSignalStatus.DISMISSED


@pytest.mark.asyncio
async def test_reviewer_can_mark_needs_more_review():
    uow = InMemoryUnitOfWork()
    signal = _make_signal()
    await uow.argus_signals.add(signal)
    updated = await ReviewArgusSignal(uow).execute(
        ReviewArgusSignalInput(
            signal_id=signal.id,
            decision=ArgusReviewDecision.NEEDS_MORE_REVIEW,
            expected_version=signal.version,
        )
    )
    assert updated.status == ArgusSignalStatus.NEEDS_MORE_REVIEW


@pytest.mark.asyncio
async def test_review_creates_argus_signal_review_row():
    uow = InMemoryUnitOfWork()
    signal = _make_signal()
    await uow.argus_signals.add(signal)
    reviewer_id = uuid4()
    await ReviewArgusSignal(uow).execute(
        ReviewArgusSignalInput(
            signal_id=signal.id,
            decision=ArgusReviewDecision.CONFIRMED,
            expected_version=signal.version,
            reviewer_id=reviewer_id,
            note="Looks correct",
        )
    )
    reviews = await uow.argus_signal_reviews.list_for_signal(signal.id)
    assert len(reviews) == 1
    assert reviews[0].decision == ArgusReviewDecision.CONFIRMED
    assert reviews[0].reviewer_id == reviewer_id
    assert reviews[0].note == "Looks correct"


# ── Error paths ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_missing_signal_raises_typed_not_found_error():
    """Previously a bare ``ValueError`` (which maps to 500 in app.py's global
    handler); now a typed ``ArgusSignalNotFoundError`` that maps to 404."""
    uow = InMemoryUnitOfWork()
    with pytest.raises(ArgusSignalNotFoundError, match="not found"):
        await ReviewArgusSignal(uow).execute(
            ReviewArgusSignalInput(
                signal_id=uuid4(),
                decision=ArgusReviewDecision.CONFIRMED,
                expected_version=1,
            )
        )


# ── Optimistic concurrency ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_stale_expected_version_raises_modified_error():
    """Reviewer loaded the signal when version was 1, but someone has bumped
    it to 2 since.  The pre-check fires and the error carries the current
    state so the client can re-render."""
    uow = InMemoryUnitOfWork()
    signal = _make_signal()
    await uow.argus_signals.add(signal)

    # Simulate a previous reviewer's action that bumped the version.
    await uow.argus_signals.update_with_version_check(
        signal_id=signal.id,
        expected_version=signal.version,
        updates={"status": ArgusSignalStatus.CONFIRMED.value},
    )

    with pytest.raises(ArgusSignalModifiedError) as excinfo:
        await ReviewArgusSignal(uow).execute(
            ReviewArgusSignalInput(
                signal_id=signal.id,
                decision=ArgusReviewDecision.DISMISSED,
                expected_version=signal.version,  # now stale
            )
        )
    assert excinfo.value.signal_id == signal.id
    assert excinfo.value.current_version == signal.version + 1
    assert excinfo.value.current_signal is not None
    assert excinfo.value.current_signal["status"] == ArgusSignalStatus.CONFIRMED.value


@pytest.mark.asyncio
async def test_stale_review_does_not_create_review_row():
    """When the pre-check rejects a stale ``expected_version``, no audit row
    is inserted — the conflict-of-intent must not pollute the activity log."""
    uow = InMemoryUnitOfWork()
    signal = _make_signal()
    await uow.argus_signals.add(signal)
    # Bump the version once so the next call is stale.
    await uow.argus_signals.update_with_version_check(
        signal_id=signal.id,
        expected_version=signal.version,
        updates={"status": ArgusSignalStatus.CONFIRMED.value},
    )

    with pytest.raises(ArgusSignalModifiedError):
        await ReviewArgusSignal(uow).execute(
            ReviewArgusSignalInput(
                signal_id=signal.id,
                decision=ArgusReviewDecision.DISMISSED,
                expected_version=signal.version,
            )
        )

    reviews = await uow.argus_signal_reviews.list_for_signal(signal.id)
    assert reviews == []


@pytest.mark.asyncio
async def test_concurrent_race_after_pre_check_yields_modified_error():
    """Simulate a TOCTOU race: pre-check passes, but the SQL update returns
    zero rows because someone won the race between the SELECT and the UPDATE.

    The use case must detect this, roll back the tentative review insert,
    and raise ``ArgusSignalModifiedError``.  We patch the repository's
    ``update_with_version_check`` to always return None to simulate the lost
    race deterministically.
    """
    uow = InMemoryUnitOfWork()
    signal = _make_signal()
    await uow.argus_signals.add(signal)

    async def _always_lose_race(*_args, **_kwargs):
        return None

    uow.argus_signals.update_with_version_check = _always_lose_race  # type: ignore[method-assign]

    with pytest.raises(ArgusSignalModifiedError):
        await ReviewArgusSignal(uow).execute(
            ReviewArgusSignalInput(
                signal_id=signal.id,
                decision=ArgusReviewDecision.CONFIRMED,
                expected_version=signal.version,
            )
        )

    # The use case must have rolled back, not committed.
    assert uow.commits == 0
    assert uow.rollbacks == 1


@pytest.mark.asyncio
async def test_successful_review_commits_exactly_once():
    """The use case is the unit-of-work boundary: one commit, no rollbacks."""
    uow = InMemoryUnitOfWork()
    signal = _make_signal()
    await uow.argus_signals.add(signal)
    await ReviewArgusSignal(uow).execute(
        ReviewArgusSignalInput(
            signal_id=signal.id,
            decision=ArgusReviewDecision.CONFIRMED,
            expected_version=signal.version,
        )
    )
    assert uow.commits == 1
    assert uow.rollbacks == 0
