"""RunArgusSignalDetection — deterministic signal detection over Chronos + Hermes.

Design notes
------------
- The use case owns the unit-of-work boundary: it calls ``await uow.commit()``
  exactly once, and only when at least one mutation occurred.  When neither
  engine produced or refreshed a row, we explicitly ``rollback`` so a transient
  infra failure can't leave a committed empty transaction in the audit trail.
  (See risk G1 in the architect's review.)
- Each detector wraps **only** the repository call in ``try/except``.  A
  failure short-circuits that engine but does not crash the API; instead the
  engine name is appended to ``result.engines_errored`` so the caller (and
  operators) can detect a partial run.  (See risk G2.)
- ``NEW_SOURCE_CHANGE`` dedupes by ``(target_id, change_type)`` so repeated
  changes to the same Hermes target collapse into a single signal whose
  evidence grows over time.  Keying on ``change.id`` defeats dedupe entirely.
  (See risk G7.)
- ``SOURCE_FETCH_FAILURE_SPIKE`` is one signal per target, but every failure
  in the window is linked as evidence so reviewers see the full burst rather
  than one arbitrary failure row.  (See risk G3.)
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from atlas.application.unit_of_work import UnitOfWork
from atlas.domain.entities import (
    ArgusDetectionResult,
    ArgusSignal,
    ArgusSignalEvidence,
    HermesSourceChange,
)
from atlas.domain.enums import (
    ArgusEvidenceType,
    ArgusSignalType,
    ConflictStatus,
    HermesChangeType,
)
from atlas.domain.services.argus_dedupe import make_argus_dedupe_key
from atlas.domain.services.argus_severity import (
    severity_for_atlas_high_conflict,
    severity_for_chronos_sequence_conflict,
    severity_for_hermes_fetch_failure_spike,
    severity_for_hermes_source_change,
)

logger = logging.getLogger(__name__)

# Threshold below which we don't open a SOURCE_FETCH_FAILURE_SPIKE signal.
# Matches the lowest band returned by ``severity_for_hermes_fetch_failure_spike``.
_FETCH_FAILURE_SPIKE_THRESHOLD = 5


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass
class RunArgusSignalDetectionInput:
    include_chronos: bool = True
    include_hermes: bool = True
    include_atlas: bool = True
    # Orion detector is NOT YET IMPLEMENTED.  Defaults to ``False`` so the
    # API response is honest: a ``True`` value silently produced no signals,
    # which misleads operators into thinking Orion was actually evaluated.
    # Set to ``True`` only if you are testing the stub path explicitly.
    include_orion: bool = False
    recent_limit: int = 100
    # Minimum number of OPEN ``claim_conflicts`` an event must have before
    # Argus emits a ``HIGH_CONFLICT_ACCIDENT_RECORD`` signal.  Must be >= 2;
    # the underlying repo enforces this at the DB layer too.  The signal's
    # *severity* is then determined by ``severity_for_atlas_high_conflict``
    # independently of this threshold, so a single deployment can tune the
    # noise floor without losing severity escalation as numbers grow.
    high_conflict_threshold: int = 3


class RunArgusSignalDetection:
    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    async def execute(
        self, input: RunArgusSignalDetectionInput | None = None
    ) -> ArgusDetectionResult:
        if input is None:
            input = RunArgusSignalDetectionInput()

        # Defence-in-depth validation for non-HTTP callers that bypass the API
        # schema bounds.  The schema enforces ge=1, le=1000 at the HTTP layer;
        # this catches direct use-case calls with bad values.
        if not (1 <= input.recent_limit <= 1000):
            raise ValueError(f"recent_limit must be between 1 and 1000, got {input.recent_limit}")

        result = ArgusDetectionResult()
        now = _utc_now()

        if input.include_chronos:
            await self._detect_chronos(result, input.recent_limit, now)
        if input.include_hermes:
            await self._detect_hermes(result, input.recent_limit, now)
        if input.include_atlas:
            await self._detect_atlas(
                result,
                min_count=input.high_conflict_threshold,
                limit=input.recent_limit,
                now=now,
            )
        if input.include_orion:
            # The Orion detector is not yet implemented.  Rather than silently
            # producing zero signals (which operators could mistake for "Orion
            # found nothing"), we record the engine as skipped so the response
            # and dashboards make the gap visible.  This is distinct from
            # ``engines_errored`` — a skip is intentional, an error is not.
            result.engines_skipped.append("orion")

        # G1: only commit when something was actually written or touched.
        # ``signals_reused_count`` is included because the SQL upsert mutates
        # ``last_detected_at`` (and possibly ``severity``) on reuse — that's a
        # real write we need to persist.  When the run found nothing at all,
        # we roll back so transient infra failures don't leave empty committed
        # transactions for operators to investigate.
        wrote_anything = (
            result.signals_created_count
            + result.signals_reused_count
            + result.evidence_links_created_count
            > 0
        )
        if wrote_anything:
            await self._uow.commit()
        else:
            await self._uow.rollback()
        return result

    # ── Chronos ───────────────────────────────────────────────────────────────

    async def _detect_chronos(
        self, result: ArgusDetectionResult, limit: int, now: datetime
    ) -> None:
        try:
            pending = await self._uow.chronos_sequence_reviews.list_pending(limit=limit)
        except Exception:
            # G2: tight catch — only the repo call.  We swallow because the
            # other engines can still produce useful output; the engine name
            # is surfaced to the caller for monitoring.
            logger.exception("Argus Chronos detector: list_pending failed; skipping.")
            result.engines_errored.append("chronos")
            return

        severity, confidence = severity_for_chronos_sequence_conflict()
        for review in pending:
            dedupe_key = make_argus_dedupe_key(
                ArgusSignalType.TIMELINE_SEQUENCE_CONFLICT,
                "chronos",
                [str(review.id)],
            )
            signal = ArgusSignal(
                signal_type=ArgusSignalType.TIMELINE_SEQUENCE_CONFLICT,
                severity=severity,
                confidence=confidence,
                title="Timeline sequence conflict detected",
                description=review.reason,
                accident_event_id=review.accident_event_id,
                source_engine="chronos",
                dedupe_key=dedupe_key,
                first_detected_at=now,
                last_detected_at=now,
            )
            signal = await self._upsert_signal(signal, result)
            await self._upsert_evidence(
                ArgusSignalEvidence(
                    signal_id=signal.id,
                    evidence_type=ArgusEvidenceType.CHRONOS_SEQUENCE_REVIEW,
                    evidence_id=review.id,
                    engine="chronos",
                    summary=review.reason,
                ),
                result,
            )

    # ── Hermes ────────────────────────────────────────────────────────────────

    async def _detect_hermes(self, result: ArgusDetectionResult, limit: int, now: datetime) -> None:
        try:
            changes = await self._uow.hermes_source_changes.list_recent(limit=limit)
        except Exception:
            logger.exception("Argus Hermes detector: list_recent failed; skipping.")
            result.engines_errored.append("hermes")
            return

        # Build one pass through ``changes`` so we can emit (a) per-change
        # NEW_SOURCE_CHANGE signals and (b) per-target spike signals that link
        # *every* failed change in the burst as evidence.  Iteration order is
        # preserved so behaviour stays deterministic across runs.
        failures_by_target: dict[UUID, list[HermesSourceChange]] = defaultdict(list)
        non_failure_changes: list[HermesSourceChange] = []
        for change in changes:
            if change.change_type == HermesChangeType.FETCH_FAILED:
                failures_by_target[change.target_id].append(change)
            elif change.change_type == HermesChangeType.CONTENT_UNCHANGED:
                # By design we do not emit signals for "nothing changed".
                continue
            else:
                non_failure_changes.append(change)

        # ── NEW_SOURCE_CHANGE per change_type per target ────────────────────
        for change in non_failure_changes:
            ct = change.change_type
            assert ct is not None  # narrowed by the filter above
            severity, confidence = severity_for_hermes_source_change(ct)
            title = (
                "New source first seen"
                if ct == HermesChangeType.FIRST_SEEN
                else "Source content changed"
            )
            # G7: dedupe on (target_id, change_type) — NOT change.id — so the
            # second time we observe content changing on the same target we
            # update the existing signal instead of creating a new one.
            dedupe_key = make_argus_dedupe_key(
                ArgusSignalType.NEW_SOURCE_CHANGE,
                "hermes",
                [str(change.target_id), ct.value],
            )
            signal = ArgusSignal(
                signal_type=ArgusSignalType.NEW_SOURCE_CHANGE,
                severity=severity,
                confidence=confidence,
                title=title,
                description=f"change_type={ct.value} target_id={change.target_id}",
                source_engine="hermes",
                dedupe_key=dedupe_key,
                first_detected_at=now,
                last_detected_at=now,
            )
            signal = await self._upsert_signal(signal, result)
            await self._upsert_evidence(
                ArgusSignalEvidence(
                    signal_id=signal.id,
                    evidence_type=ArgusEvidenceType.HERMES_SOURCE_CHANGE,
                    evidence_id=change.id,
                    engine="hermes",
                    summary=f"change_type={ct.value} target_id={change.target_id}",
                ),
                result,
            )

        # ── SOURCE_FETCH_FAILURE_SPIKE per target ──────────────────────────
        for target_id, failures in failures_by_target.items():
            count = len(failures)
            if count < _FETCH_FAILURE_SPIKE_THRESHOLD:
                continue
            severity, confidence = severity_for_hermes_fetch_failure_spike(count)
            dedupe_key = make_argus_dedupe_key(
                ArgusSignalType.SOURCE_FETCH_FAILURE_SPIKE,
                "hermes",
                [str(target_id)],
            )
            signal = ArgusSignal(
                signal_type=ArgusSignalType.SOURCE_FETCH_FAILURE_SPIKE,
                severity=severity,
                confidence=confidence,
                title=f"Fetch failure spike: {count} recent failures for target",
                description=f"Target {target_id} has {count} recent fetch failures.",
                source_engine="hermes",
                dedupe_key=dedupe_key,
                first_detected_at=now,
                last_detected_at=now,
            )
            signal = await self._upsert_signal(signal, result)
            # G3: link every failure in the burst as evidence (idempotent via
            # the unique constraint on (signal_id, evidence_type, evidence_id)).
            for failure in failures:
                await self._upsert_evidence(
                    ArgusSignalEvidence(
                        signal_id=signal.id,
                        evidence_type=ArgusEvidenceType.HERMES_SOURCE_CHANGE,
                        evidence_id=failure.id,
                        engine="hermes",
                        summary=(
                            f"change_type={HermesChangeType.FETCH_FAILED.value} "
                            f"target_id={failure.target_id}"
                        ),
                    ),
                    result,
                )

    # ── Atlas ────────────────────────────────────────────────────────────────

    async def _detect_atlas(
        self,
        result: ArgusDetectionResult,
        *,
        min_count: int,
        limit: int,
        now: datetime,
    ) -> None:
        """Emit ``HIGH_CONFLICT_ACCIDENT_RECORD`` signals for events whose OPEN
        ``claim_conflicts`` count is at least ``min_count``.

        One signal per event, dedupe-keyed on the event id, so re-running
        detection updates the existing signal (escalating severity if more
        conflicts have piled up) instead of creating duplicates.  Every OPEN
        conflict on the event is linked as ``ATLAS_CONFLICT`` evidence so
        reviewers can click through directly.
        """
        if min_count < 2:
            # Defensive: the repo also rejects this, but raising early gives a
            # nicer call site for unit tests and prevents a wasted DB round-trip.
            logger.warning(
                "Argus Atlas detector: high_conflict_threshold=%d is below 2; skipping.",
                min_count,
            )
            return
        try:
            high_events = await self._uow.conflicts.count_open_conflicts_per_event(
                min_count=min_count, limit=limit
            )
        except Exception:
            logger.exception(
                "Argus Atlas detector: count_open_conflicts_per_event failed; skipping."
            )
            result.engines_errored.append("atlas")
            return

        for event_id, open_count in high_events:
            severity, confidence = severity_for_atlas_high_conflict(open_count)
            dedupe_key = make_argus_dedupe_key(
                ArgusSignalType.HIGH_CONFLICT_ACCIDENT_RECORD,
                "atlas",
                [str(event_id)],
            )
            signal = ArgusSignal(
                signal_type=ArgusSignalType.HIGH_CONFLICT_ACCIDENT_RECORD,
                severity=severity,
                confidence=confidence,
                title=f"High-conflict accident record: {open_count} open conflicts",
                description=(
                    f"Event {event_id} has {open_count} OPEN claim conflicts "
                    f"(threshold={min_count})."
                ),
                accident_event_id=event_id,
                source_engine="atlas",
                dedupe_key=dedupe_key,
                first_detected_at=now,
                last_detected_at=now,
            )
            signal = await self._upsert_signal(signal, result)

            # Link the OPEN conflicts as evidence.  ``find_by_event`` returns
            # everything for the event; we filter to OPEN here because that's
            # the slice that motivated the signal.  Already-linked conflicts
            # are idempotent via the (signal_id, evidence_type, evidence_id)
            # unique constraint, so re-runs grow the evidence set in lockstep
            # with new OPEN conflicts and never duplicate older ones.
            conflicts = await self._uow.conflicts.find_by_event(event_id)
            for c in conflicts:
                if c.status != ConflictStatus.OPEN:
                    continue
                await self._upsert_evidence(
                    ArgusSignalEvidence(
                        signal_id=signal.id,
                        evidence_type=ArgusEvidenceType.ATLAS_CONFLICT,
                        evidence_id=c.id,
                        engine="atlas",
                        summary=f"OPEN conflict on field={c.field_name}",
                    ),
                    result,
                )

    # ── Shared upsert plumbing ───────────────────────────────────────────────

    async def _upsert_signal(
        self, signal: ArgusSignal, result: ArgusDetectionResult
    ) -> ArgusSignal:
        persisted, created = await self._uow.argus_signals.upsert_signal(signal)
        if created:
            result.signals_created_count += 1
            # Maintain the per-type breakdown in lock-step with the total
            # so the invariant ``sum(by_type.values()) == created_count`` holds.
            key = signal.signal_type.value
            result.signals_created_by_type[key] = result.signals_created_by_type.get(key, 0) + 1
        else:
            result.signals_reused_count += 1
        if persisted.id not in result.signal_ids:
            result.signal_ids.append(persisted.id)
        return persisted

    async def _upsert_evidence(
        self, evidence: ArgusSignalEvidence, result: ArgusDetectionResult
    ) -> None:
        persisted, created = await self._uow.argus_signal_evidence.upsert_evidence(evidence)
        if created:
            result.evidence_links_created_count += 1
            result.evidence_ids.append(persisted.id)
