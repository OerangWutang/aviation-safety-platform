"""Manual reopen of a previously resolved conflict.

This is the curator-driven counterpart to
``IngestSourceData``'s automatic reopen-on-contradiction path.

Behavior:
    1. Verify the conflict exists and is RESOLVED.
    2. Optimistic version check.
    3. Un-supersede only claims in this conflict that were superseded by the
       previous winner.  Restore their pre-superseded claim type from history
       so CONFIRMED/manual claims are not downgraded to RAW.
    4. Update conflict status to OPEN with USER_REOPENED reason.
    5. Append a ConflictActivityLogEntry.
    6. Reproject the event.

The previous winner (whether a raw claim or a manual override) is *not*
itself superseded - the user can choose to re-resolve it as the winner again
if they wish, or pick another claim. This keeps the operation reversible
without losing audit data.
"""

from __future__ import annotations

import logging
from uuid import UUID, uuid4

from atlas.application.dto import ProjectionDTO
from atlas.application.unit_of_work import UnitOfWork
from atlas.application.use_cases._conflict_modified import (
    build_conflict_modified_error_after_failed_update,
    build_conflict_modified_error_from_known,
)
from atlas.application.use_cases.reproject_event import ReProjectEvent
from atlas.domain.entities import Claim, ClaimHistory, ConflictActivityLogEntry
from atlas.domain.enums import ClaimType, ConflictModifierReason, ConflictStatus, ModifierType
from atlas.domain.exceptions import (
    ConflictNotFoundError,
    DomainValidationError,
)
from atlas.domain.utils import utc_now

logger = logging.getLogger(__name__)


class ReopenConflict:
    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    async def execute(
        self,
        conflict_id: UUID,
        expected_version: int,
        current_user_id: UUID,
        reason: str = "",
    ) -> tuple[object, ProjectionDTO]:
        if current_user_id is None:
            raise DomainValidationError("current_user_id is required")

        conflict = await self._uow.conflicts.get(conflict_id)
        if conflict is None:
            raise ConflictNotFoundError(f"Conflict {conflict_id} not found")
        if conflict.status != ConflictStatus.RESOLVED:
            # Only resolved conflicts can be reopened. Surface as 422 via the
            # global exception handler - the request payload was syntactically
            # valid but semantically incompatible with the conflict state.
            raise DomainValidationError(
                f"Conflict {conflict_id} is not resolved; only resolved conflicts can be reopened"
            )

        if conflict.version != expected_version:
            raise await build_conflict_modified_error_from_known(self._uow, conflict)

        previous_winner_id = conflict.winning_claim_id

        # Un-supersede only claims that belong to this exact conflict.
        # A winning claim can theoretically supersede claims in more than one
        # context over time; reopening this conflict must not revive unrelated
        # claims.  ``bulk_unsupersede`` restores each claim's pre-superseded
        # claim_type from ClaimHistory when available instead of downgrading
        # everything to RAW.
        unsuperseded: list[Claim] = []
        if previous_winner_id is not None:
            conflict_claim_ids = set(
                await self._uow.conflicts.get_claim_ids_for_conflict(conflict_id)
            ) or set(conflict.claim_ids)
            superseded = await self._uow.claims.find_superseded_by(previous_winner_id)
            ids = [
                claim.id
                for claim in superseded
                if claim.id in conflict_claim_ids
                and claim.event_id == conflict.event_id
                and claim.field_name == conflict.field_name
            ]
            if ids:
                unsuperseded = await self._uow.claims.bulk_unsupersede(ids)

        # Mutate the in-memory conflict for state-machine validation, but
        # persist via update_with_version_check for the optimistic lock.
        conflict.reopen_by_user(reason=reason or "Manual reopen")
        now = utc_now()
        updated_conflict = await self._uow.conflicts.update_with_version_check(
            conflict_id=conflict_id,
            expected_version=expected_version,
            updates={
                "status": ConflictStatus.OPEN.value,
                "winning_claim_id": None,
                "resolved_by": None,
                "resolved_at": None,
                "last_modified_reason": ConflictModifierReason.USER_REOPENED.value,
                "last_modified_note": conflict.last_modified_note,
                "updated_at": now,
            },
        )
        if updated_conflict is None:
            await self._uow.rollback()
            raise await build_conflict_modified_error_after_failed_update(
                self._uow, conflict_id, conflict.event_id
            )

        for claim in unsuperseded:
            await self._uow.claim_history.add(
                ClaimHistory(
                    id=uuid4(),
                    claim_id=claim.id,
                    event_id=claim.event_id,
                    from_value=None,
                    to_value=claim.field_value,
                    from_claim_type=ClaimType.SUPERSEDED,
                    to_claim_type=claim.claim_type,
                    action="reactivated",
                    reason=reason or "Reactivated by manual conflict reopen",
                    modifier_type=ModifierType.USER,
                    modifier_id=current_user_id,
                )
            )

        await self._uow.conflict_activity.add(
            ConflictActivityLogEntry(
                id=uuid4(),
                conflict_id=conflict_id,
                event_id=conflict.event_id,
                sequence=await self._uow.conflict_activity.next_sequence(conflict_id),
                from_status=ConflictStatus.RESOLVED,
                to_status=ConflictStatus.OPEN,
                modifier_type=ModifierType.USER,
                modifier_id=current_user_id,
                reason=reason or "Conflict reopened by user",
                version_at_moment=conflict.version,
                claims_snapshot={
                    "previous_winning_claim_id": str(previous_winner_id)
                    if previous_winner_id
                    else None,
                    "reactivated_claim_ids": [str(claim.id) for claim in unsuperseded],
                },
            )
        )

        projection = await ReProjectEvent(self._uow).execute(
            event_id=updated_conflict.event_id,
            caused_by_conflict_id=conflict_id,
            commit=False,
        )
        await self._uow.commit()
        logger.info(
            "Conflict reopened",
            extra={
                "conflict_id": str(conflict_id),
                "event_id": str(updated_conflict.event_id),
                "reopened_by": str(current_user_id),
                "reactivated_claims": len(unsuperseded),
            },
        )
        return updated_conflict, projection
