"""Unit tests for the shared ``_conflict_modified`` error builder.

These directly test the helpers extracted in this revision so the API error
shape (``current_version`` / ``current_conflict`` / ``current_projection`` /
``latest_activity`` / ``modifier_reason``) is pinned to a contract that
``resolve_conflict.py`` and ``reopen_conflict.py`` both rely on.

End-to-end coverage already exercises this via the use-case tests, but those
tests don't distinguish the two helper semantics:

* ``_from_known`` reports the **caller's already-loaded** conflict (the
  pre-write stale-version path), so the surfaced version is the stale one
  the caller saw.
* ``_after_failed_update`` re-reads the row (the post-write
  optimistic-update-failure path), so the surfaced version is whatever the
  winning concurrent writer left behind.

Future refactors that conflate these would silently change what the API
returns; the two tests below pin the distinction.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from atlas.application.use_cases._conflict_modified import (
    build_conflict_modified_error_after_failed_update,
    build_conflict_modified_error_from_known,
)
from atlas.domain.entities import ClaimConflict, ConflictActivityLogEntry
from atlas.domain.enums import ConflictModifierReason, ConflictStatus, ModifierType
from atlas.domain.exceptions import ConflictModifiedError
from tests.domain._fake_uow import InMemoryUnitOfWork


async def _add_conflict(
    uow: InMemoryUnitOfWork,
    *,
    field_name: str = "fatalities_total",
    version: int = 7,
    status: ConflictStatus = ConflictStatus.OPEN,
    modifier_reason: ConflictModifierReason = ConflictModifierReason.NEW_EVIDENCE,
) -> ClaimConflict:
    conflict = ClaimConflict(
        id=uuid4(),
        event_id=uuid4(),
        field_name=field_name,
        status=status,
        version=version,
        last_modified_reason=modifier_reason,
        claim_ids=[],
        updated_at=datetime(2026, 5, 1, 12, 0, tzinfo=UTC),
    )
    await uow.conflicts.add(conflict)
    return conflict


async def test_from_known_surfaces_caller_provided_conflict_unchanged():
    """The pre-write stale-version path must report the version the caller
    saw, even if the DB row has been updated concurrently since then.

    This is the contract that lets the API tell a client "your snapshot at
    version=N is out of date" rather than "the row is now at version=M".
    """
    uow = InMemoryUnitOfWork()
    conflict = await _add_conflict(uow, version=7)

    # Simulate a concurrent writer bumping the row underneath us by one
    # version. ``update_with_version_check`` mirrors the SQL update which
    # always increments ``version`` itself; we only control the **other**
    # fields, but a one-step bump is enough to prove the helper is reading
    # the caller's snapshot rather than the DB row.
    bumped = await uow.conflicts.update_with_version_check(
        conflict_id=conflict.id,
        expected_version=7,
        updates={
            "last_modified_reason": ConflictModifierReason.USER_RESOLVED.value,
            "updated_at": datetime(2026, 5, 2, 12, 0, tzinfo=UTC),
        },
    )
    assert bumped is not None
    assert bumped.version == 8

    err = await build_conflict_modified_error_from_known(uow, conflict)

    assert isinstance(err, ConflictModifiedError)
    # Reports v7 (what the caller had), NOT the current v8.
    assert err.current_version == 7
    assert err.current_conflict is not None
    assert err.current_conflict["version"] == 7
    assert err.conflict_id == conflict.id
    # Reports the modifier_reason the caller saw, not the concurrent
    # writer's USER_RESOLVED value now on the row.
    assert err.modifier_reason == ConflictModifierReason.NEW_EVIDENCE
    # No projection or activity-log rows exist for this synthetic conflict.
    assert err.current_projection is None
    assert err.latest_activity is None


async def test_after_failed_update_re_reads_and_reflects_current_state():
    """The post-write path must re-read so the error reflects what the next
    writer would race against — not whatever stale entity the caller held."""
    uow = InMemoryUnitOfWork()
    conflict = await _add_conflict(uow, version=3)

    # Bump the row to simulate the concurrent winner.
    await uow.conflicts.update_with_version_check(
        conflict_id=conflict.id,
        expected_version=3,
        updates={
            "version": 4,
            "last_modified_reason": ConflictModifierReason.USER_RESOLVED.value,
            "updated_at": datetime(2026, 5, 2, 12, 0, tzinfo=UTC),
        },
    )

    # Add an activity-log row so the test confirms the helper picks it up.
    await uow.conflict_activity.add(
        ConflictActivityLogEntry(
            id=uuid4(),
            conflict_id=conflict.id,
            event_id=conflict.event_id,
            sequence=1,
            from_status=ConflictStatus.OPEN,
            to_status=ConflictStatus.RESOLVED,
            modifier_type=ModifierType.USER,
            modifier_id=uuid4(),
            reason="winning concurrent writer",
            version_at_moment=4,
        )
    )

    err = await build_conflict_modified_error_after_failed_update(
        uow, conflict.id, conflict.event_id
    )

    # Re-read picked up the post-update state.
    assert err.current_version == 4
    assert err.current_conflict is not None
    assert err.current_conflict["version"] == 4
    # And the modifier reason now matches the concurrent writer, not the
    # NEW_EVIDENCE value the row was seeded with.
    assert err.modifier_reason == ConflictModifierReason.USER_RESOLVED.value
    assert err.latest_activity is not None
    assert err.latest_activity["reason"] == "winning concurrent writer"


async def test_after_failed_update_handles_deleted_conflict_gracefully():
    """If the conflict row has been deleted between the failed update and the
    re-read, the helper must still produce a well-formed error (sentinel
    version, ``None`` payloads) rather than crashing.

    This pins the existing behaviour: the API contract is that
    ``current_version`` is an int even in this edge case; clients use it to
    decide whether to retry or surface a "no longer exists" message.
    """
    uow = InMemoryUnitOfWork()
    missing_conflict_id = uuid4()
    missing_event_id = uuid4()

    err = await build_conflict_modified_error_after_failed_update(
        uow, missing_conflict_id, missing_event_id
    )

    assert err.conflict_id == missing_conflict_id
    # Sentinel value when no row exists — matches the pre-refactor behaviour.
    assert err.current_version == -1
    assert err.current_conflict is None
    assert err.current_projection is None
    assert err.latest_activity is None
    assert err.modifier_reason is None


if __name__ == "__main__":  # pragma: no cover - convenience entrypoint
    pytest.main([__file__, "-v"])
