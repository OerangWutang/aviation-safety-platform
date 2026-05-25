"""Pin the design asymmetry between the two reopen paths.

There are two routes from RESOLVED back to OPEN:

1. **Curator-driven** (``ReopenConflict.execute``) — the curator explicitly
   says "I changed my mind."  In this case the conflict's prior losers are
   *reactivated* via ``bulk_unsupersede`` so they re-enter the projection
   under their original ``claim_type``.  This is documented in
   ``ARCHITECTURE.md`` ("Reopen — what happens" section, step 3).

2. **Ingestion-triggered** (``ConflictReconciler._reopen_resolved_for_evidence``)
   — a new source arrives whose value contradicts the resolved winner.  In
   this case the prior losers stay ``SUPERSEDED``.  The new contradicting
   claim is added as fresh evidence and the conflict re-opens between the
   prior winner and the new claim only.

These two behaviours are **deliberately different**:

- The curator reopen says "my earlier resolve decision is suspect; restore
  the prior dispute exactly as it was."
- The ingestion reopen says "a new dispute has emerged; the earlier resolve
  decision still stood at the time it was made."

This file pins both behaviours so a well-intentioned refactor that tries to
make them symmetric is caught immediately.  See r12 release-readiness
review for the discussion.

Domain invariants this test protects (from the project brief):
- Invariant 6: Conflict lifecycle OPEN -> RESOLVED -> OPEN with correct
  claim_history and conflict_activity audit behavior.
- Invariant 7: Resolved conflicts must honor winning_claim_id unless new
  contradictory evidence reopens or reconciliation explicitly changes
  state.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from atlas.application.dto import IngestionClaimDTO
from atlas.application.use_cases.ingest_source_data import IngestSourceData
from atlas.application.use_cases.reopen_conflict import ReopenConflict
from atlas.application.use_cases.resolve_conflict import ResolveConflict
from atlas.domain.entities import Source
from atlas.domain.enums import ClaimType, ConflictStatus, SourceKind
from tests.domain._fake_uow import InMemoryUnitOfWork, make_settings


@pytest.fixture
async def three_sources():
    """A UoW with three external sources and a curator-override seed.

    Three sources are needed because the ingestion-triggered reopen
    requires a fresh source bringing a contradicting claim *after* the
    conflict has already been resolved.
    """
    uow = InMemoryUnitOfWork()
    settings = make_settings()
    await uow.sources.add(
        Source(
            id=settings.curator_override_source_id,
            name=settings.curator_override_source_name,
            kind=SourceKind.INTERNAL,
            reliability_tier=1,
        )
    )
    src_a = Source(id=uuid4(), name="A", kind=SourceKind.EXTERNAL, reliability_tier=1)
    src_b = Source(id=uuid4(), name="B", kind=SourceKind.EXTERNAL, reliability_tier=2)
    src_c = Source(id=uuid4(), name="C", kind=SourceKind.EXTERNAL, reliability_tier=3)
    for s in (src_a, src_b, src_c):
        await uow.sources.add(s)
    return uow, settings, src_a, src_b, src_c


async def _resolved_conflict_with_winner_a(uow, settings, src_a, src_b):
    """Set up: A says 5, B says 6, curator picks A.  Returns
    ``(event_id, conflict, winner_id, loser_id)``."""
    field = "fatalities_total"
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
    winner_id, loser_id = uow.store.conflict_claim_links[conflict.id]
    # ``_create_conflict`` in the sibling test files relies on the same
    # ordering; if this assertion ever fires it means a fake-UoW change
    # broke the implicit contract these tests share.
    assert uow.store.claims[winner_id].source_id == src_a.id

    resolved, _ = await ResolveConflict(uow, settings=settings).execute(
        conflict_id=conflict.id,
        expected_version=conflict.version,
        winning_claim_id=winner_id,
        current_user_id=uuid4(),
        reason="A is canonical for now",
    )
    assert resolved.status == ConflictStatus.RESOLVED
    assert uow.store.claims[loser_id].claim_type == ClaimType.SUPERSEDED
    return event_id, resolved, winner_id, loser_id


async def test_curator_reopen_reactivates_prior_loser(three_sources):
    """Baseline: ``ReopenConflict`` (the explicit curator endpoint) DOES
    flip SUPERSEDED claims back to active state via ``bulk_unsupersede``.

    Already covered by ``test_resolve_and_reopen_use_cases``; this is the
    explicit counterpart to the ingestion-path assertion below.
    """
    uow, settings, src_a, src_b, _src_c = three_sources
    _event_id, resolved, _winner_id, loser_id = await _resolved_conflict_with_winner_a(
        uow, settings, src_a, src_b
    )

    await ReopenConflict(uow).execute(
        conflict_id=resolved.id,
        expected_version=resolved.version,
        current_user_id=uuid4(),
        reason="curator reopen",
    )

    # Loser claim is active again, ready to participate in the next resolve.
    assert uow.store.claims[loser_id].claim_type == ClaimType.RAW
    assert uow.store.claims[loser_id].superseded_by_claim_id is None
    # Audit row recorded.
    assert any(
        h.action == "reactivated" and h.claim_id == loser_id for h in uow.store.claim_history
    )


async def test_ingestion_triggered_reopen_does_not_revive_prior_losers(three_sources):
    """Pin: when ingestion brings contradicting evidence, the reopen path
    leaves the prior conflict's losers SUPERSEDED.

    The new dispute is between the prior winner and the new claim only.
    The curator's earlier resolve decision is preserved as historical
    fact: at the time it was made, B (the loser) was correctly displaced
    by A (the winner).  The new evidence does not retroactively invalidate
    that decision.

    If a future refactor makes this path call ``bulk_unsupersede`` to
    mirror the curator path, this test will fail loudly so the change is
    deliberate and not accidental.
    """
    uow, settings, src_a, src_b, src_c = three_sources
    event_id, resolved, _winner_id, loser_id = await _resolved_conflict_with_winner_a(
        uow, settings, src_a, src_b
    )

    # Source C arrives with a value contradicting A's winning claim.
    await IngestSourceData(uow, settings=settings).execute(
        source_id=src_c.id,
        raw_payload={"r": "c"},
        ingestion_run_id=uuid4(),
        claims_data=[IngestionClaimDTO(field_name="fatalities_total", field_value=7)],
        event_id=event_id,
    )

    reloaded = uow.store.conflicts[resolved.id]
    # The lifecycle transition itself happens (covered by p2 regressions);
    # the *additional* invariant pinned here is what happens to the prior
    # losers, which the existing test does not assert.
    assert reloaded.status == ConflictStatus.OPEN
    assert reloaded.winning_claim_id is None
    # Prior loser stays SUPERSEDED.
    assert uow.store.claims[loser_id].claim_type == ClaimType.SUPERSEDED
    assert uow.store.claims[loser_id].superseded_by_claim_id is not None
    # No reactivation audit row for the prior loser.
    assert not any(
        h.action == "reactivated" and h.claim_id == loser_id for h in uow.store.claim_history
    ), (
        "Ingestion-triggered reopen must not write 'reactivated' history rows "
        "for the prior conflict's losers; only curator-driven reopen does that."
    )


async def test_ingestion_reopen_then_curator_can_still_reopen_to_revive_losers(
    three_sources,
):
    """If the curator wants the prior losers back in the active set, they
    can still use the explicit reopen endpoint after the ingestion-path
    has reopened the conflict on its own — except the conflict is already
    OPEN at that point, so the curator endpoint rejects the call.

    This is the documented escape hatch: the curator should resolve the
    newly-opened conflict, then reopen if they want the older losers
    reactivated.  This test pins that the ``ReopenConflict`` use case
    correctly refuses to operate on an OPEN conflict, which is the only
    state-machine guard that keeps these two paths from interleaving in
    surprising ways.
    """
    from atlas.domain.exceptions import DomainValidationError

    uow, settings, src_a, src_b, src_c = three_sources
    event_id, resolved, _winner_id, _loser_id = await _resolved_conflict_with_winner_a(
        uow, settings, src_a, src_b
    )

    await IngestSourceData(uow, settings=settings).execute(
        source_id=src_c.id,
        raw_payload={"r": "c"},
        ingestion_run_id=uuid4(),
        claims_data=[IngestionClaimDTO(field_name="fatalities_total", field_value=7)],
        event_id=event_id,
    )

    reloaded = uow.store.conflicts[resolved.id]
    assert reloaded.status == ConflictStatus.OPEN

    with pytest.raises(DomainValidationError):
        await ReopenConflict(uow).execute(
            conflict_id=reloaded.id,
            expected_version=reloaded.version,
            current_user_id=uuid4(),
            reason="trying to revive prior losers while already OPEN",
        )
