"""Fake repositories: source, ingestion, events, claims, conflicts, projection, outbox."""

from __future__ import annotations

import copy
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from uuid import UUID

from atlas.domain.entities import (
    AccidentEvent,
    AccidentProjectionHistory,
    ArchiveManifest,
    Claim,
    ClaimConflict,
    ClaimHistory,
    ConflictActivityLogEntry,
    EventIdentityIndex,
    IngestionRun,
    OutboxEvent,
    PendingDuplicateReview,
    ProjectedAccidentRecord,
    RawSnapshot,
    Source,
)
from atlas.domain.enums import (
    ClaimType,
    ConflictStatus,
    DuplicateReviewStatus,
    OutboxStatus,
)
from atlas.domain.exceptions import IngestionRunSourceMismatchError
from tests.domain.fakes._store import (
    _cap_registration_norms,
    _normalise_registration_lookup,
    _slice_after_id,
    _Store,
)


class FakeSourceRepository:
    def __init__(self, store: _Store) -> None:
        self._s = store

    async def get(self, id: UUID) -> Source | None:
        return self._s.sources.get(id)

    async def get_by_name(self, name: str) -> Source | None:
        return next((s for s in self._s.sources.values() if s.name == name), None)

    async def get_all(self) -> list[Source]:
        return list(self._s.sources.values())

    async def get_by_ids(self, ids: list[UUID]) -> list[Source]:
        return [self._s.sources[i] for i in ids if i in self._s.sources]

    async def add(self, source: Source) -> None:
        self._s.sources[source.id] = source

    async def update_field_mapping(
        self, source_id: UUID, field_mapping: dict[str, str]
    ) -> Source | None:
        existing = self._s.sources.get(source_id)
        if existing is None:
            return None
        updated = existing.model_copy(update={"field_mapping_json": dict(field_mapping)})
        self._s.sources[source_id] = updated
        return updated


class FakeRawSnapshotRepository:
    def __init__(self, store: _Store) -> None:
        self._s = store

    async def add(self, snapshot: RawSnapshot) -> None:
        self._s.snapshots[snapshot.id] = snapshot
        self._s.snapshots_by_run[(snapshot.source_id, snapshot.ingestion_run_id)] = snapshot

    async def get(self, snapshot_id: UUID) -> RawSnapshot | None:
        return self._s.snapshots.get(snapshot_id)

    async def try_add_unique(self, snapshot: RawSnapshot) -> bool:
        run_key = (snapshot.source_id, snapshot.ingestion_run_id)
        if run_key in self._s.snapshots_by_run:
            return False
        self._s.snapshots[snapshot.id] = snapshot
        self._s.snapshots_by_run[run_key] = snapshot
        return True

    async def find_by_source_run(
        self, source_id: UUID, ingestion_run_id: UUID
    ) -> RawSnapshot | None:
        return self._s.snapshots_by_run.get((source_id, ingestion_run_id))

    async def update_ingestion_result(
        self, snapshot_id: UUID, result_json: dict[str, object]
    ) -> None:
        snapshot = self._s.snapshots.get(snapshot_id)
        if snapshot is None:
            raise RuntimeError(f"Failed to persist ingestion result for snapshot {snapshot_id}")
        snapshot.ingestion_result_json = result_json

    async def find_latest_by_source_record_id(
        self, source_id: UUID, source_record_id: str
    ) -> RawSnapshot | None:
        candidates = [
            s
            for s in self._s.snapshots.values()
            if s.source_id == source_id and s.source_record_id == source_record_id
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda s: s.created_at)

    async def find_latest_event_id_by_source_record_id(
        self, source_id: UUID, source_record_id: str
    ) -> UUID | None:
        candidates = sorted(
            (
                s
                for s in self._s.snapshots.values()
                if s.source_id == source_id and s.source_record_id == source_record_id
            ),
            key=lambda s: s.created_at,
            reverse=True,
        )
        for snapshot in candidates:
            for claim in sorted(self._s.claims.values(), key=lambda c: c.created_at, reverse=True):
                if claim.raw_snapshot_id == snapshot.id:
                    return claim.event_id
        return None

    async def lock_for_source_record_correction(
        self, source_id: UUID, source_record_id: str
    ) -> None:
        # No-op: single-threaded asyncio tests never race on source records.
        return None


class FakeIngestionRunRepository:
    def __init__(self, store: _Store) -> None:
        self._s = store

    async def add(self, run: IngestionRun) -> None:
        self._s.ingestion_runs[run.id] = run

    async def get(self, id: UUID) -> IngestionRun | None:
        return self._s.ingestion_runs.get(id)

    async def update_status(self, id: UUID, status: str, finished_at: object | None = None) -> None:
        run = self._s.ingestion_runs.get(id)
        if run is None:
            raise RuntimeError(f"Failed to update ingestion run {id} to status {status!r}")
        run.status = status
        run.finished_at = (
            finished_at
            if isinstance(finished_at, datetime)
            else (datetime.now(UTC) if status in {"finished", "completed", "failed"} else None)
        )

    async def ensure_started(self, id: UUID, source_id: UUID) -> None:
        if id not in self._s.ingestion_runs:
            self._s.ingestion_runs[id] = IngestionRun(id=id, source_id=source_id, status="running")
            return
        existing = self._s.ingestion_runs[id]
        if existing.source_id != source_id:
            raise IngestionRunSourceMismatchError(
                run_id=id,
                expected_source_id=source_id,
                actual_source_id=existing.source_id,
            )


class FakeAccidentEventRepository:
    def __init__(self, store: _Store) -> None:
        self._s = store

    async def get(self, id: UUID) -> AccidentEvent | None:
        return self._s.events.get(id)

    async def lock_for_update(self, id: UUID) -> AccidentEvent | None:
        # No-op/read: in-memory unit tests run single-threaded.  PostgreSQL
        # row-lock behaviour is exercised by integration tests.
        return self._s.events.get(id)

    async def add(self, event: AccidentEvent) -> None:
        self._s.events[event.id] = event

    async def save(self, event: AccidentEvent) -> None:
        self._s.events[event.id] = event

    async def update(self, event: AccidentEvent) -> None:
        self._s.events[event.id] = event

    async def list_all_ids(self) -> list[UUID]:
        return list(self._s.events.keys())

    async def list_ids_page(self, limit: int, offset: int = 0) -> list[UUID]:
        return sorted(self._s.events.keys())[offset : offset + limit]

    async def list_ids_after_keyset(self, after_id: UUID | None, limit: int) -> list[UUID]:
        ids = sorted(self._s.events.keys())
        if after_id is not None:
            ids = [event_id for event_id in ids if event_id > after_id]
        return ids[:limit]

    async def lock_for_reprojection(self, event_id: UUID) -> None:
        # No-op: the in-memory fake runs single-threaded inside one event
        # loop, so there is nothing to serialize. The SQL impl uses
        # ``pg_advisory_xact_lock``; that contention path is exercised by the
        # integration tests against a real Postgres.
        return None

    async def try_atomic_merge(self, source_event_id: UUID, target_event_id: UUID) -> bool:
        # In the in-memory fake we simulate the conditional UPDATE atomically
        # (single-threaded, so no actual race).  Returns True only if the
        # source event had merged_into_event_id == None before this call.
        event = self._s.events.get(source_event_id)
        if event is None or event.merged_into_event_id is not None:
            return False
        event.merged_into_event_id = target_event_id
        return True

    async def find_existing_ids(self, ids: list[UUID]) -> set[UUID]:
        """Return the subset of ids that exist in the in-memory store."""
        return {i for i in ids if i in self._s.events}


class FakeClaimRepository:
    def __init__(self, store: _Store) -> None:
        self._s = store

    async def get(self, id: UUID) -> Claim | None:
        return self._s.claims.get(id)

    async def lock_for_update(self, id: UUID) -> Claim | None:
        return self._s.claims.get(id)

    async def get_many(self, claim_ids: list[UUID]) -> list[Claim]:
        return [copy.deepcopy(self._s.claims[i]) for i in claim_ids if i in self._s.claims]

    async def add(self, claim: Claim) -> None:
        self._s.claims[claim.id] = claim

    async def update(self, claim: Claim) -> None:
        self._s.claims[claim.id] = claim

    async def find_active_by_event(self, event_id: UUID) -> list[Claim]:
        return [c for c in self._s.claims.values() if c.event_id == event_id and c.is_active]

    async def find_all_by_event(
        self,
        event_id: UUID,
        *,
        limit: int | None = None,
        after_id: UUID | None = None,
    ) -> list[Claim]:
        items = sorted(
            (c for c in self._s.claims.values() if c.event_id == event_id),
            key=lambda c: (c.created_at, str(c.id)),
        )
        return _slice_after_id(items, after_id, limit)

    async def find_event_id_by_raw_snapshot_id(self, raw_snapshot_id: UUID) -> UUID | None:
        claims = [c for c in self._s.claims.values() if c.raw_snapshot_id == raw_snapshot_id]
        if not claims:
            return None
        claims.sort(
            key=lambda c: (
                1 if c.claim_type == ClaimType.SUPERSEDED else 0,
                -c.created_at.timestamp(),
                str(c.id),
            )
        )
        return claims[0].event_id

    async def find_active_by_event_field(self, event_id: UUID, field_name: str) -> list[Claim]:
        return [
            c
            for c in self._s.claims.values()
            if c.event_id == event_id and c.field_name == field_name and c.is_active
        ]

    async def find_active_by_source_record(
        self, source_id: UUID, source_record_id: str
    ) -> list[Claim]:
        results = []
        for claim in self._s.claims.values():
            if not claim.is_active or claim.source_id != source_id or claim.raw_snapshot_id is None:
                continue
            snapshot = self._s.snapshots.get(claim.raw_snapshot_id)
            if snapshot is None:
                continue
            if snapshot.source_id == source_id and snapshot.source_record_id == source_record_id:
                results.append(claim)
        return results

    async def bulk_supersede(self, claim_ids: list[UUID], by_claim_id: UUID) -> list[Claim]:
        active_ids = [
            cid for cid in claim_ids if cid in self._s.claims and self._s.claims[cid].is_active
        ]
        snapshot = [copy.deepcopy(self._s.claims[i]) for i in active_ids]
        for cid in active_ids:
            self._s.claims[cid].claim_type = ClaimType.SUPERSEDED
            self._s.claims[cid].superseded_by_claim_id = by_claim_id
        return snapshot

    async def find_superseded_by(self, by_claim_id: UUID) -> list[Claim]:
        return [c for c in self._s.claims.values() if c.superseded_by_claim_id == by_claim_id]

    async def bulk_unsupersede(self, claim_ids: list[UUID]) -> list[Claim]:
        reactivated: list[Claim] = []
        for cid in claim_ids:
            claim = self._s.claims.get(cid)
            if claim is None:
                continue
            restored_type = ClaimType.RAW
            for history in reversed(self._s.claim_history):
                if (
                    history.claim_id == cid
                    and history.to_claim_type == ClaimType.SUPERSEDED
                    and history.from_claim_type is not None
                ):
                    restored_type = (
                        ClaimType.RAW
                        if history.from_claim_type == ClaimType.SUPERSEDED
                        else history.from_claim_type
                    )
                    break
            claim.claim_type = restored_type
            claim.superseded_by_claim_id = None
            reactivated.append(copy.deepcopy(claim))
        return reactivated

    async def count_total(self) -> int:
        return len(self._s.claims)


class FakeClaimHistoryRepository:
    def __init__(self, store: _Store) -> None:
        self._s = store

    async def add(self, history: ClaimHistory) -> None:
        self._s.claim_history.append(history)

    async def find_by_event(
        self,
        event_id: UUID,
        *,
        limit: int | None = None,
        after_id: UUID | None = None,
    ) -> list[ClaimHistory]:
        items = sorted(
            (h for h in self._s.claim_history if h.event_id == event_id),
            key=lambda h: (h.created_at, str(h.id)),
        )
        return _slice_after_id(items, after_id, limit)


class FakeConflictRepository:
    def __init__(self, store: _Store) -> None:
        self._s = store

    def _hydrate(self, conflict: ClaimConflict) -> ClaimConflict:
        """Return a fresh Pydantic instance with claim_ids attached.

        Real SqlAlchemy repositories build fresh domain objects from ORM rows
        on every call. Returning live store references here would let callers
        accidentally mutate the store via ``conflict.resolve(...)`` and break
        the optimistic version check - the real DB has no such leak.
        """
        copy_ = conflict.model_copy(deep=True)
        copy_.claim_ids = list(self._s.conflict_claim_links.get(conflict.id, []))
        return copy_

    async def get(self, id: UUID) -> ClaimConflict | None:
        c = self._s.conflicts.get(id)
        return self._hydrate(c) if c is not None else None

    async def add(self, conflict: ClaimConflict) -> None:
        self._s.conflicts[conflict.id] = conflict.model_copy(deep=True)
        self._s.conflict_claim_links.setdefault(conflict.id, [])

    async def try_add_open(self, conflict: ClaimConflict) -> bool:
        """Insert if no OPEN conflict for (event_id, field_name) exists yet."""
        for existing in self._s.conflicts.values():
            if (
                existing.event_id == conflict.event_id
                and existing.field_name == conflict.field_name
                and existing.status == ConflictStatus.OPEN
            ):
                return False
        self._s.conflicts[conflict.id] = conflict.model_copy(deep=True)
        self._s.conflict_claim_links.setdefault(conflict.id, [])
        return True

    async def save(self, conflict: ClaimConflict) -> None:
        self._s.conflicts[conflict.id] = conflict.model_copy(deep=True)

    async def find_by_event(
        self,
        event_id: UUID,
        limit: int | None = None,
        offset: int = 0,
        after_id: UUID | None = None,
    ) -> list[ClaimConflict]:
        items = sorted(
            (c for c in self._s.conflicts.values() if c.event_id == event_id),
            key=lambda c: (c.created_at, str(c.id)),
            reverse=True,
        )
        if after_id is not None:
            sliced = _slice_after_id(items, after_id, limit)
        elif limit is None:
            sliced = items[offset:]
        else:
            sliced = items[offset : offset + limit]
        return [self._hydrate(c) for c in sliced]

    async def close_event_conflicts_as_merged(
        self,
        event_id: UUID,
        *,
        note: str,
    ) -> list[ClaimConflict]:
        updated: list[ClaimConflict] = []
        from atlas.domain.enums import ConflictModifierReason

        for stored in self._s.conflicts.values():
            if stored.event_id != event_id or stored.status != ConflictStatus.OPEN:
                continue
            stored.status = ConflictStatus.RESOLVED
            # A merge tombstone is not a curator winner selection; do not invent
            # an arbitrary winning claim for an orphaned OPEN conflict.
            stored.winning_claim_id = None
            stored.resolved_at = stored.resolved_at or datetime.now(UTC)
            stored.last_modified_reason = ConflictModifierReason.SYSTEM_AUTO_CLOSED
            stored.last_modified_note = note[:255]
            stored.updated_at = datetime.now(UTC)
            stored.version += 1
            updated.append(self._hydrate(stored))
        return sorted(updated, key=lambda c: (c.created_at, str(c.id)), reverse=True)

    async def find_by_event_field(self, event_id: UUID, field_name: str) -> ClaimConflict | None:
        candidates = sorted(
            (
                c
                for c in self._s.conflicts.values()
                if c.event_id == event_id and c.field_name == field_name
            ),
            key=lambda c: c.created_at,
            reverse=True,
        )
        return self._hydrate(candidates[0]) if candidates else None

    async def find_open_by_event_field(
        self, event_id: UUID, field_name: str
    ) -> ClaimConflict | None:
        for c in self._s.conflicts.values():
            if (
                c.event_id == event_id
                and c.field_name == field_name
                and c.status == ConflictStatus.OPEN
            ):
                return self._hydrate(c)
        return None

    async def get_claim_ids_for_conflict(self, conflict_id: UUID) -> list[UUID]:
        return list(self._s.conflict_claim_links.get(conflict_id, []))

    async def add_claim_to_conflict(self, conflict_id: UUID, claim_id: UUID) -> None:
        if claim_id not in self._s.conflict_claim_links[conflict_id]:
            self._s.conflict_claim_links[conflict_id].append(claim_id)

    async def update_with_version_check(
        self, conflict_id: UUID, expected_version: int, updates: dict
    ) -> ClaimConflict | None:
        c = self._s.conflicts.get(conflict_id)
        if c is None or c.version != expected_version:
            return None
        for key, value in updates.items():
            # The production SQL update uses ``ConflictStatus.value`` /
            # ``ConflictModifierReason.value``; the fake stores back into a
            # Pydantic model so coerce strings to their enum to keep the
            # round-trip representation consistent.
            if key == "status" and isinstance(value, str):
                value = ConflictStatus(value)
            if key == "last_modified_reason" and isinstance(value, str):
                from atlas.domain.enums import ConflictModifierReason

                value = ConflictModifierReason(value)
            setattr(c, key, value)
        c.version = expected_version + 1
        return self._hydrate(c)

    async def find_resolved_by_winning_claim(self, claim_id: UUID) -> ClaimConflict | None:
        """Return the RESOLVED conflict whose winning_claim_id equals claim_id."""
        for c in self._s.conflicts.values():
            if c.status == ConflictStatus.RESOLVED and c.winning_claim_id == claim_id:
                return self._hydrate(c)
        return None

    async def find_resolved_by_winning_claims(
        self, claim_ids: list[UUID]
    ) -> dict[UUID, ClaimConflict]:
        """Batch variant: return {winning_claim_id: conflict} for a list of ids."""
        id_set = set(claim_ids)
        out: dict[UUID, ClaimConflict] = {}
        for c in self._s.conflicts.values():
            if c.status == ConflictStatus.RESOLVED and c.winning_claim_id in id_set:
                out[c.winning_claim_id] = self._hydrate(c)
        return out

    async def count_by_status(self, status: ConflictStatus) -> int:
        return sum(1 for c in self._s.conflicts.values() if c.status == status)

    async def count_open_conflicts_per_event(
        self,
        min_count: int = 3,
        limit: int = 50,
    ) -> list[tuple[UUID, int]]:
        if min_count < 2:
            raise ValueError(
                "min_count must be >= 2 (a single OPEN conflict is not a 'high conflict' record)"
            )
        counts: dict[UUID, int] = defaultdict(int)
        for c in self._s.conflicts.values():
            if c.status == ConflictStatus.OPEN:
                counts[c.event_id] += 1
        qualifying = [(eid, n) for eid, n in counts.items() if n >= min_count]
        # (count DESC, event_id ASC) so behaviour matches the SQL repo.  Python
        # sorts are stable, so we sort by the secondary key first, then the
        # primary key, to get the right composite ordering.
        qualifying.sort(key=lambda pair: pair[0])
        qualifying.sort(key=lambda pair: pair[1], reverse=True)
        return qualifying[:limit]


class FakeConflictActivityLogRepository:
    def __init__(self, store: _Store) -> None:
        self._s = store

    async def add(self, entry: ConflictActivityLogEntry) -> None:
        self._s.conflict_activity.append(entry)

    async def next_sequence(self, conflict_id: UUID) -> int:
        seqs = [e.sequence for e in self._s.conflict_activity if e.conflict_id == conflict_id]
        return max(seqs, default=0) + 1

    async def find_by_conflict(
        self,
        conflict_id: UUID,
        *,
        limit: int | None = None,
        after_id: UUID | None = None,
    ) -> list[ConflictActivityLogEntry]:
        items = sorted(
            (e for e in self._s.conflict_activity if e.conflict_id == conflict_id),
            key=lambda e: e.sequence,
        )
        return _slice_after_id(items, after_id, limit)

    async def find_by_event(
        self,
        event_id: UUID,
        *,
        limit: int | None = None,
        after_id: UUID | None = None,
    ) -> list[ConflictActivityLogEntry]:
        items = sorted(
            (e for e in self._s.conflict_activity if e.event_id == event_id),
            key=lambda e: (e.created_at, str(e.id)),
        )
        return _slice_after_id(items, after_id, limit)

    async def latest_for_conflict(self, conflict_id: UUID) -> ConflictActivityLogEntry | None:
        items = await self.find_by_conflict(conflict_id)
        return items[-1] if items else None


class FakeProjectionRepository:
    def __init__(self, store: _Store) -> None:
        self._s = store

    async def get(self, event_id: UUID) -> ProjectedAccidentRecord | None:
        return self._s.projections.get(event_id)

    async def upsert(self, projection: ProjectedAccidentRecord) -> None:
        self._s.projections[projection.event_id] = projection

    async def delete(self, event_id: UUID) -> None:
        self._s.projections.pop(event_id, None)

    async def find_candidates_for_event_matching(
        self, event_date: str, limit: int = 50
    ) -> list[ProjectedAccidentRecord]:
        """Return projections whose event_date is within ±1 day of the given date."""
        from datetime import date as _date

        try:
            target = _date.fromisoformat(event_date)
        except (ValueError, TypeError):
            return []
        results = []
        for proj in self._s.projections.values():
            raw_date = proj.fields.get("event_date")
            if raw_date is None:
                continue
            try:
                candidate_date = _date.fromisoformat(str(raw_date)[:10])
            except (ValueError, TypeError):
                continue
            if abs((candidate_date - target).days) <= 1:
                results.append(proj)
        return results[:limit]

    async def count_total(self) -> int:
        return len(self._s.projections)

    async def iter_all_claims(self):
        for event_id, proj in sorted(self._s.projections.items()):
            yield event_id, proj.fields


class FakeProjectionHistoryRepository:
    def __init__(self, store: _Store) -> None:
        self._s = store

    async def add(self, history: AccidentProjectionHistory) -> None:
        self._s.projection_history.append(history)

    async def find_by_event(
        self,
        event_id: UUID,
        *,
        limit: int | None = None,
        after_id: UUID | None = None,
    ) -> list[AccidentProjectionHistory]:
        items = sorted(
            (h for h in self._s.projection_history if h.accident_event_id == event_id),
            key=lambda h: (h.projection_version, str(h.id)),
        )
        return _slice_after_id(items, after_id, limit)

    async def find_by_outbox_event(self, outbox_event_id: UUID) -> AccidentProjectionHistory | None:
        return next(
            (
                h
                for h in self._s.projection_history
                if h.caused_by_outbox_event_id == outbox_event_id
            ),
            None,
        )


class FakeOutboxRepository:
    def __init__(self, store: _Store) -> None:
        self._s = store

    async def add(self, event: OutboxEvent) -> None:
        self._s.outbox.append(event)

    async def fetch_and_lock_pending(
        self, limit: int, worker_id: str, max_attempts: int = 5
    ) -> list[OutboxEvent]:
        now = datetime.now(UTC)
        eligible = [
            e
            for e in self._s.outbox
            if e.status == OutboxStatus.PENDING
            or (
                e.status == OutboxStatus.FAILED
                and e.attempt_count < max_attempts
                and (e.next_attempt_at is None or e.next_attempt_at <= now)
            )
        ][:limit]
        for e in eligible:
            e.status = OutboxStatus.PROCESSING
            e.locked_at = now
            e.locked_by = worker_id
            e.attempt_count += 1
        return eligible

    async def update_status(
        self,
        event_id: UUID,
        status: OutboxStatus,
        attempt_count: int,
        last_error: str | None = None,
        next_attempt_at: datetime | None = None,
        expected_worker_id: str | None = None,
        expected_attempt_count: int | None = None,
    ) -> bool:
        for e in self._s.outbox:
            if e.id == event_id:
                # Fenced update: only the lock holder may write a terminal
                # state. If the lock has been preempted (stale recovery +
                # another worker grabbed it) the original worker's late write
                # must be a no-op rather than overwriting the new outcome.
                if expected_worker_id is not None and expected_attempt_count is not None:
                    if (
                        e.status != OutboxStatus.PROCESSING
                        or e.locked_by != expected_worker_id
                        or e.attempt_count != expected_attempt_count
                    ):
                        return False
                e.status = status
                e.attempt_count = attempt_count
                e.last_error = last_error
                e.next_attempt_at = next_attempt_at
                e.locked_at = None
                e.locked_by = None
                if status == OutboxStatus.PROCESSED:
                    e.processed_at = datetime.now(UTC)
                return True
        return False

    async def list_recent(self, limit: int = 50) -> list[OutboxEvent]:
        return sorted(self._s.outbox, key=lambda e: e.created_at, reverse=True)[:limit]

    async def requeue_stale_locked_with_dead_letters(
        self, stale_after_minutes: int = 10, max_attempts: int = 5
    ) -> tuple[int, list[OutboxEvent]]:
        cutoff = datetime.now(UTC) - timedelta(minutes=stale_after_minutes)
        count = 0
        deadlettered: list[OutboxEvent] = []
        for e in self._s.outbox:
            if (
                e.status == OutboxStatus.PROCESSING
                and e.locked_at is not None
                and e.locked_at < cutoff
            ):
                if e.attempt_count < max_attempts:
                    # Budget remaining: requeue for a fresh attempt.
                    e.status = OutboxStatus.PENDING
                    e.locked_at = None
                    e.locked_by = None
                    e.next_attempt_at = None
                else:
                    # Exhausted: dead-letter so the row does not stay stuck.
                    e.status = OutboxStatus.DEAD_LETTER
                    e.locked_at = None
                    e.locked_by = None
                    e.next_attempt_at = None
                    e.last_error = (e.last_error or "") + " [stale lock dead-lettered]"
                    deadlettered.append(e)
                count += 1
        return count, deadlettered

    async def requeue_stale_locked(
        self, stale_after_minutes: int = 10, max_attempts: int = 5
    ) -> int:
        count, _ = await self.requeue_stale_locked_with_dead_letters(
            stale_after_minutes=stale_after_minutes,
            max_attempts=max_attempts,
        )
        return count

    async def count_by_status(self, status: OutboxStatus) -> int:
        return sum(1 for e in self._s.outbox if e.status == status)

    async def oldest_unprocessed_age_seconds(self) -> float | None:
        candidates = [
            e.created_at
            for e in self._s.outbox
            if e.status in {OutboxStatus.PENDING, OutboxStatus.PROCESSING, OutboxStatus.FAILED}
        ]
        if not candidates:
            return None
        return max(0.0, (datetime.now(UTC) - min(candidates)).total_seconds())

    async def record_worker_heartbeat(
        self, worker_id: str, *, successful_batch: bool = False
    ) -> None:
        # Fake implementation keeps only timestamps used by metrics tests.
        now = datetime.now(UTC)
        self._s.worker_heartbeats[worker_id] = {
            "last_loop_at": now,
            "last_successful_batch_at": now
            if successful_batch
            else self._s.worker_heartbeats.get(worker_id, {}).get("last_successful_batch_at"),
        }

    async def worker_heartbeat_age_seconds(self) -> float | None:
        values = [v["last_loop_at"] for v in self._s.worker_heartbeats.values()]
        if not values:
            return None
        return max(0.0, (datetime.now(UTC) - max(values)).total_seconds())

    async def worker_successful_batch_age_seconds(self) -> float | None:
        values = [
            v["last_successful_batch_at"]
            for v in self._s.worker_heartbeats.values()
            if v.get("last_successful_batch_at") is not None
        ]
        if not values:
            return None
        return max(0.0, (datetime.now(UTC) - max(values)).total_seconds())


class FakeArchiveManifestRepository:
    def __init__(self, store: _Store) -> None:
        self._s = store

    async def add(self, manifest: ArchiveManifest) -> None:
        self._s.archive_manifests.append(manifest)


class FakeEventIdentityIndexRepository:
    """In-memory identity index - written synchronously within the ingestion call.

    No advisory lock needed in the fake because asyncio tests run on a single
    event-loop thread; coroutines never execute truly concurrently.

    The ``upsert`` method merges ``source_record_ids`` (union) and preserves
    existing non-None field values when the new entry has None for that field,
    mirroring the ``COALESCE`` logic in the SQL implementation.
    """

    def __init__(self, store: _Store) -> None:
        self._s = store

    async def upsert(self, entry: EventIdentityIndex) -> None:
        existing = self._s.identity_index.get(entry.event_id)
        if existing is None:
            self._s.identity_index[entry.event_id] = entry
            return
        # Merge: prefer new non-None values; union source_record_ids and
        # registration_norms so all known aliases stay searchable.
        merged_ids = list({*existing.source_record_ids, *entry.source_record_ids})
        merged_reg_norms = _cap_registration_norms(
            [*existing.registration_norms, *entry.registration_norms]
        )
        updated = existing.model_copy(
            update={
                "event_date_norm": entry.event_date_norm or existing.event_date_norm,
                "registration_norm": entry.registration_norm or existing.registration_norm,
                "operator_norm": entry.operator_norm or existing.operator_norm,
                "location_norm": entry.location_norm or existing.location_norm,
                "aircraft_type_norm": entry.aircraft_type_norm or existing.aircraft_type_norm,
                "source_record_ids": merged_ids,
                "registration_norms": merged_reg_norms,
                "updated_at": entry.updated_at,
            }
        )
        self._s.identity_index[entry.event_id] = updated

    async def enrich_identity_index_from_alias(self, entry: EventIdentityIndex) -> None:
        existing = self._s.identity_index.get(entry.event_id)
        if existing is None:
            self._s.identity_index[entry.event_id] = entry
            return
        merged_reg_norms = _cap_registration_norms(
            list(
                dict.fromkeys(
                    existing.registration_norms
                    + entry.registration_norms
                    + ([entry.registration_norm] if entry.registration_norm else [])
                )
            )
        )
        merged_ids = list(dict.fromkeys(existing.source_record_ids + entry.source_record_ids))
        updated = existing.model_copy(
            update={
                "event_date_norm": existing.event_date_norm or entry.event_date_norm,
                "registration_norm": existing.registration_norm or entry.registration_norm,
                "operator_norm": existing.operator_norm or entry.operator_norm,
                "location_norm": existing.location_norm or entry.location_norm,
                "aircraft_type_norm": (existing.aircraft_type_norm or entry.aircraft_type_norm),
                "source_record_ids": merged_ids,
                "registration_norms": merged_reg_norms,
                "updated_at": entry.updated_at,
            }
        )
        self._s.identity_index[entry.event_id] = updated

    async def merge_identity_index(self, source_event_id: UUID, target_event_id: UUID) -> None:
        src = self._s.identity_index.get(source_event_id)
        if src is None:
            import logging

            logging.getLogger(__name__).warning(
                "merge_identity_index (fake): source identity row missing for %s -> %s",
                source_event_id,
                target_event_id,
            )
            return

        tgt = self._s.identity_index.get(target_event_id)
        source_regs = src.registration_norms + (
            [src.registration_norm] if src.registration_norm else []
        )
        if tgt is None:
            self._s.identity_index[target_event_id] = src.model_copy(
                update={
                    "event_id": target_event_id,
                    "registration_norms": _cap_registration_norms(list(dict.fromkeys(source_regs))),
                }
            )
            return

        updated = tgt.model_copy(
            update={
                "event_date_norm": tgt.event_date_norm or src.event_date_norm,
                "registration_norm": tgt.registration_norm or src.registration_norm,
                "operator_norm": tgt.operator_norm or src.operator_norm,
                "location_norm": tgt.location_norm or src.location_norm,
                "aircraft_type_norm": tgt.aircraft_type_norm or src.aircraft_type_norm,
                "source_record_ids": list(
                    dict.fromkeys(tgt.source_record_ids + src.source_record_ids)
                ),
                "registration_norms": _cap_registration_norms(
                    list(dict.fromkeys(tgt.registration_norms + source_regs))
                ),
                "updated_at": src.updated_at,
            }
        )
        self._s.identity_index[target_event_id] = updated

    async def find_candidates(
        self, event_date_norm: str, limit: int = 50
    ) -> list[EventIdentityIndex]:
        from datetime import date as _date
        from datetime import timedelta as _td

        try:
            centre = _date.fromisoformat(event_date_norm)
        except (ValueError, TypeError):
            return []
        lo = str(centre - _td(days=1))
        hi = str(centre + _td(days=1))
        results = []
        for entry in self._s.identity_index.values():
            if entry.event_date_norm is None:
                continue
            if not (lo <= entry.event_date_norm <= hi):
                continue
            # Include merged events as historical identity aliases.
            # The ingestion use case canonicalizes the candidate before writing.
            if entry.event_id not in self._s.events:
                continue
            results.append(entry)
        # Newest first (mirrors SQL ORDER BY updated_at DESC).
        results.sort(key=lambda e: e.updated_at, reverse=True)
        return results[:limit]

    async def lock_for_identity_resolution(
        self, event_date_norm: str, registration_norm: str | None
    ) -> None:
        # No-op: single-threaded asyncio tests never race.
        return None

    async def find_by_registration(
        self,
        registration_norm: str,
        event_date_norm: str | None = None,
    ) -> list:
        """Find entries by primary or historical registration alias."""
        registration_norm = _normalise_registration_lookup(registration_norm)
        from datetime import date as _date
        from datetime import timedelta as _td

        lo = hi = None
        if event_date_norm:
            try:
                centre = _date.fromisoformat(event_date_norm)
                lo = str(centre - _td(days=1))
                hi = str(centre + _td(days=1))
            except (ValueError, TypeError):
                pass
        results = []
        for entry in self._s.identity_index.values():
            event = self._s.events.get(entry.event_id)
            if event is None:
                continue
            primary_match = entry.registration_norm == registration_norm
            alias_match = registration_norm in entry.registration_norms
            if not (primary_match or alias_match):
                continue
            if lo and hi:
                if not entry.event_date_norm or not (lo <= entry.event_date_norm <= hi):
                    continue
            results.append(entry)
        results.sort(
            key=lambda e: (
                self._s.events[e.event_id].is_merged,
                -e.updated_at.timestamp(),
                str(e.event_id),
            )
        )
        return results


class FakePendingDuplicateReviewRepository:
    def __init__(self, store: _Store) -> None:
        self._s = store

    async def add(self, review: PendingDuplicateReview) -> PendingDuplicateReview | None:
        existing = await self.find_pending_pair(review.event_id_a, review.event_id_b)
        if existing is not None:
            return existing
        self._s.duplicate_reviews[review.id] = review
        return review

    async def get(self, id: UUID) -> PendingDuplicateReview | None:
        return self._s.duplicate_reviews.get(id)

    async def list_pending(
        self, *, limit: int, after_id: UUID | None = None
    ) -> list[PendingDuplicateReview]:
        pending = [
            r
            for r in self._s.duplicate_reviews.values()
            if r.status == DuplicateReviewStatus.PENDING
        ]
        pending.sort(key=lambda r: (r.created_at, r.id), reverse=True)
        if after_id is not None:
            cursor = next((r for r in pending if r.id == after_id), None)
            if cursor is not None:
                cursor_key = (cursor.created_at, cursor.id)
                pending = [r for r in pending if (r.created_at, r.id) < cursor_key]
        return pending[:limit]

    async def find_pending_for_event(self, event_id: UUID) -> list[PendingDuplicateReview]:
        return [
            r
            for r in self._s.duplicate_reviews.values()
            if r.status == DuplicateReviewStatus.PENDING
            and (r.event_id_a == event_id or r.event_id_b == event_id)
        ]

    def _matches_pair(
        self, review: PendingDuplicateReview, event_id_a: UUID, event_id_b: UUID
    ) -> bool:
        return (review.event_id_a == event_id_a and review.event_id_b == event_id_b) or (
            review.event_id_a == event_id_b and review.event_id_b == event_id_a
        )

    async def find_pending_pair(
        self, event_id_a: UUID, event_id_b: UUID
    ) -> PendingDuplicateReview | None:
        matches = [
            r
            for r in self._s.duplicate_reviews.values()
            if r.status == DuplicateReviewStatus.PENDING
            and self._matches_pair(r, event_id_a, event_id_b)
        ]
        matches.sort(key=lambda r: (r.created_at, r.id), reverse=True)
        return matches[0] if matches else None

    async def find_existing_pair(
        self, event_id_a: UUID, event_id_b: UUID
    ) -> PendingDuplicateReview | None:
        matches = [
            r
            for r in self._s.duplicate_reviews.values()
            if self._matches_pair(r, event_id_a, event_id_b)
        ]
        matches.sort(
            key=lambda r: (r.status == DuplicateReviewStatus.PENDING, r.created_at, r.id),
            reverse=True,
        )
        return matches[0] if matches else None

    async def update_status(
        self,
        id: UUID,
        status: DuplicateReviewStatus,
        resolved_by: UUID | None = None,
        resolution_note: str | None = None,
    ) -> PendingDuplicateReview | None:
        r = self._s.duplicate_reviews.get(id)
        if r is None:
            return None
        r.status = status
        r.resolved_by = resolved_by
        r.resolution_note = resolution_note
        if status != DuplicateReviewStatus.PENDING:
            r.resolved_at = datetime.now(UTC)
        return r


# ── Orion Fake Repositories ───────────────────────────────────────────────────
