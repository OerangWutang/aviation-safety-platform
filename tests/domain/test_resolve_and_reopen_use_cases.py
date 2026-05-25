"""Use-case tests for ResolveConflict + ReopenConflict.

These cover the behaviors flagged in the review:
- last_modified_note (== curator reason) is persisted on the conflict row.
- Stale expected_version raises ConflictModifiedError without leaking any
  manual-override claim into the session.
- ClaimNotInConflictError propagates (router maps to 422 globally).
- Resolved conflict can be reopened by a curator; previously superseded
  losing claims are reactivated and the activity log is appended.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from atlas.application.dto import IngestionClaimDTO
from atlas.application.use_cases.ingest_source_data import IngestSourceData
from atlas.application.use_cases.reopen_conflict import ReopenConflict
from atlas.application.use_cases.resolve_conflict import ResolveConflict
from atlas.domain.entities import Claim, ClaimHistory, Source
from atlas.domain.enums import (
    ClaimType,
    ConflictModifierReason,
    ConflictStatus,
    ModifierType,
    SourceKind,
)
from atlas.domain.exceptions import (
    ClaimNotInConflictError,
    ConflictModifiedError,
    DomainValidationError,
    InvariantViolationError,
)
from tests.domain._fake_uow import InMemoryUnitOfWork, make_settings


@pytest.fixture
async def uow_with_two_sources():
    uow = InMemoryUnitOfWork()
    settings = make_settings()
    # Seed CuratorOverride source so manual-override resolution paths work.
    override = Source(
        id=settings.curator_override_source_id,
        name=settings.curator_override_source_name,
        kind=SourceKind.INTERNAL,
        reliability_tier=1,
    )
    await uow.sources.add(override)
    src_a = Source(id=uuid4(), name="A", kind=SourceKind.EXTERNAL, reliability_tier=1)
    src_b = Source(id=uuid4(), name="B", kind=SourceKind.EXTERNAL, reliability_tier=2)
    await uow.sources.add(src_a)
    await uow.sources.add(src_b)
    return uow, settings, src_a, src_b


async def _create_conflict(uow, settings, src_a, src_b, *, field="fatalities_total"):
    event_id = await IngestSourceData(uow, settings=settings).execute(
        source_id=src_a.id,
        raw_payload={"r": "a"},
        ingestion_run_id=uuid4(),
        claims_data=[IngestionClaimDTO(field_name=field, field_value=5)],
    )
    await IngestSourceData(uow, settings=settings).execute(
        source_id=src_b.id,
        raw_payload={"r": "b"},
        ingestion_run_id=uuid4(),
        claims_data=[IngestionClaimDTO(field_name=field, field_value=6)],
        event_id=event_id,
    )
    conflict = next(iter(uow.store.conflicts.values()))
    return event_id, conflict


async def test_resolve_persists_curator_reason_as_last_modified_note(uow_with_two_sources):
    uow, settings, src_a, src_b = uow_with_two_sources
    _, conflict = await _create_conflict(uow, settings, src_a, src_b)
    winner_claim_id = uow.store.conflict_claim_links[conflict.id][0]
    user_id = uuid4()
    reason = "official AAIB report ref 2024-007"

    updated, _proj = await ResolveConflict(uow, settings=settings).execute(
        conflict_id=conflict.id,
        expected_version=conflict.version,
        winning_claim_id=winner_claim_id,
        current_user_id=user_id,
        reason=reason,
    )

    assert updated.status == ConflictStatus.RESOLVED
    assert updated.last_modified_reason == ConflictModifierReason.USER_RESOLVED.value
    # Regression: the reason must be persisted on the conflict row, not just
    # in the activity log.
    assert updated.last_modified_note == reason

    # The activity log row's reason matches the curator-provided reason too.
    activity = [e for e in uow.store.conflict_activity if e.conflict_id == conflict.id]
    assert any(e.reason == reason for e in activity)


async def test_resolve_long_reason_is_truncated_to_column_width(uow_with_two_sources):
    uow, settings, src_a, src_b = uow_with_two_sources
    _, conflict = await _create_conflict(uow, settings, src_a, src_b)
    winner_claim_id = uow.store.conflict_claim_links[conflict.id][0]
    long_reason = "x" * 500

    updated, _ = await ResolveConflict(uow, settings=settings).execute(
        conflict_id=conflict.id,
        expected_version=conflict.version,
        winning_claim_id=winner_claim_id,
        current_user_id=uuid4(),
        reason=long_reason,
    )
    # Truncated to last_modified_note column width (255).
    assert updated.last_modified_note is not None
    assert len(updated.last_modified_note) == 255


async def test_stale_version_with_manual_override_does_not_create_override_claim(
    uow_with_two_sources,
):
    uow, settings, src_a, src_b = uow_with_two_sources
    _, conflict = await _create_conflict(uow, settings, src_a, src_b)

    # Pretend somebody else already bumped the version.
    stale_version = conflict.version - 1

    pre_claim_count = len(uow.store.claims)
    with pytest.raises(ConflictModifiedError) as exc_info:
        await ResolveConflict(uow, settings=settings).execute(
            conflict_id=conflict.id,
            expected_version=stale_version,
            manual_override_value=999,
            current_user_id=uuid4(),
            reason="stale write",
        )

    # Regression: the early version check must short-circuit BEFORE the
    # override claim is added to the session, otherwise a rollback would
    # be required to discard it.
    assert len(uow.store.claims) == pre_claim_count
    assert exc_info.value.current_version == conflict.version


async def test_resolve_input_guards_raise_typed_domain_validation(uow_with_two_sources):
    uow, settings, src_a, src_b = uow_with_two_sources
    _, conflict = await _create_conflict(uow, settings, src_a, src_b)
    winner_id, _ = uow.store.conflict_claim_links[conflict.id]

    with pytest.raises(DomainValidationError):
        await ResolveConflict(uow, settings=settings).execute(
            conflict_id=conflict.id,
            expected_version=conflict.version,
            winning_claim_id=winner_id,
            manual_override_value=999,
            current_user_id=uuid4(),
        )

    with pytest.raises(DomainValidationError):
        await ResolveConflict(uow, settings=settings).execute(
            conflict_id=conflict.id,
            expected_version=conflict.version,
            current_user_id=uuid4(),
        )

    with pytest.raises(DomainValidationError):
        await ResolveConflict(uow, settings=settings).execute(
            conflict_id=conflict.id,
            expected_version=conflict.version,
            winning_claim_id=winner_id,
            current_user_id=None,
        )


async def test_reopen_missing_user_raises_typed_domain_validation(uow_with_two_sources):
    uow, settings, src_a, src_b = uow_with_two_sources
    _, conflict = await _create_conflict(uow, settings, src_a, src_b)
    winner_id, _ = uow.store.conflict_claim_links[conflict.id]

    resolved, _ = await ResolveConflict(uow, settings=settings).execute(
        conflict_id=conflict.id,
        expected_version=conflict.version,
        winning_claim_id=winner_id,
        current_user_id=uuid4(),
    )

    with pytest.raises(DomainValidationError):
        await ReopenConflict(uow).execute(
            conflict_id=conflict.id,
            expected_version=resolved.version,
            current_user_id=None,
        )


async def test_missing_curator_override_source_is_invariant_violation(uow_with_two_sources):
    uow, settings, src_a, src_b = uow_with_two_sources
    _, conflict = await _create_conflict(uow, settings, src_a, src_b)
    del uow.store.sources[settings.curator_override_source_id]

    with pytest.raises(InvariantViolationError):
        await ResolveConflict(uow, settings=settings).execute(
            conflict_id=conflict.id,
            expected_version=conflict.version,
            manual_override_value=999,
            current_user_id=uuid4(),
        )


async def test_resolve_with_manual_override_succeeds_and_creates_override_claim(
    uow_with_two_sources,
):
    """Regression: the happy-path of manual override must actually work.

    Prior to this test, every manual-override test in the suite supplied a stale
    ``expected_version`` so it short-circuited at the optimistic version check
    before reaching ``conflict.resolve(...)``.  That hid a real bug: the entity
    method enforces ``winning_claim_id in self.claim_ids``, but the in-memory
    ``conflict`` was loaded before the new override claim was added to the
    conflict's claim list - so the check raised ``ClaimNotInConflictError`` on
    the happy path.

    The fix is to keep the in-memory entity consistent by calling
    ``conflict.add_claim_id(new_claim.id)`` after persisting the link.
    """
    uow, settings, src_a, src_b = uow_with_two_sources
    event_id, conflict = await _create_conflict(uow, settings, src_a, src_b)
    user_id = uuid4()
    pre_claim_count = len(uow.store.claims)

    updated, _projection = await ResolveConflict(uow, settings=settings).execute(
        conflict_id=conflict.id,
        expected_version=conflict.version,
        manual_override_value=999,
        current_user_id=user_id,
        reason="curator override",
    )

    assert updated.status == ConflictStatus.RESOLVED
    assert updated.last_modified_reason == ConflictModifierReason.USER_RESOLVED.value

    # Exactly one new MANUAL_OVERRIDE claim should have been created and it
    # must be the winner referenced by the resolved conflict.
    assert len(uow.store.claims) == pre_claim_count + 1
    override_claims = [
        c for c in uow.store.claims.values() if c.claim_type == ClaimType.MANUAL_OVERRIDE
    ]
    assert len(override_claims) == 1
    override_claim = override_claims[0]
    assert updated.winning_claim_id == override_claim.id
    assert override_claim.field_value == 999
    assert override_claim.event_id == event_id
    assert override_claim.created_by == user_id

    # The override claim must be linked to the conflict in the link table.
    assert override_claim.id in uow.store.conflict_claim_links[conflict.id]


async def test_resolve_with_manual_override_projects_overridden_value(
    uow_with_two_sources,
):
    """The new MANUAL_OVERRIDE claim must beat all other active claims in the
    projection, regardless of source reliability tier.  ``WinnerPolicy`` ranks
    MANUAL_OVERRIDE strictly above CONFIRMED/RAW, so the projected field value
    must equal the override value the curator supplied.
    """
    uow, settings, src_a, src_b = uow_with_two_sources
    event_id, conflict = await _create_conflict(
        uow, settings, src_a, src_b, field="fatalities_total"
    )

    _updated, projection = await ResolveConflict(uow, settings=settings).execute(
        conflict_id=conflict.id,
        expected_version=conflict.version,
        manual_override_value=42,
        current_user_id=uuid4(),
        reason="reviewed reports agree on 42",
    )

    assert projection.event_id == event_id
    assert projection.fields.get("fatalities_total") == 42
    # The previously-disputed field must no longer be marked unresolved.
    assert "fatalities_total" not in projection.unresolved_conflict_fields


async def test_resolve_with_claim_not_in_conflict_raises(uow_with_two_sources):
    uow, settings, src_a, src_b = uow_with_two_sources
    _, conflict = await _create_conflict(uow, settings, src_a, src_b)
    foreign_claim_id = uuid4()

    with pytest.raises(ClaimNotInConflictError):
        await ResolveConflict(uow, settings=settings).execute(
            conflict_id=conflict.id,
            expected_version=conflict.version,
            winning_claim_id=foreign_claim_id,
            current_user_id=uuid4(),
        )


async def test_resolve_supersedes_losing_claims(uow_with_two_sources):
    uow, settings, src_a, src_b = uow_with_two_sources
    _, conflict = await _create_conflict(uow, settings, src_a, src_b)
    winner_id, loser_id = uow.store.conflict_claim_links[conflict.id]

    await ResolveConflict(uow, settings=settings).execute(
        conflict_id=conflict.id,
        expected_version=conflict.version,
        winning_claim_id=winner_id,
        current_user_id=uuid4(),
        reason="canonical source wins",
    )

    assert uow.store.claims[winner_id].claim_type != ClaimType.SUPERSEDED
    assert uow.store.claims[loser_id].claim_type == ClaimType.SUPERSEDED
    assert uow.store.claims[loser_id].superseded_by_claim_id == winner_id


async def test_reopen_resolved_conflict_reactivates_losers_and_logs(uow_with_two_sources):
    uow, settings, src_a, src_b = uow_with_two_sources
    _event_id, conflict = await _create_conflict(uow, settings, src_a, src_b)
    winner_id, loser_id = uow.store.conflict_claim_links[conflict.id]
    user_id = uuid4()

    # Resolve first.
    resolved, _ = await ResolveConflict(uow, settings=settings).execute(
        conflict_id=conflict.id,
        expected_version=conflict.version,
        winning_claim_id=winner_id,
        current_user_id=user_id,
        reason="initial decision",
    )
    assert resolved.status == ConflictStatus.RESOLVED

    # Then reopen.
    reopened, projection = await ReopenConflict(uow).execute(
        conflict_id=conflict.id,
        expected_version=resolved.version,
        current_user_id=user_id,
        reason="curator changed their mind",
    )

    assert reopened.status == ConflictStatus.OPEN
    assert reopened.winning_claim_id is None
    assert reopened.last_modified_reason == ConflictModifierReason.USER_REOPENED.value
    assert reopened.last_modified_note == "curator changed their mind"

    # Loser claim is back in active state and detached from the winner.
    assert uow.store.claims[loser_id].claim_type == ClaimType.RAW
    assert uow.store.claims[loser_id].superseded_by_claim_id is None

    # Projection now shows the field as DISPUTED again.
    assert "fatalities_total" in projection.unresolved_conflict_fields

    # Activity log records the manual reopen transition.
    activity = [e for e in uow.store.conflict_activity if e.conflict_id == conflict.id]
    last = activity[-1]
    assert last.from_status == ConflictStatus.RESOLVED
    assert last.to_status == ConflictStatus.OPEN

    # Claim history records the reactivation.
    reactivation_history = [h for h in uow.store.claim_history if h.action == "reactivated"]
    assert len(reactivation_history) == 1
    assert reactivation_history[0].claim_id == loser_id


async def test_reopen_restores_loser_previous_confirmed_type(uow_with_two_sources):
    uow, settings, src_a, src_b = uow_with_two_sources
    _event_id, conflict = await _create_conflict(uow, settings, src_a, src_b)
    winner_id, loser_id = uow.store.conflict_claim_links[conflict.id]

    # Simulate a curator-confirmed claim that later loses this conflict.
    uow.store.claims[loser_id].claim_type = ClaimType.CONFIRMED

    resolved, _ = await ResolveConflict(uow, settings=settings).execute(
        conflict_id=conflict.id,
        expected_version=conflict.version,
        winning_claim_id=winner_id,
        current_user_id=uuid4(),
        reason="raw claim wins for now",
    )
    assert uow.store.claims[loser_id].claim_type == ClaimType.SUPERSEDED

    await ReopenConflict(uow).execute(
        conflict_id=conflict.id,
        expected_version=resolved.version,
        current_user_id=uuid4(),
        reason="restore evidence",
    )

    assert uow.store.claims[loser_id].claim_type == ClaimType.CONFIRMED
    reactivation_history = [
        h for h in uow.store.claim_history if h.action == "reactivated" and h.claim_id == loser_id
    ]
    assert reactivation_history[-1].to_claim_type == ClaimType.CONFIRMED


async def test_resolve_history_uses_pre_supersede_claim_type_even_if_repo_returns_updated_entities(
    uow_with_two_sources,
):
    """Regression: audit history must preserve the losing claim's original
    type even if a repository implementation returns post-update entities from
    bulk_supersede(). Reopen relies on this history to restore the claim.
    """
    import copy

    uow, settings, src_a, src_b = uow_with_two_sources
    _event_id, conflict = await _create_conflict(uow, settings, src_a, src_b)
    winner_id, loser_id = uow.store.conflict_claim_links[conflict.id]
    uow.store.claims[loser_id].claim_type = ClaimType.CONFIRMED

    original_bulk_supersede = uow.claims.bulk_supersede

    async def bulk_supersede_returning_updated_entities(claim_ids, by_claim_id):
        await original_bulk_supersede(claim_ids, by_claim_id)
        return [copy.deepcopy(uow.store.claims[cid]) for cid in claim_ids]

    uow.claims.bulk_supersede = bulk_supersede_returning_updated_entities

    await ResolveConflict(uow, settings=settings).execute(
        conflict_id=conflict.id,
        expected_version=conflict.version,
        winning_claim_id=winner_id,
        current_user_id=uuid4(),
        reason="canonical source wins",
    )

    superseded_history = [
        h for h in uow.store.claim_history if h.action == "superseded" and h.claim_id == loser_id
    ]
    assert superseded_history[-1].from_claim_type == ClaimType.CONFIRMED
    assert superseded_history[-1].to_claim_type == ClaimType.SUPERSEDED


async def test_reopen_does_not_unsupersede_unrelated_claims_with_same_winner(
    uow_with_two_sources,
):
    uow, settings, src_a, src_b = uow_with_two_sources
    event_id, conflict = await _create_conflict(uow, settings, src_a, src_b)
    winner_id, _loser_id = uow.store.conflict_claim_links[conflict.id]

    unrelated_claim = Claim(
        id=uuid4(),
        event_id=event_id,
        source_id=src_b.id,
        field_name="registration",
        field_value="PH-XYZ",
        claim_type=ClaimType.SUPERSEDED,
        superseded_by_claim_id=winner_id,
    )
    await uow.claims.add(unrelated_claim)
    await uow.claim_history.add(
        ClaimHistory(
            id=uuid4(),
            claim_id=unrelated_claim.id,
            event_id=event_id,
            from_value=unrelated_claim.field_value,
            to_value=unrelated_claim.field_value,
            from_claim_type=ClaimType.CONFIRMED,
            to_claim_type=ClaimType.SUPERSEDED,
            action="superseded",
            reason="unrelated previous workflow",
            modifier_type=ModifierType.USER,
            modifier_id=uuid4(),
        )
    )

    resolved, _ = await ResolveConflict(uow, settings=settings).execute(
        conflict_id=conflict.id,
        expected_version=conflict.version,
        winning_claim_id=winner_id,
        current_user_id=uuid4(),
        reason="resolve fatalities conflict",
    )

    await ReopenConflict(uow).execute(
        conflict_id=conflict.id,
        expected_version=resolved.version,
        current_user_id=uuid4(),
        reason="reopen only this conflict",
    )

    assert uow.store.claims[unrelated_claim.id].claim_type == ClaimType.SUPERSEDED
    assert uow.store.claims[unrelated_claim.id].superseded_by_claim_id == winner_id


async def test_reopen_only_works_on_resolved_conflict(uow_with_two_sources):
    uow, settings, src_a, src_b = uow_with_two_sources
    _, conflict = await _create_conflict(uow, settings, src_a, src_b)

    with pytest.raises(DomainValidationError):
        await ReopenConflict(uow).execute(
            conflict_id=conflict.id,
            expected_version=conflict.version,
            current_user_id=uuid4(),
            reason="too early",
        )


async def test_resolve_with_superseded_claim_raises_claim_not_eligible(uow_with_two_sources):
    uow, settings, src_a, src_b = uow_with_two_sources
    _, conflict = await _create_conflict(uow, settings, src_a, src_b)
    winner_id, loser_id = uow.store.conflict_claim_links[conflict.id]

    # Resolve once to supersede the loser.
    resolved, _ = await ResolveConflict(uow, settings=settings).execute(
        conflict_id=conflict.id,
        expected_version=conflict.version,
        winning_claim_id=winner_id,
        current_user_id=uuid4(),
    )

    # Reopen so we can try to resolve again with the now-superseded claim.
    reopened, _ = await ReopenConflict(uow).execute(
        conflict_id=conflict.id,
        expected_version=resolved.version,
        current_user_id=uuid4(),
        reason="test reopen",
    )

    # The loser was reactivated by reopen. Directly force it back to SUPERSEDED
    # to simulate a situation where the claim shouldn't be eligible to win.
    uow.store.claims[loser_id].claim_type = ClaimType.SUPERSEDED

    from atlas.domain.exceptions import ClaimNotEligibleError

    with pytest.raises(ClaimNotEligibleError):
        await ResolveConflict(uow, settings=settings).execute(
            conflict_id=conflict.id,
            expected_version=reopened.version,
            winning_claim_id=loser_id,  # superseded claim - must be rejected
            current_user_id=uuid4(),
        )


async def test_reopen_with_stale_version_raises_conflict_modified(uow_with_two_sources):
    uow, settings, src_a, src_b = uow_with_two_sources
    _, conflict = await _create_conflict(uow, settings, src_a, src_b)
    winner_id, _ = uow.store.conflict_claim_links[conflict.id]

    resolved, _ = await ResolveConflict(uow, settings=settings).execute(
        conflict_id=conflict.id,
        expected_version=conflict.version,
        winning_claim_id=winner_id,
        current_user_id=uuid4(),
    )

    with pytest.raises(ConflictModifiedError):
        await ReopenConflict(uow).execute(
            conflict_id=conflict.id,
            expected_version=resolved.version - 1,
            current_user_id=uuid4(),
            reason="stale",
        )


async def test_resolve_with_explicit_null_manual_override_is_allowed(
    uow_with_two_sources,
):
    """Explicit JSON null is a meaningful curator override, distinct from omission."""
    uow, settings, src_a, src_b = uow_with_two_sources
    event_id, conflict = await _create_conflict(
        uow, settings, src_a, src_b, field="fatalities_total"
    )

    updated, projection = await ResolveConflict(uow, settings=settings).execute(
        conflict_id=conflict.id,
        expected_version=conflict.version,
        manual_override_value=None,
        manual_override_provided=True,
        current_user_id=uuid4(),
        reason="official source says value is unknown",
    )

    assert updated.status == ConflictStatus.RESOLVED
    override_claim = uow.store.claims[updated.winning_claim_id]
    assert override_claim.claim_type == ClaimType.MANUAL_OVERRIDE
    assert override_claim.field_value is None
    assert override_claim.event_id == event_id
    assert projection.fields["fatalities_total"] is None


async def test_resolve_revalidates_winner_under_lock_before_commit(uow_with_two_sources):
    uow, settings, src_a, src_b = uow_with_two_sources
    _, conflict = await _create_conflict(uow, settings, src_a, src_b)
    winner_id, _ = uow.store.conflict_claim_links[conflict.id]

    async def supersede_during_final_lock(claim_id):
        uow.store.claims[claim_id].claim_type = ClaimType.SUPERSEDED
        return uow.store.claims[claim_id]

    uow.claims.lock_for_update = supersede_during_final_lock  # type: ignore[method-assign]

    from atlas.domain.exceptions import ClaimNotEligibleError

    with pytest.raises(ClaimNotEligibleError):
        await ResolveConflict(uow, settings=settings).execute(
            conflict_id=conflict.id,
            expected_version=conflict.version,
            winning_claim_id=winner_id,
            current_user_id=uuid4(),
            reason="stale winner race",
        )

    stored = uow.store.conflicts[conflict.id]
    assert stored.status == ConflictStatus.OPEN
    assert stored.winning_claim_id is None
