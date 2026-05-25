"""Fake Argus signal, evidence, and review repositories."""

from __future__ import annotations

from uuid import UUID

from atlas.domain.entities import (
    ArgusSignal,
    ArgusSignalEvidence,
    ArgusSignalReview,
)
from atlas.domain.enums import (
    ArgusSeverity,
    ArgusSignalStatus,
    ArgusSignalType,
)
from atlas.domain.interfaces.repositories import (
    ArgusSignalEvidenceRepository,
    ArgusSignalRepository,
    ArgusSignalReviewRepository,
)
from atlas.domain.services.argus_severity import severity_rank
from atlas.domain.utils import utc_now
from tests.domain.fakes._store import (
    _ArgusStore,
)


class FakeArgusSignalRepository(ArgusSignalRepository):
    def __init__(self, s: _ArgusStore) -> None:
        self._s = s

    async def get(self, id: UUID) -> ArgusSignal | None:
        return self._s.signals.get(id)

    async def add(self, signal: ArgusSignal) -> None:
        self._s.signals[signal.id] = signal

    async def save(self, signal: ArgusSignal) -> None:
        self._s.signals[signal.id] = signal

    async def find_by_dedupe_key(self, dedupe_key: str) -> ArgusSignal | None:
        return next((s for s in self._s.signals.values() if s.dedupe_key == dedupe_key), None)

    async def upsert_signal(self, signal: ArgusSignal) -> tuple[ArgusSignal, bool]:
        existing = await self.find_by_dedupe_key(signal.dedupe_key)
        if existing is not None:
            update: dict[str, object] = {
                "last_detected_at": signal.last_detected_at,
                "updated_at": utc_now(),
            }
            if existing.status == ArgusSignalStatus.AUTO_RESOLVED:
                update["status"] = ArgusSignalStatus.OPEN
            # G4: escalate severity / confidence in lock-step with the SQL
            # repository.  We never downgrade.
            if severity_rank(signal.severity) > severity_rank(existing.severity):
                update["severity"] = signal.severity
            if signal.confidence > existing.confidence:
                update["confidence"] = signal.confidence
            updated = existing.model_copy(update=update)
            self._s.signals[existing.id] = updated
            return (updated, False)
        await self.add(signal)
        return (signal, True)

    async def list(
        self,
        status: ArgusSignalStatus | None = None,
        signal_type: ArgusSignalType | None = None,
        severity: ArgusSeverity | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[ArgusSignal]:
        items = list(self._s.signals.values())
        if status is not None:
            items = [s for s in items if s.status == status]
        if signal_type is not None:
            items = [s for s in items if s.signal_type == signal_type]
        if severity is not None:
            items = [s for s in items if s.severity == severity]
        # G6: stable tiebreaker on ``id`` so identical ``last_detected_at``
        # values produce a deterministic order across calls.  Mirrors the
        # ORDER BY in ``SqlArgusSignalRepository.list``.
        items.sort(key=lambda s: (s.last_detected_at, s.id), reverse=True)
        return items[offset : offset + limit]

    async def list_page(
        self,
        *,
        status: ArgusSignalStatus | None = None,
        signal_type: ArgusSignalType | None = None,
        severity: ArgusSeverity | None = None,
        limit: int = 50,
        after_id: UUID | None = None,
    ) -> list[ArgusSignal]:
        # Mirror of ``SqlArgusSignalRepository.list_page``.  Stale cursors
        # (after_id refers to a deleted signal) silently fall back to
        # "no cursor" — same backward-compat behaviour as the SQL helper.
        items = list(self._s.signals.values())
        if status is not None:
            items = [s for s in items if s.status == status]
        if signal_type is not None:
            items = [s for s in items if s.signal_type == signal_type]
        if severity is not None:
            items = [s for s in items if s.severity == severity]
        items.sort(key=lambda s: (s.last_detected_at, s.id), reverse=True)

        if after_id is not None:
            cursor = self._s.signals.get(after_id)
            if cursor is not None:
                # The cursor is the *last item returned by the previous page*.
                # We want strictly less in (last_detected_at, id) terms — the
                # same predicate the SQL repo emits with ``row_key < cursor_key``.
                cursor_key = (cursor.last_detected_at, cursor.id)
                items = [s for s in items if (s.last_detected_at, s.id) < cursor_key]
        return items[:limit]

    async def update_with_version_check(
        self,
        signal_id: UUID,
        expected_version: int,
        updates: dict,
    ) -> ArgusSignal | None:
        # Mirror of ``SqlArgusSignalRepository.update_with_version_check`` for
        # use-case-level tests.  Returns ``None`` (not an exception) on a
        # version mismatch so the use case decides how to surface the race.
        existing = self._s.signals.get(signal_id)
        if existing is None or existing.version != expected_version:
            return None
        merged_updates: dict[str, object] = dict(updates)
        # The production SQL update receives string-valued enum columns
        # (``ArgusSignalStatus.CONFIRMED.value``).  The fake stores Pydantic
        # models, so coerce known enum-valued columns back to their enum
        # member to keep the round-trip representation faithful.  Mirrors
        # the same coercion in ``FakeConflictRepository.update_with_version_check``.
        if "status" in merged_updates and isinstance(merged_updates["status"], str):
            merged_updates["status"] = ArgusSignalStatus(merged_updates["status"])
        if "severity" in merged_updates and isinstance(merged_updates["severity"], str):
            merged_updates["severity"] = ArgusSeverity(merged_updates["severity"])
        # Mirror SQL: bump version and updated_at on every successful update.
        merged_updates["version"] = existing.version + 1
        merged_updates["updated_at"] = utc_now()
        updated = existing.model_copy(update=merged_updates)
        self._s.signals[signal_id] = updated
        return updated


class FakeArgusSignalEvidenceRepository(ArgusSignalEvidenceRepository):
    def __init__(self, s: _ArgusStore) -> None:
        self._s = s

    async def add(self, evidence: ArgusSignalEvidence) -> None:
        self._s.evidence.append(evidence)

    async def upsert_evidence(
        self, evidence: ArgusSignalEvidence
    ) -> tuple[ArgusSignalEvidence, bool]:
        existing = next(
            (
                e
                for e in self._s.evidence
                if e.signal_id == evidence.signal_id
                and e.evidence_type == evidence.evidence_type
                and e.evidence_id == evidence.evidence_id
            ),
            None,
        )
        if existing is not None:
            return (existing, False)
        await self.add(evidence)
        return (evidence, True)

    async def list_for_signal(self, signal_id: UUID) -> list[ArgusSignalEvidence]:
        return [e for e in self._s.evidence if e.signal_id == signal_id]


class FakeArgusSignalReviewRepository(ArgusSignalReviewRepository):
    def __init__(self, s: _ArgusStore) -> None:
        self._s = s

    async def add(self, review: ArgusSignalReview) -> None:
        self._s.reviews.append(review)

    async def list_for_signal(self, signal_id: UUID) -> list[ArgusSignalReview]:
        return [r for r in self._s.reviews if r.signal_id == signal_id]
