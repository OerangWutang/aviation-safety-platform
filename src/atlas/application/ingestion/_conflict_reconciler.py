"""ConflictReconciler - reconcile conflicts after claim writes."""

from __future__ import annotations

import logging
from uuid import UUID, uuid4

from atlas.application.unit_of_work import UnitOfWork
from atlas.domain.entities import Claim, ClaimConflict, ConflictActivityLogEntry
from atlas.domain.enums import ConflictModifierReason, ConflictStatus, ModifierType
from atlas.domain.exceptions import ConflictReconciliationError
from atlas.domain.services.conflict_detector import (
    ConflictDetector,
    normalize_value,
    unique_normalised_values,
)
from atlas.domain.services.conflict_utils import latest_resolved_conflicts_by_field
from atlas.domain.utils import utc_now

logger = logging.getLogger(__name__)

_MAX_VERSION_RETRY = 5


class ConflictReconciler:
    """Reconcile open/resolved conflicts after claims are written.

    Responsibilities
    ----------------
    * Auto-resolve stale OPEN conflicts when source-record supersession removes
      the active disagreement.
    * Reconcile RESOLVED conflicts whose winning claim was superseded: update
      the winner if all remaining active claims agree, or reopen if they disagree.
    * Detect new conflicts in the current active claim set.
    * Open new conflicts or merge evidence into existing ones.
    """

    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    async def reconcile_superseded_winners(
        self,
        resolved_conflicts_and_replacements: list[tuple[ClaimConflict, Claim]],
        ingestion_run_id: UUID,
    ) -> None:
        """Fix RESOLVED conflicts whose winning claim was just superseded."""
        for resolved_conflict, replacement_claim in resolved_conflicts_and_replacements:
            await self._reconcile_resolved_winner(
                resolved_conflict, replacement_claim, ingestion_run_id
            )

    async def auto_resolve_stale_open_conflicts(
        self,
        event_id: UUID,
        affected_fields: set[str],
        ingestion_run_id: UUID,
    ) -> None:
        """Close OPEN conflicts that no longer exist in active evidence."""
        for field_name in affected_fields:
            await self._auto_resolve_stale_open_conflict(event_id, field_name, ingestion_run_id)

    async def detect_and_apply_new_conflicts(
        self,
        event_id: UUID,
        modifier_id: UUID,
        modifier_type: ModifierType = ModifierType.INGESTION,
    ) -> None:
        """Detect conflicts in the current active claim set and persist them.

        ``modifier_id`` is recorded in activity-log rows.  For ingestion paths
        this is the ``ingestion_run_id``; for the merge path it is the curator
        ``user_id`` who triggered the merge.

        ``modifier_type`` defaults to ``INGESTION``; pass ``USER`` when this
        method is called from a curator-driven path (e.g. event merge).

        Batching strategy
        -----------------
        Rather than issuing one ``find_open_by_event_field`` + one
        ``find_by_event_field`` per detected conflict (N x 2 round-trips), we
        fetch **all** existing conflicts for the event once and build in-memory
        maps.  Only the write paths (``try_add_open``, ``add_claim_to_conflict``,
        ``update_with_version_check``) touch the DB per-conflict.
        """
        active_claims = await self._uow.claims.find_active_by_event(event_id)
        detected = ConflictDetector().detect(active_claims)

        if not detected:
            return

        # Fetch all existing conflicts once and partition by field.
        existing_conflicts = await self._uow.conflicts.find_by_event(event_id)
        open_by_field: dict[str, ClaimConflict] = {
            c.field_name: c for c in existing_conflicts if c.status == ConflictStatus.OPEN
        }
        resolved_by_field = latest_resolved_conflicts_by_field(existing_conflicts)

        for potential in detected:
            field_name = potential.field_name

            # Case 1: existing OPEN conflict for this field.
            if field_name in open_by_field:
                await self._merge_evidence_into_open(
                    open_by_field[field_name],
                    potential.claim_ids,
                    modifier_id,
                    log_reason="New evidence added to existing conflict",
                    modifier_type=modifier_type,
                )
                continue

            # Case 2: previously RESOLVED conflict that the new evidence contradicts.
            if field_name in resolved_by_field:
                existing_any = resolved_by_field[field_name]
                winner = (
                    await self._uow.claims.get(existing_any.winning_claim_id)
                    if existing_any.winning_claim_id
                    else None
                )
                if winner:
                    winner_value = normalize_value(winner.field_value)
                    field_claims = [c for c in active_claims if c.field_name == field_name]
                    if any(normalize_value(c.field_value) != winner_value for c in field_claims):
                        await self._reopen_resolved_for_evidence(
                            existing_any,
                            [c.id for c in field_claims],
                            modifier_id,
                            modifier_type=modifier_type,
                        )
                        continue

            # Case 3: brand-new conflict.
            inserted = await self._uow.conflicts.try_add_open(potential)
            if not inserted:
                # Concurrent writer beat us; reload and merge into it.
                reloaded_open = await self._uow.conflicts.find_open_by_event_field(
                    event_id, field_name
                )
                if reloaded_open is not None:
                    await self._merge_evidence_into_open(
                        reloaded_open,
                        potential.claim_ids,
                        modifier_id,
                        log_reason="Merged into concurrent conflict",
                        modifier_type=modifier_type,
                    )
                continue

            for claim_id in potential.claim_ids:
                await self._uow.conflicts.add_claim_to_conflict(potential.id, claim_id)
            await self._uow.conflict_activity.add(
                ConflictActivityLogEntry(
                    id=uuid4(),
                    conflict_id=potential.id,
                    event_id=potential.event_id,
                    sequence=await self._uow.conflict_activity.next_sequence(potential.id),
                    from_status=None,
                    to_status=ConflictStatus.OPEN,
                    modifier_type=modifier_type,
                    modifier_id=modifier_id,
                    reason="Initial conflict detected",
                    version_at_moment=potential.version,
                )
            )

    # ── Internal helpers ──────────────────────────────────────────────────────

    async def _reconcile_resolved_winner(
        self,
        resolved_conflict: ClaimConflict,
        replacement_claim: Claim,
        ingestion_run_id: UUID,
    ) -> None:
        field_name = resolved_conflict.field_name
        event_id = resolved_conflict.event_id
        active_field_claims = await self._uow.claims.find_active_by_event_field(
            event_id, field_name
        )
        if not active_field_claims:
            return

        unique_values = unique_normalised_values(active_field_claims)

        if len(unique_values) == 1:
            active_ids = {c.id for c in active_field_claims}
            if replacement_claim.field_name == field_name and replacement_claim.id in active_ids:
                new_winner = replacement_claim
            else:
                new_winner = active_field_claims[0]

            now = utc_now()
            for _ in range(_MAX_VERSION_RETRY):
                # Fence the winner update before adding the replacement claim
                # link. If a concurrent transaction modifies the conflict, the
                # version check fails without leaving a partial evidence link.
                updated = await self._uow.conflicts.update_with_version_check(
                    conflict_id=resolved_conflict.id,
                    expected_version=resolved_conflict.version,
                    updates={"winning_claim_id": new_winner.id, "updated_at": now},
                )
                if updated is not None:
                    existing_ids = set(
                        await self._uow.conflicts.get_claim_ids_for_conflict(resolved_conflict.id)
                    )
                    if new_winner.id not in existing_ids:
                        await self._uow.conflicts.add_claim_to_conflict(
                            resolved_conflict.id, new_winner.id
                        )
                    await self._uow.conflict_activity.add(
                        ConflictActivityLogEntry(
                            id=uuid4(),
                            conflict_id=resolved_conflict.id,
                            event_id=resolved_conflict.event_id,
                            sequence=await self._uow.conflict_activity.next_sequence(
                                resolved_conflict.id
                            ),
                            from_status=ConflictStatus.RESOLVED,
                            to_status=ConflictStatus.RESOLVED,
                            modifier_type=ModifierType.INGESTION,
                            modifier_id=ingestion_run_id,
                            reason=(
                                "Winning claim superseded by source-record correction; "
                                "winner updated to equivalent replacement"
                            ),
                            version_at_moment=updated.version,
                        )
                    )
                    return
                reloaded = await self._uow.conflicts.get(resolved_conflict.id)
                if reloaded is None or reloaded.status != ConflictStatus.RESOLVED:
                    return
                resolved_conflict = reloaded
        else:
            await self._reopen_resolved_for_evidence(
                resolved_conflict,
                [c.id for c in active_field_claims],
                ingestion_run_id,
            )

    async def _auto_resolve_stale_open_conflict(
        self,
        event_id: UUID,
        field_name: str,
        ingestion_run_id: UUID,
    ) -> None:
        existing_open = await self._uow.conflicts.find_open_by_event_field(event_id, field_name)
        if existing_open is None:
            return

        for _ in range(_MAX_VERSION_RETRY):
            active_field_claims = await self._uow.claims.find_active_by_event_field(
                event_id, field_name
            )
            if not active_field_claims:
                return
            if len(unique_normalised_values(active_field_claims)) > 1:
                return

            winning_claim = active_field_claims[0]
            now = utc_now()
            # Fence the state transition before adding the winning claim link.
            # Failed optimistic updates must not leave partial evidence rows.
            updated = await self._uow.conflicts.update_with_version_check(
                conflict_id=existing_open.id,
                expected_version=existing_open.version,
                updates={
                    "status": ConflictStatus.RESOLVED.value,
                    "winning_claim_id": winning_claim.id,
                    "resolved_by": None,
                    "resolved_at": now,
                    "last_modified_reason": ConflictModifierReason.SYSTEM_AUTO_CLOSED.value,
                    "last_modified_note": (
                        "Active evidence now agrees after source-record supersession"
                    )[:255],
                    "updated_at": now,
                },
            )
            if updated is not None:
                existing_ids = set(
                    await self._uow.conflicts.get_claim_ids_for_conflict(existing_open.id)
                )
                if winning_claim.id not in existing_ids:
                    await self._uow.conflicts.add_claim_to_conflict(
                        existing_open.id, winning_claim.id
                    )
                await self._uow.conflict_activity.add(
                    ConflictActivityLogEntry(
                        id=uuid4(),
                        conflict_id=existing_open.id,
                        event_id=existing_open.event_id,
                        sequence=await self._uow.conflict_activity.next_sequence(existing_open.id),
                        from_status=ConflictStatus.OPEN,
                        to_status=ConflictStatus.RESOLVED,
                        modifier_type=ModifierType.INGESTION,
                        modifier_id=ingestion_run_id,
                        reason=(
                            "Auto-closed because source-record supersession removed "
                            "the active disagreement"
                        ),
                        version_at_moment=updated.version,
                    )
                )
                return
            reloaded = await self._uow.conflicts.get(existing_open.id)
            if reloaded is None or reloaded.status != ConflictStatus.OPEN:
                return
            existing_open = reloaded

        raise ConflictReconciliationError(
            conflict_id=existing_open.id,
            operation="auto_resolve_stale_open",
            retries=_MAX_VERSION_RETRY,
        )

    async def _merge_evidence_into_open(
        self,
        existing_open: ClaimConflict,
        new_claim_ids: list[UUID],
        modifier_id: UUID,
        log_reason: str,
        modifier_type: ModifierType = ModifierType.INGESTION,
    ) -> None:
        conflict_id = existing_open.id
        for _ in range(_MAX_VERSION_RETRY):
            now = utc_now()
            # Fence the conflict-row update before adding evidence links. If a
            # concurrent transaction has already resolved or modified this row,
            # the version check fails and we retry without leaving partial
            # conflict_claim rows behind.
            updated = await self._uow.conflicts.update_with_version_check(
                conflict_id=conflict_id,
                expected_version=existing_open.version,
                updates={
                    "last_modified_reason": ConflictModifierReason.NEW_EVIDENCE.value,
                    "updated_at": now,
                },
            )
            if updated is not None:
                existing_ids = set(
                    await self._uow.conflicts.get_claim_ids_for_conflict(conflict_id)
                )
                for claim_id in new_claim_ids:
                    if claim_id not in existing_ids:
                        await self._uow.conflicts.add_claim_to_conflict(conflict_id, claim_id)
                await self._uow.conflict_activity.add(
                    ConflictActivityLogEntry(
                        id=uuid4(),
                        conflict_id=conflict_id,
                        event_id=existing_open.event_id,
                        sequence=await self._uow.conflict_activity.next_sequence(conflict_id),
                        from_status=ConflictStatus.OPEN,
                        to_status=ConflictStatus.OPEN,
                        modifier_type=modifier_type,
                        modifier_id=modifier_id,
                        reason=log_reason,
                        version_at_moment=updated.version,
                    )
                )
                return
            reloaded = await self._uow.conflicts.get(conflict_id)
            if reloaded is None:
                return
            if reloaded.status != ConflictStatus.OPEN:
                logger.info("Conflict %s resolved during evidence merge; deferring", conflict_id)
                return
            existing_open = reloaded

        raise ConflictReconciliationError(
            conflict_id=conflict_id,
            operation="merge_evidence_into_open",
            retries=_MAX_VERSION_RETRY,
        )

    async def _reopen_resolved_for_evidence(
        self,
        existing_resolved: ClaimConflict,
        contradicting_claim_ids: list[UUID],
        modifier_id: UUID,
        modifier_type: ModifierType = ModifierType.INGESTION,
    ) -> None:
        conflict_id = existing_resolved.id
        for _ in range(_MAX_VERSION_RETRY):
            now = utc_now()
            # Lock/fence the state transition first, then add evidence links.
            # Preserve the resolved-state cleanup fields so an OPEN conflict
            # never carries stale winner/resolution metadata.
            updated = await self._uow.conflicts.update_with_version_check(
                conflict_id=conflict_id,
                expected_version=existing_resolved.version,
                updates={
                    "status": ConflictStatus.OPEN.value,
                    "winning_claim_id": None,
                    "resolved_by": None,
                    "resolved_at": None,
                    "last_modified_reason": ConflictModifierReason.NEW_EVIDENCE.value,
                    "last_modified_note": "New evidence contradicts resolved value"[:255],
                    "updated_at": now,
                },
            )
            if updated is not None:
                existing_ids = set(
                    await self._uow.conflicts.get_claim_ids_for_conflict(conflict_id)
                )
                for claim_id in contradicting_claim_ids:
                    if claim_id not in existing_ids:
                        await self._uow.conflicts.add_claim_to_conflict(conflict_id, claim_id)
                await self._uow.conflict_activity.add(
                    ConflictActivityLogEntry(
                        id=uuid4(),
                        conflict_id=conflict_id,
                        event_id=existing_resolved.event_id,
                        sequence=await self._uow.conflict_activity.next_sequence(conflict_id),
                        from_status=ConflictStatus.RESOLVED,
                        to_status=ConflictStatus.OPEN,
                        modifier_type=modifier_type,
                        modifier_id=modifier_id,
                        reason="Reopened due to contradictory new evidence",
                        version_at_moment=updated.version,
                    )
                )
                return
            reloaded = await self._uow.conflicts.get(conflict_id)
            if reloaded is None:
                return
            if reloaded.status == ConflictStatus.OPEN:
                await self._merge_evidence_into_open(
                    reloaded,
                    contradicting_claim_ids,
                    modifier_id,
                    log_reason="Contradictory evidence merged into already-reopened conflict",
                    modifier_type=modifier_type,
                )
                return
            existing_resolved = reloaded

        raise ConflictReconciliationError(
            conflict_id=conflict_id,
            operation="reopen_resolved_for_evidence",
            retries=_MAX_VERSION_RETRY,
        )
