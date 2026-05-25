from __future__ import annotations

import logging
from uuid import UUID, uuid4

from atlas.application.dto import ProjectionDTO
from atlas.application.settings_protocol import CuratorOverrideSettings
from atlas.application.unit_of_work import UnitOfWork
from atlas.application.use_cases._conflict_modified import (
    build_conflict_modified_error_after_failed_update,
    build_conflict_modified_error_from_known,
)
from atlas.application.use_cases.reproject_event import ReProjectEvent
from atlas.config import get_settings
from atlas.domain.entities import Claim, ClaimHistory, ConflictActivityLogEntry
from atlas.domain.enums import ClaimType, ConflictModifierReason, ConflictStatus, ModifierType
from atlas.domain.exceptions import (
    ClaimNotEligibleError,
    ClaimNotFoundError,
    ClaimNotInConflictError,
    ConflictAlreadyResolvedError,
    ConflictNotFoundError,
    DomainValidationError,
    InvariantViolationError,
)
from atlas.domain.utils import utc_now

logger = logging.getLogger(__name__)


class ResolveConflict:
    def __init__(self, uow: UnitOfWork, settings: CuratorOverrideSettings | None = None) -> None:
        self._uow = uow
        self._settings = settings

    async def execute(
        self,
        conflict_id: UUID,
        expected_version: int,
        winning_claim_id: UUID | None = None,
        manual_override_value: object | None = None,
        manual_override_provided: bool = False,
        current_user_id: UUID | None = None,
        reason: str = "",
    ) -> tuple[object, ProjectionDTO]:
        # Backward-compatible direct use-case callers historically signalled a
        # manual override by passing any non-None value.  The explicit flag is
        # required only to distinguish an intentional JSON null override from
        # an omitted override.
        manual_override_selected = manual_override_provided or manual_override_value is not None
        if winning_claim_id is not None and manual_override_selected:
            raise DomainValidationError(
                "Provide either winning_claim_id or manual_override_value, not both"
            )
        if winning_claim_id is None and not manual_override_selected:
            raise DomainValidationError("Need a winner or an overridden value")
        if current_user_id is None:
            raise DomainValidationError("current_user_id is required")

        conflict = await self._uow.conflicts.get(conflict_id)
        if not conflict:
            raise ConflictNotFoundError(f"Conflict {conflict_id} not found")
        if conflict.status != ConflictStatus.OPEN:
            raise ConflictAlreadyResolvedError(conflict_id)

        # Check before any writes so stale manual overrides cannot leak into the session.
        if conflict.version != expected_version:
            raise await build_conflict_modified_error_from_known(self._uow, conflict)

        claim_ids = set(await self._uow.conflicts.get_claim_ids_for_conflict(conflict_id))

        if manual_override_selected:
            settings = self._settings or get_settings()
            override_source = await self._uow.sources.get(settings.curator_override_source_id)
            if override_source is None:
                raise InvariantViolationError(
                    f"Curator override source '{settings.curator_override_source_name}' not found. "
                    "Seed it via the admin migration before resolving with a manual value."
                )

            winning_claim = Claim(
                id=uuid4(),
                event_id=conflict.event_id,
                source_id=override_source.id,
                field_name=conflict.field_name,
                field_value=manual_override_value,
                claim_type=ClaimType.MANUAL_OVERRIDE,
                created_by=current_user_id,
            )
            await self._uow.claims.add(winning_claim)
            # Flush so the claim exists before its history row's FK
            # references it (see _claim_writer.py for rationale).
            await self._uow.flush()
            await self._uow.claim_history.add(
                ClaimHistory(
                    id=uuid4(),
                    claim_id=winning_claim.id,
                    event_id=conflict.event_id,
                    from_value=None,
                    to_value=manual_override_value,
                    from_claim_type=None,
                    to_claim_type=ClaimType.MANUAL_OVERRIDE,
                    action="created",
                    reason=reason or "Manual override created during conflict resolution",
                    modifier_type=ModifierType.USER,
                    modifier_id=current_user_id,
                )
            )
            await self._uow.conflicts.add_claim_to_conflict(conflict_id, winning_claim.id)
            # Keep the in-memory entity consistent with the DB link we just
            # inserted.  ``conflict.resolve(...)`` validates that
            # ``winning_claim_id`` is one of ``self.claim_ids`` - without this
            # call the new override claim would not be there yet (the entity
            # was loaded before the override was created) and the validation
            # would raise ``ClaimNotInConflictError`` on every happy-path
            # manual override.
            conflict.add_claim_id(winning_claim.id)
            winning_claim_id_final: UUID = winning_claim.id
        else:
            if winning_claim_id is None:
                raise RuntimeError("winning_claim_id is None after input guard - this is a bug")
            if winning_claim_id not in claim_ids:
                raise ClaimNotInConflictError(
                    f"Claim {winning_claim_id} is not part of conflict {conflict_id}"
                )
            existing_claim = await self._uow.claims.get(winning_claim_id)
            if existing_claim is None:
                raise ClaimNotFoundError(f"Winning claim {winning_claim_id} not found")
            # A superseded claim cannot be picked as winner. This can happen
            # if a prior resolve already superseded all losing claims and
            # someone attempts to resolve again to a claim that was
            # subsequently marked SUPERSEDED.
            if not existing_claim.can_win():
                raise ClaimNotEligibleError(
                    f"Claim {winning_claim_id} has type {existing_claim.claim_type!r} and "
                    "is not eligible to win (must be RAW, CONFIRMED, or MANUAL_OVERRIDE)"
                )
            winning_claim_id_final = winning_claim_id

        conflict.resolve(
            winning_claim_id=winning_claim_id_final,
            resolved_by=current_user_id,
            reason=reason,
        )

        if not manual_override_selected:
            # Re-check the source winner under a row lock immediately before
            # committing the conflict resolution. This closes the TOCTOU window
            # where ingestion/merge can supersede the selected claim after the
            # initial ``can_win`` check but before ``winning_claim_id`` is saved.
            locked_winner = await self._uow.claims.lock_for_update(winning_claim_id_final)
            if locked_winner is None:
                raise ClaimNotFoundError(f"Winning claim {winning_claim_id_final} not found")
            if not locked_winner.can_win():
                raise ClaimNotEligibleError(
                    f"Claim {winning_claim_id_final} has type {locked_winner.claim_type!r} and "
                    "is not eligible to win (must be RAW, CONFIRMED, or MANUAL_OVERRIDE)"
                )

        now = utc_now()
        # Persist the same fields that ``ClaimConflict.resolve`` mutated. The
        # repository increments ``version`` itself; ``last_modified_note`` is
        # truncated to the column width on the entity.
        updated_conflict = await self._uow.conflicts.update_with_version_check(
            conflict_id=conflict_id,
            expected_version=expected_version,
            updates={
                "status": ConflictStatus.RESOLVED.value,
                "winning_claim_id": winning_claim_id_final,
                "resolved_by": current_user_id,
                "resolved_at": now,
                "last_modified_reason": ConflictModifierReason.USER_RESOLVED.value,
                "last_modified_note": conflict.last_modified_note,
                "updated_at": now,
            },
        )

        if updated_conflict is None:
            await self._uow.rollback()
            err = await build_conflict_modified_error_after_failed_update(
                self._uow, conflict_id, conflict.event_id
            )
            # Close the implicit autobegin transaction opened by the re-reads
            # above.  Without this, the session holds an open read transaction
            # until garbage collection rather than releasing it immediately.
            await self._uow.rollback()
            raise err

        all_claim_ids = set(await self._uow.conflicts.get_claim_ids_for_conflict(conflict_id))
        losing_ids = list(all_claim_ids - {winning_claim_id_final})
        if losing_ids:
            # Only currently winnable claims should be superseded by this
            # resolution. Historical SUPERSEDED evidence remains linked to the
            # conflict for audit purposes, but its supersession lineage must
            # not be overwritten. The repository also enforces this guard.
            candidate_claims = await self._uow.claims.get_many(losing_ids)
            original_claim_types = {claim.id: claim.claim_type for claim in candidate_claims}
            active_losing_ids = [claim.id for claim in candidate_claims if claim.can_win()]
            losing_claims = await self._uow.claims.bulk_supersede(
                active_losing_ids,
                winning_claim_id_final,
            )
            for losing_claim in losing_claims:
                await self._uow.claim_history.add(
                    ClaimHistory(
                        id=uuid4(),
                        claim_id=losing_claim.id,
                        event_id=losing_claim.event_id,
                        from_value=losing_claim.field_value,
                        to_value=None,
                        from_claim_type=original_claim_types.get(
                            losing_claim.id, losing_claim.claim_type
                        ),
                        to_claim_type=ClaimType.SUPERSEDED,
                        action="superseded",
                        reason=reason or "Superseded by conflict resolution",
                        modifier_type=ModifierType.USER,
                        modifier_id=current_user_id,
                    )
                )

        await self._uow.conflict_activity.add(
            ConflictActivityLogEntry(
                id=uuid4(),
                conflict_id=conflict_id,
                event_id=updated_conflict.event_id,
                sequence=await self._uow.conflict_activity.next_sequence(conflict_id),
                from_status=ConflictStatus.OPEN,
                to_status=ConflictStatus.RESOLVED,
                modifier_type=ModifierType.USER,
                modifier_id=current_user_id,
                reason=reason or "Conflict resolved by user",
                version_at_moment=conflict.version,
                claims_snapshot={"winning_claim_id": str(winning_claim_id_final)},
            )
        )

        projection = await ReProjectEvent(self._uow).execute(
            event_id=updated_conflict.event_id,
            caused_by_conflict_id=conflict_id,
            commit=False,
        )
        await self._uow.commit()
        logger.info(
            "Conflict resolved",
            extra={
                "conflict_id": str(conflict_id),
                "event_id": str(updated_conflict.event_id),
                "winning_claim_id": str(winning_claim_id_final),
                "resolved_by": str(current_user_id),
            },
        )
        return updated_conflict, projection
