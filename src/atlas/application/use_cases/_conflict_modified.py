"""Shared helpers to build ``ConflictModifiedError`` from current UoW state.

Both ``ResolveConflict`` and ``ReopenConflict`` raise
``ConflictModifiedError`` on (a) the pre-write stale-version check and
(b) the post-write ``update_with_version_check`` failure.  The payload
shape (``current_conflict`` / ``current_projection`` / ``latest_activity``
/ ``modifier_reason``) is part of the API error contract surfaced by the
conflicts router and must remain identical between the two use cases.

Two separate functions are intentional rather than one with a mode flag:
the pre-write path already has the conflict entity loaded in memory and
must report **that** value, while the post-write path must re-read so the
error reflects the row state the next writer would see.  Naming each path
makes the call site read clearly and avoids accidentally swapping them.

The transaction-management semantics (when to ``rollback`` before
re-reading, etc.) remain the caller's responsibility — this helper is
purely a constructor that issues read queries through the supplied UoW.
"""

from __future__ import annotations

from uuid import UUID

from atlas.application.unit_of_work import UnitOfWork
from atlas.domain.entities import ClaimConflict
from atlas.domain.exceptions import ConflictModifiedError


async def build_conflict_modified_error_from_known(
    uow: UnitOfWork,
    conflict: ClaimConflict,
) -> ConflictModifiedError:
    """Pre-write stale-version path: report the already-loaded conflict.

    Called when the caller's loaded conflict has ``version !=
    expected_version`` and no write has been attempted yet.  The helper
    reads only the auxiliary state (current projection, latest activity);
    the conflict itself is taken as-is.
    """
    current_proj = await uow.projections.get(conflict.event_id)
    latest_activity = await uow.conflict_activity.latest_for_conflict(conflict.id)
    return ConflictModifiedError(
        conflict_id=conflict.id,
        current_version=conflict.version,
        current_conflict=conflict.model_dump(mode="json"),
        current_projection=current_proj.model_dump(mode="json") if current_proj else None,
        latest_activity=latest_activity.model_dump(mode="json") if latest_activity else None,
        modifier_reason=conflict.last_modified_reason,
    )


async def build_conflict_modified_error_after_failed_update(
    uow: UnitOfWork,
    conflict_id: UUID,
    event_id: UUID,
) -> ConflictModifiedError:
    """Post-write optimistic-update failure: re-read for the latest state.

    Called after ``update_with_version_check`` returned ``None``.  The
    in-memory ``conflict`` we loaded is now stale by definition; the
    helper re-fetches so the response surfaces the version a retry
    would race against.

    ``event_id`` is passed explicitly because the helper does not assume
    the re-fetched conflict row still exists (concurrent deletion is
    unusual but the contract must still produce a valid error payload).
    """
    current_conflict = await uow.conflicts.get(conflict_id)
    current_proj = await uow.projections.get(event_id)
    latest_activity = await uow.conflict_activity.latest_for_conflict(conflict_id)
    return ConflictModifiedError(
        conflict_id=conflict_id,
        current_version=current_conflict.version if current_conflict else -1,
        current_conflict=current_conflict.model_dump(mode="json") if current_conflict else None,
        current_projection=current_proj.model_dump(mode="json") if current_proj else None,
        latest_activity=latest_activity.model_dump(mode="json") if latest_activity else None,
        modifier_reason=current_conflict.last_modified_reason if current_conflict else None,
    )
