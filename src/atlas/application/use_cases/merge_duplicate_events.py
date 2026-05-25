"""MergeDuplicateEvents: audited, atomic merge of two accident events.

Copy-then-supersede pattern
-----------------------------
We create new Claim rows on the target (same field/source/snapshot, new id)
and mark the source claims SUPERSEDED. The source event is then marked
merged_into_event_id = target. This avoids FK constraint complexity from
reassigning claims.event_id while preserving the full audit trail on both
events.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from uuid import UUID, uuid4

from atlas.application.ingestion._conflict_reconciler import ConflictReconciler
from atlas.application.unit_of_work import UnitOfWork
from atlas.domain.entities import (
    AccidentEvent,
    Claim,
    ClaimHistory,
    ConflictActivityLogEntry,
    OutboxEvent,
)
from atlas.domain.enums import (
    ClaimType,
    ConflictStatus,
    DuplicateReviewStatus,
    ModifierType,
)
from atlas.domain.exceptions import (
    CannotMergeIntoSelfError,
    EventAlreadyMergedError,
    EventNotFoundError,
)
from atlas.domain.utils import utc_now

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MergeResult:
    target_event_id: UUID
    source_event_id: UUID
    claims_transferred: int
    review_id: UUID | None = None


class MergeDuplicateEvents:
    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    async def execute(
        self,
        source_event_id: UUID,
        target_event_id: UUID,
        resolved_by: UUID,
        note: str = "",
        review_id: UUID | None = None,
    ) -> MergeResult:
        if source_event_id == target_event_id:
            raise CannotMergeIntoSelfError(f"Cannot merge event {source_event_id} into itself")

        # Lightweight existence checks give callers precise 404s.  They do not
        # provide serialization; that happens immediately below with row-level
        # locks and the conditional ``try_atomic_merge`` update.
        source = await self._uow.events.get(source_event_id)
        if source is None:
            raise EventNotFoundError(f"Source event {source_event_id} not found")

        target = await self._uow.events.get(target_event_id)
        if target is None:
            raise EventNotFoundError(f"Target event {target_event_id} not found")

        # Lock both rows before claiming/copying evidence.  Ingestion also
        # locks the event row before writing claims, so this prevents the race
        # where ingestion reads ``is_merged == False``, merge copies old claims,
        # and ingestion later writes new orphan claims to the absorbed event.
        # Sort UUIDs to avoid merge(A->B) vs merge(B->A)-style deadlocks.
        locked: dict[UUID, AccidentEvent] = {}
        for lock_id in sorted((source_event_id, target_event_id), key=str):
            locked_event = await self._uow.events.lock_for_update(lock_id)
            if locked_event is None:
                if lock_id == source_event_id:
                    raise EventNotFoundError(f"Source event {source_event_id} not found")
                raise EventNotFoundError(f"Target event {target_event_id} not found")
            locked[lock_id] = locked_event

        source = locked[source_event_id]
        target = locked[target_event_id]
        if source.is_merged:
            raise EventAlreadyMergedError(
                f"Source event {source_event_id} is already merged into {source.merged_into_event_id}"
            )
        if target.is_merged:
            raise EventAlreadyMergedError(
                f"Target event {target_event_id} is already merged into {target.merged_into_event_id}"
            )

        logger.info("Merging %s -> %s by=%s", source_event_id, target_event_id, resolved_by)

        # Atomically claim the merge via conditional UPDATE+RETURNING.
        # Only the first concurrent request wins; the loser gets EventAlreadyMergedError.
        claimed = await self._uow.events.try_atomic_merge(source_event_id, target_event_id)
        if not claimed:
            raise EventAlreadyMergedError(
                f"Source event {source_event_id} was already merged (concurrent request won the race)"
            )

        # 0. Union source identity aliases into the canonical target identity row.
        #    This runs while both events are locked and before claim/conflict
        #    reconciliation, so future ingestions can find source aliases on the
        #    surviving event without relying on the merge-pointer chain.
        await self._uow.identity_index.merge_identity_index(
            source_event_id=source_event_id,
            target_event_id=target_event_id,
        )

        # 1. Transfer active claims from source to target.
        #    Preserve the original claim_type and created_by - merge is a transfer,
        #    not a downgrade. The merge actor is recorded in ClaimHistory.
        source_active = await self._uow.claims.find_active_by_event(source_event_id)
        old_to_new: dict[UUID, UUID] = {}
        transferred = 0
        for old in source_active:
            new_claim = Claim(
                id=uuid4(),
                event_id=target_event_id,
                source_id=old.source_id,
                raw_snapshot_id=old.raw_snapshot_id,
                field_name=old.field_name,
                field_value=old.field_value,
                claim_type=old.claim_type,
                created_by=old.created_by,
                created_at=old.created_at,
            )
            await self._uow.claims.add(new_claim)
            old_to_new[old.id] = new_claim.id
            # Flush so the new claim row exists before its history row
            # references it.  See _claim_writer.py for the full
            # rationale: no ORM relationship() + self-referential FK on
            # claims defeats SQLAlchemy's automatic insert ordering.
            await self._uow.flush()
            await self._uow.claim_history.add(
                ClaimHistory(
                    id=uuid4(),
                    claim_id=new_claim.id,
                    event_id=target_event_id,
                    from_value=None,
                    to_value=old.field_value,
                    from_claim_type=None,
                    to_claim_type=old.claim_type,
                    action="merged",
                    reason=(
                        f"Transferred from event {source_event_id} during merge"
                        f" by {resolved_by}" + (f": {note}" if note else "")
                    ),
                    modifier_type=ModifierType.USER,
                    modifier_id=resolved_by,
                )
            )
            transferred += 1

        # 2. Supersede each source claim by its corresponding new target claim.
        for old in source_active:
            original_claim_type = old.claim_type
            await self._uow.claims.bulk_supersede([old.id], by_claim_id=old_to_new[old.id])
            await self._uow.claim_history.add(
                ClaimHistory(
                    id=uuid4(),
                    claim_id=old.id,
                    event_id=old.event_id,
                    from_value=old.field_value,
                    to_value=old.field_value,
                    from_claim_type=original_claim_type,
                    to_claim_type=ClaimType.SUPERSEDED,
                    action="superseded",
                    reason=f"Superseded by merge into {target_event_id}"
                    + (f": {note}" if note else ""),
                    modifier_type=ModifierType.USER,
                    modifier_id=resolved_by,
                )
            )

        # 3. Source event is already marked merged by try_atomic_merge.

        # 4. Tombstone source-event conflicts. Source claims have been copied to
        #    the target and superseded on the absorbed event, so any conflicts
        #    that still point at ``source_event_id`` are no longer actionable.
        #    Leaving them OPEN would leak phantom conflicts into curator queues
        #    and stale dispute markers into maintenance rebuilds.
        merge_conflict_note = f"Event merged into {target_event_id} by {resolved_by}" + (
            f": {note}" if note else ""
        )
        source_conflicts = await self._uow.conflicts.close_event_conflicts_as_merged(
            source_event_id,
            note=merge_conflict_note,
        )
        for conflict in source_conflicts:
            await self._uow.conflict_activity.add(
                ConflictActivityLogEntry(
                    id=uuid4(),
                    conflict_id=conflict.id,
                    event_id=conflict.event_id,
                    sequence=await self._uow.conflict_activity.next_sequence(conflict.id),
                    from_status=ConflictStatus.OPEN,
                    to_status=ConflictStatus.RESOLVED,
                    modifier_type=ModifierType.SYSTEM,
                    modifier_id=None,
                    reason=merge_conflict_note,
                    version_at_moment=conflict.version,
                    claims_snapshot={
                        "event_merged_into": str(target_event_id),
                        "previous_winning_claim_id": None,
                    },
                    created_at=utc_now(),
                )
            )

        # 5. Run conflict detection via ConflictReconciler so that resolved
        #    conflicts are correctly reopened when transferred claims contradict
        #    them - the previous private _detect_conflicts did not handle that case.
        #    Pass ModifierType.USER so activity-log rows correctly record this as
        #    a curator action rather than an ingestion event.
        await ConflictReconciler(self._uow).detect_and_apply_new_conflicts(
            target_event_id, resolved_by, modifier_type=ModifierType.USER
        )

        # 6. Queue re-projection for target.
        await self._uow.outbox.add(
            OutboxEvent(
                id=uuid4(),
                event_type="CLAIMS_UPDATED",
                aggregate_id=target_event_id,
                payload={"event_id": str(target_event_id), "merged_from": str(source_event_id)},
            )
        )

        # 7. Remove the absorbed event's old read model in the same transaction.
        #    Public reads canonicalize through the merge pointer, but direct/admin
        #    projection lookups should not see stale pre-merge fields. A later
        #    full rebuild will write an explicit tombstone projection for merged
        #    events.
        await self._uow.projections.delete(source_event_id)

        # 8. Close any pending duplicate review for this pair.
        closed_review_id = await self._close_review(
            source_event_id, target_event_id, review_id, resolved_by, note
        )

        await self._uow.commit()
        logger.info(
            "Merge complete: %s -> %s (%d claims, review=%s)",
            source_event_id,
            target_event_id,
            transferred,
            closed_review_id,
        )
        return MergeResult(
            target_event_id=target_event_id,
            source_event_id=source_event_id,
            claims_transferred=transferred,
            review_id=closed_review_id,
        )

    async def _close_review(
        self,
        source_id: UUID,
        target_id: UUID,
        review_id: UUID | None,
        resolved_by: UUID,
        note: str,
    ) -> UUID | None:
        note_text = (note or "Merged via admin action")[:500]
        if review_id is not None:
            await self._uow.duplicate_reviews.update_status(
                review_id, DuplicateReviewStatus.MERGED, resolved_by, note_text
            )
            return review_id
        existing = await self._uow.duplicate_reviews.find_existing_pair(source_id, target_id)
        if existing and existing.status == DuplicateReviewStatus.PENDING:
            await self._uow.duplicate_reviews.update_status(
                existing.id, DuplicateReviewStatus.MERGED, resolved_by, note_text
            )
            return existing.id
        return None
