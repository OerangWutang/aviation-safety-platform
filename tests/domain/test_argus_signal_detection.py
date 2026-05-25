"""Domain tests for RunArgusSignalDetection use case."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from atlas.application.use_cases.run_argus_signal_detection import (
    RunArgusSignalDetection,
    RunArgusSignalDetectionInput,
)
from atlas.domain.entities import ChronosSequenceReview, HermesSourceChange
from atlas.domain.enums import (
    ArgusEvidenceType,
    ArgusSignalType,
    ChronosSequenceReviewStatus,
    HermesChangeType,
)
from tests.domain._fake_uow import InMemoryUnitOfWork


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _make_sequence_review(**kwargs) -> ChronosSequenceReview:
    defaults = dict(
        id=uuid4(),
        accident_event_id=uuid4(),
        timeline_event_id_a=uuid4(),
        timeline_event_id_b=uuid4(),
        reason="Out-of-order timestamp",
        status=ChronosSequenceReviewStatus.PENDING,
    )
    defaults.update(kwargs)
    return ChronosSequenceReview(**defaults)


@pytest.mark.asyncio
async def test_creates_signal_from_chronos_pending_review():
    uow = InMemoryUnitOfWork()
    event_id = uuid4()
    review = _make_sequence_review(accident_event_id=event_id)
    uow.store.chronos.sequence_reviews.append(review)

    result = await RunArgusSignalDetection(uow).execute(
        RunArgusSignalDetectionInput(include_hermes=False)
    )

    assert result.signals_created_count == 1
    signal = await uow.argus_signals.get(result.signal_ids[0])
    assert signal is not None
    assert signal.signal_type == ArgusSignalType.TIMELINE_SEQUENCE_CONFLICT
    assert signal.accident_event_id == event_id


@pytest.mark.asyncio
async def test_creates_evidence_link_for_chronos_review():
    uow = InMemoryUnitOfWork()
    review = _make_sequence_review()
    uow.store.chronos.sequence_reviews.append(review)

    result = await RunArgusSignalDetection(uow).execute(
        RunArgusSignalDetectionInput(include_hermes=False)
    )

    assert result.evidence_links_created_count == 1
    evidence = await uow.argus_signal_evidence.list_for_signal(result.signal_ids[0])
    assert len(evidence) == 1
    assert evidence[0].evidence_type == ArgusEvidenceType.CHRONOS_SEQUENCE_REVIEW
    assert evidence[0].evidence_id == review.id


@pytest.mark.asyncio
async def test_running_detection_twice_is_idempotent():
    uow = InMemoryUnitOfWork()
    review = _make_sequence_review(reason="dup")
    uow.store.chronos.sequence_reviews.append(review)

    inp = RunArgusSignalDetectionInput(include_hermes=False)
    r1 = await RunArgusSignalDetection(uow).execute(inp)
    r2 = await RunArgusSignalDetection(uow).execute(inp)

    assert r1.signals_created_count == 1
    assert r2.signals_created_count == 0
    assert r2.signals_reused_count == 1
    assert len(await uow.argus_signals.list()) == 1


@pytest.mark.asyncio
async def test_creates_signal_from_hermes_first_seen():
    uow = InMemoryUnitOfWork()
    change = HermesSourceChange(
        id=uuid4(),
        target_id=uuid4(),
        change_type=HermesChangeType.FIRST_SEEN,
        detected_at=_utc_now(),
    )
    uow.store.hermes.changes.append(change)

    result = await RunArgusSignalDetection(uow).execute(
        RunArgusSignalDetectionInput(include_chronos=False)
    )

    assert result.signals_created_count == 1
    signal = await uow.argus_signals.get(result.signal_ids[0])
    assert signal is not None
    assert signal.signal_type == ArgusSignalType.NEW_SOURCE_CHANGE


@pytest.mark.asyncio
async def test_creates_signal_from_hermes_content_changed():
    uow = InMemoryUnitOfWork()
    change = HermesSourceChange(
        id=uuid4(),
        target_id=uuid4(),
        change_type=HermesChangeType.CONTENT_CHANGED,
        detected_at=_utc_now(),
    )
    uow.store.hermes.changes.append(change)

    result = await RunArgusSignalDetection(uow).execute(
        RunArgusSignalDetectionInput(include_chronos=False)
    )

    assert result.signals_created_count == 1
    signal = await uow.argus_signals.get(result.signal_ids[0])
    assert signal is not None
    assert signal.signal_type == ArgusSignalType.NEW_SOURCE_CHANGE
    assert "content changed" in signal.title.lower()


@pytest.mark.asyncio
async def test_skips_hermes_content_unchanged():
    uow = InMemoryUnitOfWork()
    change = HermesSourceChange(
        id=uuid4(),
        target_id=uuid4(),
        change_type=HermesChangeType.CONTENT_UNCHANGED,
        detected_at=_utc_now(),
    )
    uow.store.hermes.changes.append(change)

    result = await RunArgusSignalDetection(uow).execute(
        RunArgusSignalDetectionInput(include_chronos=False)
    )

    assert result.signals_created_count == 0
    assert len(await uow.argus_signals.list()) == 0


@pytest.mark.asyncio
async def test_fetch_failure_spike_creates_signal_when_5_or_more():
    uow = InMemoryUnitOfWork()
    target_id = uuid4()
    for _ in range(6):
        uow.store.hermes.changes.append(
            HermesSourceChange(
                id=uuid4(),
                target_id=target_id,
                change_type=HermesChangeType.FETCH_FAILED,
                detected_at=_utc_now(),
            )
        )

    result = await RunArgusSignalDetection(uow).execute(
        RunArgusSignalDetectionInput(include_chronos=False)
    )

    assert result.signals_created_count == 1
    signal = await uow.argus_signals.get(result.signal_ids[0])
    assert signal is not None
    assert signal.signal_type == ArgusSignalType.SOURCE_FETCH_FAILURE_SPIKE
    # G3: every failure in the burst is linked as evidence, not just one.
    evidence = await uow.argus_signal_evidence.list_for_signal(signal.id)
    assert len(evidence) == 6
    assert all(e.evidence_type == ArgusEvidenceType.HERMES_SOURCE_CHANGE for e in evidence)
    assert result.evidence_links_created_count == 6


@pytest.mark.asyncio
async def test_detection_does_not_fail_when_orion_requested_but_not_implemented():
    """Requesting the Orion engine when it isn't implemented must not crash.
    Instead the result surfaces the skip in ``engines_skipped`` so operators
    can see the gap without triggering an alert (it's expected, not an error).
    """
    uow = InMemoryUnitOfWork()
    result = await RunArgusSignalDetection(uow).execute(
        RunArgusSignalDetectionInput(
            include_chronos=False,
            include_hermes=False,
            include_atlas=False,
            include_orion=True,
        )
    )
    assert result.signals_created_count == 0
    assert result.engines_errored == []
    assert result.engines_skipped == ["orion"]


# ── G7: NEW_SOURCE_CHANGE dedupes by target + change_type ────────────────────


@pytest.mark.asyncio
async def test_new_source_change_dedupes_by_target_and_change_type():
    """Two CONTENT_CHANGED rows for the *same* target collapse into one signal.

    The dedupe key was previously based on ``change.id``, which gave every row
    its own signal — defeating dedupe entirely.  Keying on ``(target_id,
    change_type)`` is the documented intent.
    """
    from atlas.domain.enums import ArgusSeverity

    uow = InMemoryUnitOfWork()
    target_id = uuid4()
    # Two distinct change rows, same target, same change_type.
    uow.store.hermes.changes.append(
        HermesSourceChange(
            id=uuid4(),
            target_id=target_id,
            change_type=HermesChangeType.CONTENT_CHANGED,
            detected_at=_utc_now(),
        )
    )
    uow.store.hermes.changes.append(
        HermesSourceChange(
            id=uuid4(),
            target_id=target_id,
            change_type=HermesChangeType.CONTENT_CHANGED,
            detected_at=_utc_now(),
        )
    )

    result = await RunArgusSignalDetection(uow).execute(
        RunArgusSignalDetectionInput(include_chronos=False)
    )

    # One signal, but two evidence rows (one per source change).
    assert result.signals_created_count == 1
    assert result.signals_reused_count == 1
    assert result.evidence_links_created_count == 2
    signals = await uow.argus_signals.list()
    assert len(signals) == 1
    assert signals[0].signal_type == ArgusSignalType.NEW_SOURCE_CHANGE
    assert signals[0].severity == ArgusSeverity.MEDIUM


@pytest.mark.asyncio
async def test_new_source_change_keeps_first_seen_and_content_changed_separate():
    """FIRST_SEEN and CONTENT_CHANGED on the same target are different stories
    and should remain different signals, since dedupe is keyed on the pair."""
    uow = InMemoryUnitOfWork()
    target_id = uuid4()
    uow.store.hermes.changes.append(
        HermesSourceChange(
            id=uuid4(),
            target_id=target_id,
            change_type=HermesChangeType.FIRST_SEEN,
            detected_at=_utc_now(),
        )
    )
    uow.store.hermes.changes.append(
        HermesSourceChange(
            id=uuid4(),
            target_id=target_id,
            change_type=HermesChangeType.CONTENT_CHANGED,
            detected_at=_utc_now(),
        )
    )

    result = await RunArgusSignalDetection(uow).execute(
        RunArgusSignalDetectionInput(include_chronos=False)
    )

    assert result.signals_created_count == 2
    signal_types = {s.signal_type for s in await uow.argus_signals.list()}
    assert signal_types == {ArgusSignalType.NEW_SOURCE_CHANGE}


# ── G4: severity escalates on repeated upsert, never downgrades ──────────────


@pytest.mark.asyncio
async def test_fetch_failure_spike_escalates_severity_on_rerun():
    """5 failures → MEDIUM; the next 5 (total 10) → HIGH.

    One signal, twelve evidence rows total at the end (10 unique failures plus
    nothing duplicated thanks to the (signal_id, evidence_type, evidence_id)
    unique constraint).
    """
    from atlas.domain.enums import ArgusSeverity

    uow = InMemoryUnitOfWork()
    target_id = uuid4()
    for _ in range(5):
        uow.store.hermes.changes.append(
            HermesSourceChange(
                id=uuid4(),
                target_id=target_id,
                change_type=HermesChangeType.FETCH_FAILED,
                detected_at=_utc_now(),
            )
        )

    r1 = await RunArgusSignalDetection(uow).execute(
        RunArgusSignalDetectionInput(include_chronos=False)
    )
    assert r1.signals_created_count == 1
    signal_id = r1.signal_ids[0]
    signal = await uow.argus_signals.get(signal_id)
    assert signal is not None
    assert signal.severity == ArgusSeverity.MEDIUM

    # Append five more failures and rerun — count now 10, severity escalates.
    for _ in range(5):
        uow.store.hermes.changes.append(
            HermesSourceChange(
                id=uuid4(),
                target_id=target_id,
                change_type=HermesChangeType.FETCH_FAILED,
                detected_at=_utc_now(),
            )
        )

    r2 = await RunArgusSignalDetection(uow).execute(
        RunArgusSignalDetectionInput(include_chronos=False)
    )
    # No new signal — same dedupe key.
    assert r2.signals_created_count == 0
    assert r2.signals_reused_count == 1
    # All 10 failures linked as evidence (the first 5 were idempotently
    # re-upserted and not re-counted; the second 5 are new).
    assert r2.evidence_links_created_count == 5
    evidence = await uow.argus_signal_evidence.list_for_signal(signal_id)
    assert len(evidence) == 10

    escalated = await uow.argus_signals.get(signal_id)
    assert escalated is not None
    assert escalated.severity == ArgusSeverity.HIGH


@pytest.mark.asyncio
async def test_severity_never_downgrades_on_upsert():
    """If a curator (or future logic) raised the severity above what the
    current evidence justifies, a subsequent detection pass must not lower it.
    """
    from atlas.domain.entities import ArgusSignal
    from atlas.domain.enums import ArgusSeverity

    uow = InMemoryUnitOfWork()
    target_id = uuid4()
    # Seed an existing CRITICAL signal directly so dedupe will find it.
    existing = ArgusSignal(
        signal_type=ArgusSignalType.NEW_SOURCE_CHANGE,
        severity=ArgusSeverity.CRITICAL,
        confidence=0.99,
        title="Existing critical",
        source_engine="hermes",
        dedupe_key=f"ARGUS::NEW_SOURCE_CHANGE::hermes::{target_id}::CONTENT_CHANGED",
    )
    await uow.argus_signals.add(existing)

    uow.store.hermes.changes.append(
        HermesSourceChange(
            id=uuid4(),
            target_id=target_id,
            change_type=HermesChangeType.CONTENT_CHANGED,
            detected_at=_utc_now(),
        )
    )

    await RunArgusSignalDetection(uow).execute(RunArgusSignalDetectionInput(include_chronos=False))

    after = await uow.argus_signals.get(existing.id)
    assert after is not None
    assert after.severity == ArgusSeverity.CRITICAL
    # Confidence likewise must not drop.
    assert after.confidence >= 0.99


# ── G1: no commit when the run wrote nothing ─────────────────────────────────


@pytest.mark.asyncio
async def test_run_with_no_data_does_not_commit():
    """Belt-and-braces: a transient infra failure that returns 0 rows from
    every engine must not produce a committed empty transaction.  The use case
    rolls back instead so operators can correlate "engines errored" with
    "no detection rows" in their logs.
    """
    uow = InMemoryUnitOfWork()
    result = await RunArgusSignalDetection(uow).execute(RunArgusSignalDetectionInput())
    assert result.signals_created_count == 0
    assert result.signals_reused_count == 0
    assert result.evidence_links_created_count == 0
    assert uow.commits == 0
    assert uow.rollbacks == 1


@pytest.mark.asyncio
async def test_run_with_data_commits_exactly_once():
    """The use case is the unit-of-work boundary: exactly one commit when work
    happened, exactly zero rollbacks.
    """
    uow = InMemoryUnitOfWork()
    uow.store.chronos.sequence_reviews.append(_make_sequence_review())
    await RunArgusSignalDetection(uow).execute(RunArgusSignalDetectionInput(include_hermes=False))
    assert uow.commits == 1
    assert uow.rollbacks == 0


# ── G2: per-engine failure is surfaced, not swallowed ────────────────────────


@pytest.mark.asyncio
async def test_chronos_engine_failure_is_reported_in_result():
    """When the Chronos repo raises, the engine name appears in
    ``engines_errored`` and the HTTP-equivalent result remains usable."""
    uow = InMemoryUnitOfWork()

    async def _boom(*_args, **_kwargs):
        raise RuntimeError("simulated chronos failure")

    uow.chronos_sequence_reviews.list_pending = _boom  # type: ignore[method-assign]

    result = await RunArgusSignalDetection(uow).execute(
        RunArgusSignalDetectionInput(include_hermes=False)
    )

    assert result.engines_errored == ["chronos"]
    assert result.signals_created_count == 0


@pytest.mark.asyncio
async def test_hermes_engine_failure_does_not_prevent_chronos_signals():
    """A Hermes failure must not stop Chronos detection from emitting signals."""
    uow = InMemoryUnitOfWork()

    async def _boom(*_args, **_kwargs):
        raise RuntimeError("simulated hermes failure")

    uow.hermes_source_changes.list_recent = _boom  # type: ignore[method-assign]
    uow.store.chronos.sequence_reviews.append(_make_sequence_review())

    result = await RunArgusSignalDetection(uow).execute(RunArgusSignalDetectionInput())

    assert result.engines_errored == ["hermes"]
    assert result.signals_created_count == 1


# ── G6: stable list ordering under ties ──────────────────────────────────────


@pytest.mark.asyncio
async def test_list_signals_is_stably_ordered_on_tied_timestamps():
    """When two signals share ``last_detected_at`` the order must be stable
    across calls.  We use ``id`` as the deterministic tiebreaker so paginated
    consumers don't see rows reorder between requests.
    """
    from atlas.domain.entities import ArgusSignal
    from atlas.domain.enums import ArgusSeverity

    uow = InMemoryUnitOfWork()
    shared_ts = _utc_now()
    a = ArgusSignal(
        signal_type=ArgusSignalType.NEW_SOURCE_CHANGE,
        severity=ArgusSeverity.LOW,
        confidence=0.5,
        title="A",
        source_engine="hermes",
        dedupe_key=f"ARGUS::A::{uuid4()}",
        first_detected_at=shared_ts,
        last_detected_at=shared_ts,
    )
    b = ArgusSignal(
        signal_type=ArgusSignalType.NEW_SOURCE_CHANGE,
        severity=ArgusSeverity.LOW,
        confidence=0.5,
        title="B",
        source_engine="hermes",
        dedupe_key=f"ARGUS::B::{uuid4()}",
        first_detected_at=shared_ts,
        last_detected_at=shared_ts,
    )
    await uow.argus_signals.add(a)
    await uow.argus_signals.add(b)

    first = await uow.argus_signals.list()
    second = await uow.argus_signals.list()
    third = await uow.argus_signals.list()
    assert [s.id for s in first] == [s.id for s in second] == [s.id for s in third]
    # The id-DESC tiebreaker yields a deterministic order independent of dict
    # insertion order in the fake store.
    assert [s.id for s in first] == sorted([a.id, b.id], reverse=True)


# ── Atlas detector — HIGH_CONFLICT_ACCIDENT_RECORD ───────────────────────────


def _make_open_conflict(event_id, field_name: str):
    """Construct an OPEN ClaimConflict for tests.  Helper local to the Atlas
    tests so other detectors don't accidentally rely on it."""
    from atlas.domain.entities import ClaimConflict

    return ClaimConflict(event_id=event_id, field_name=field_name)


def _make_resolved_conflict(event_id, field_name: str):
    """Construct a RESOLVED ClaimConflict (winner already chosen)."""
    from atlas.domain.entities import ClaimConflict
    from atlas.domain.enums import ConflictStatus

    return ClaimConflict(
        event_id=event_id,
        field_name=field_name,
        status=ConflictStatus.RESOLVED,
        winning_claim_id=uuid4(),
        resolved_at=_utc_now(),
        resolved_by=uuid4(),
    )


@pytest.mark.asyncio
async def test_atlas_detector_emits_high_conflict_signal_above_threshold():
    """An event with conflicts >= threshold produces one signal + one evidence
    row per OPEN conflict."""
    from atlas.domain.enums import ArgusEvidenceType

    uow = InMemoryUnitOfWork()
    event_id = uuid4()
    for i in range(3):
        await uow.conflicts.add(_make_open_conflict(event_id, f"field_{i}"))

    result = await RunArgusSignalDetection(uow).execute(
        RunArgusSignalDetectionInput(
            include_chronos=False,
            include_hermes=False,
            include_atlas=True,
            include_orion=False,
            high_conflict_threshold=3,
        )
    )

    assert result.signals_created_count == 1
    assert result.evidence_links_created_count == 3
    signal_id = result.signal_ids[0]
    signal = await uow.argus_signals.get(signal_id)
    assert signal is not None
    assert signal.signal_type == ArgusSignalType.HIGH_CONFLICT_ACCIDENT_RECORD
    assert signal.accident_event_id == event_id
    evidence = await uow.argus_signal_evidence.list_for_signal(signal_id)
    assert len(evidence) == 3
    assert all(e.evidence_type == ArgusEvidenceType.ATLAS_CONFLICT for e in evidence)


@pytest.mark.asyncio
async def test_atlas_detector_does_not_signal_below_threshold():
    """Two OPEN conflicts at threshold=3 → no signal, no commit."""
    uow = InMemoryUnitOfWork()
    event_id = uuid4()
    await uow.conflicts.add(_make_open_conflict(event_id, "field_a"))
    await uow.conflicts.add(_make_open_conflict(event_id, "field_b"))

    result = await RunArgusSignalDetection(uow).execute(
        RunArgusSignalDetectionInput(
            include_chronos=False,
            include_hermes=False,
            include_atlas=True,
            include_orion=False,
            high_conflict_threshold=3,
        )
    )

    assert result.signals_created_count == 0
    assert result.evidence_links_created_count == 0
    assert uow.commits == 0
    assert uow.rollbacks == 1


@pytest.mark.asyncio
async def test_atlas_detector_ignores_resolved_conflicts():
    """Only OPEN conflicts count toward the threshold.  RESOLVED ones are
    historical and should not re-arm the signal."""
    uow = InMemoryUnitOfWork()
    event_id = uuid4()
    await uow.conflicts.add(_make_open_conflict(event_id, "field_a"))
    await uow.conflicts.add(_make_open_conflict(event_id, "field_b"))
    # Two resolved conflicts — should NOT bump the count to 4.
    await uow.conflicts.add(_make_resolved_conflict(event_id, "field_c"))
    await uow.conflicts.add(_make_resolved_conflict(event_id, "field_d"))

    result = await RunArgusSignalDetection(uow).execute(
        RunArgusSignalDetectionInput(
            include_chronos=False,
            include_hermes=False,
            include_atlas=True,
            include_orion=False,
            high_conflict_threshold=3,
        )
    )

    # Only 2 OPEN, threshold is 3 → no signal.
    assert result.signals_created_count == 0


@pytest.mark.asyncio
async def test_atlas_detector_escalates_severity_as_conflicts_grow():
    """3 OPEN → LOW; add more until 10 → HIGH.  One signal throughout."""
    from atlas.domain.enums import ArgusSeverity

    uow = InMemoryUnitOfWork()
    event_id = uuid4()
    for i in range(3):
        await uow.conflicts.add(_make_open_conflict(event_id, f"field_{i}"))

    r1 = await RunArgusSignalDetection(uow).execute(
        RunArgusSignalDetectionInput(
            include_chronos=False,
            include_hermes=False,
            include_atlas=True,
            include_orion=False,
            high_conflict_threshold=3,
        )
    )
    assert r1.signals_created_count == 1
    signal_id = r1.signal_ids[0]
    signal = await uow.argus_signals.get(signal_id)
    assert signal is not None
    assert signal.severity == ArgusSeverity.LOW

    # Grow to 10 open conflicts → HIGH band.
    for i in range(3, 10):
        await uow.conflicts.add(_make_open_conflict(event_id, f"field_{i}"))

    r2 = await RunArgusSignalDetection(uow).execute(
        RunArgusSignalDetectionInput(
            include_chronos=False,
            include_hermes=False,
            include_atlas=True,
            include_orion=False,
            high_conflict_threshold=3,
        )
    )
    assert r2.signals_created_count == 0  # dedupe-keyed on event_id
    assert r2.signals_reused_count == 1
    escalated = await uow.argus_signals.get(signal_id)
    assert escalated is not None
    assert escalated.severity == ArgusSeverity.HIGH


@pytest.mark.asyncio
async def test_atlas_detector_orders_events_by_conflict_count_descending():
    """When multiple events qualify, signals should be created for all of
    them, with the noisiest event first."""
    uow = InMemoryUnitOfWork()
    event_loud = uuid4()
    event_quiet = uuid4()
    for i in range(8):
        await uow.conflicts.add(_make_open_conflict(event_loud, f"f{i}"))
    for i in range(3):
        await uow.conflicts.add(_make_open_conflict(event_quiet, f"g{i}"))

    result = await RunArgusSignalDetection(uow).execute(
        RunArgusSignalDetectionInput(
            include_chronos=False,
            include_hermes=False,
            include_atlas=True,
            include_orion=False,
            high_conflict_threshold=3,
        )
    )

    assert result.signals_created_count == 2
    # The aggregate query returns loud-event first; our signal_ids list
    # preserves that order.
    signals = [await uow.argus_signals.get(sid) for sid in result.signal_ids]
    assert signals[0] is not None and signals[0].accident_event_id == event_loud
    assert signals[1] is not None and signals[1].accident_event_id == event_quiet


@pytest.mark.asyncio
async def test_atlas_detector_failure_is_surfaced_in_engines_errored():
    """A repository failure short-circuits the Atlas engine but does not
    break the run."""
    uow = InMemoryUnitOfWork()

    # Seed enough for the *other* engines to not produce work either, so the
    # only effect is the Atlas failure path.
    async def _boom(*_args, **_kwargs):
        raise RuntimeError("simulated atlas count failure")

    uow.conflicts.count_open_conflicts_per_event = _boom  # type: ignore[method-assign]

    result = await RunArgusSignalDetection(uow).execute(
        RunArgusSignalDetectionInput(
            include_chronos=False,
            include_hermes=False,
            include_atlas=True,
            include_orion=False,
        )
    )
    assert result.engines_errored == ["atlas"]
    assert result.signals_created_count == 0


@pytest.mark.asyncio
async def test_atlas_detector_skips_when_threshold_below_two():
    """Threshold < 2 is rejected upstream by the request schema, but the
    detector also fails safe in case the use case is invoked directly."""
    uow = InMemoryUnitOfWork()
    event_id = uuid4()
    for i in range(5):
        await uow.conflicts.add(_make_open_conflict(event_id, f"f{i}"))

    result = await RunArgusSignalDetection(uow).execute(
        RunArgusSignalDetectionInput(
            include_chronos=False,
            include_hermes=False,
            include_atlas=True,
            include_orion=False,
            high_conflict_threshold=1,
        )
    )
    assert result.signals_created_count == 0
    assert result.engines_errored == []  # not an "error" — a defensive skip


@pytest.mark.asyncio
async def test_count_open_conflicts_per_event_rejects_min_count_below_two():
    """Belt-and-braces — the fake matches the SQL contract."""
    uow = InMemoryUnitOfWork()
    with pytest.raises(ValueError, match="min_count"):
        await uow.conflicts.count_open_conflicts_per_event(min_count=1)


# ── signals_created_by_type invariant ────────────────────────────────────────


@pytest.mark.asyncio
async def test_signals_created_by_type_matches_created_count():
    """The per-type breakdown must always sum to the total — otherwise the
    Prometheus counter would diverge from the API's ``signals_created_count``.
    """
    uow = InMemoryUnitOfWork()
    # Mix multiple types so the breakdown has more than one key.
    uow.store.chronos.sequence_reviews.append(_make_sequence_review())
    uow.store.chronos.sequence_reviews.append(_make_sequence_review())
    target_id = uuid4()
    for _ in range(6):
        uow.store.hermes.changes.append(
            HermesSourceChange(
                id=uuid4(),
                target_id=target_id,
                change_type=HermesChangeType.FETCH_FAILED,
                detected_at=_utc_now(),
            )
        )

    result = await RunArgusSignalDetection(uow).execute(RunArgusSignalDetectionInput())

    assert sum(result.signals_created_by_type.values()) == result.signals_created_count
    assert result.signals_created_by_type[ArgusSignalType.TIMELINE_SEQUENCE_CONFLICT.value] == 2
    assert result.signals_created_by_type[ArgusSignalType.SOURCE_FETCH_FAILURE_SPIKE.value] == 1


@pytest.mark.asyncio
async def test_signals_created_by_type_omits_reused_signals():
    """Only *new* signals are counted in the breakdown.  Re-runs that just
    refresh ``last_detected_at`` must not double-count.
    """
    uow = InMemoryUnitOfWork()
    target_id = uuid4()
    uow.store.hermes.changes.append(
        HermesSourceChange(
            id=uuid4(),
            target_id=target_id,
            change_type=HermesChangeType.CONTENT_CHANGED,
            detected_at=_utc_now(),
        )
    )

    r1 = await RunArgusSignalDetection(uow).execute(
        RunArgusSignalDetectionInput(include_chronos=False)
    )
    assert r1.signals_created_by_type == {ArgusSignalType.NEW_SOURCE_CHANGE.value: 1}

    # Re-add an equivalent change for the same target — dedupe kicks in.
    uow.store.hermes.changes.append(
        HermesSourceChange(
            id=uuid4(),
            target_id=target_id,
            change_type=HermesChangeType.CONTENT_CHANGED,
            detected_at=_utc_now(),
        )
    )
    r2 = await RunArgusSignalDetection(uow).execute(
        RunArgusSignalDetectionInput(include_chronos=False)
    )
    assert r2.signals_created_count == 0
    # No new types created, so the breakdown stays empty.
    assert r2.signals_created_by_type == {}
