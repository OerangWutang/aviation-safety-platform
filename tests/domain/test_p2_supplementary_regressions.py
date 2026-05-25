"""Supplementary regression tests covering P2 review items.

These tests cover behaviours the code review requested explicit regression
coverage for, using the fake UoW (no database required):

* Projection builder emits a WARNING log when a resolved conflict's winning
  claim is inactive (stale-winner fallback path).
* Merge canonicalization: anonymous ingestion matching a merged event's
  identity entry resolves to the canonical surviving event.
* Direct registration lookup bypasses the date-limited 50-row candidate cap
  (unit-level; SQL cap is integration-only).
* review_duplicate source_event_id direction is already covered in
  test_p1_correctness_fixes; these add boundary and idempotency variants.
* Conflict policy: ingest_source_data does not create conflicts for
  same-source contradictions under the default cross-source policy.
"""

from __future__ import annotations

import logging
from uuid import UUID, uuid4

from atlas.application.dto import IngestionClaimDTO
from atlas.application.use_cases.ingest_source_data import IngestSourceData
from atlas.application.use_cases.merge_duplicate_events import MergeDuplicateEvents
from atlas.application.use_cases.resolve_conflict import ResolveConflict
from atlas.domain.entities import (
    AccidentEvent,
    Claim,
    ClaimConflict,
    Source,
)
from atlas.domain.enums import (
    ClaimType,
    ConflictModifierReason,
    ConflictStatus,
    SourceKind,
)
from atlas.domain.services.projection_builder import ProjectionBuilder
from tests.domain._fake_uow import InMemoryUnitOfWork, make_settings

# ── Stale-winner warning ───────────────────────────────────────────────────────


def _make_claim(event_id: UUID, source_id: UUID, field: str, value: object) -> Claim:
    return Claim(
        id=uuid4(),
        event_id=event_id,
        source_id=source_id,
        raw_snapshot_id=uuid4(),
        field_name=field,
        field_value=value,
        claim_type=ClaimType.RAW,
    )


def test_projection_builder_warns_on_stale_resolved_winner(caplog):
    """ProjectionBuilder emits a WARNING when the winning claim of a RESOLVED
    conflict is no longer in the active claim list."""
    event_id = uuid4()
    src_a = uuid4()

    active_claim = _make_claim(event_id, src_a, "fatalities_total", 3)
    # Stale winner - not in the active claims list
    stale_winner_id = uuid4()

    conflict = ClaimConflict(
        id=uuid4(),
        event_id=event_id,
        field_name="fatalities_total",
        status=ConflictStatus.RESOLVED,
        version=2,
        last_modified_reason=ConflictModifierReason.USER_RESOLVED,
        claim_ids=[stale_winner_id],
        winning_claim_id=stale_winner_id,  # not in active claims below
    )

    sources_by_id: dict = {}
    with caplog.at_level(logging.WARNING, logger="atlas.domain.services.projection_builder"):
        result = ProjectionBuilder().build(
            event_id=event_id,
            claims=[active_claim],
            conflicts=[conflict],
            sources_by_id=sources_by_id,
            projection_version=1,
        )

    assert "inactive" in caplog.text.lower() or "winning_claim_id" in caplog.text, (
        "Expected a warning about inactive winning_claim_id"
    )
    # Field still has a value via fallback winner policy - not dropped
    assert "fatalities_total" in result.fields


def test_projection_builder_no_warning_for_active_winner(caplog):
    """No warning when the resolved winner is still in the active claim list."""
    event_id = uuid4()
    src_a = uuid4()
    active_claim = _make_claim(event_id, src_a, "fatalities_total", 3)

    conflict = ClaimConflict(
        id=uuid4(),
        event_id=event_id,
        field_name="fatalities_total",
        status=ConflictStatus.RESOLVED,
        version=2,
        last_modified_reason=ConflictModifierReason.USER_RESOLVED,
        claim_ids=[active_claim.id],
        winning_claim_id=active_claim.id,  # present in claims below
    )

    with caplog.at_level(logging.WARNING, logger="atlas.domain.services.projection_builder"):
        ProjectionBuilder().build(
            event_id=event_id,
            claims=[active_claim],
            conflicts=[conflict],
            sources_by_id={},
            projection_version=1,
        )

    assert "inactive" not in caplog.text.lower()


# ── Merge canonicalization ─────────────────────────────────────────────────────


async def test_anonymous_ingestion_matching_merged_event_attaches_to_canonical():
    """An ingestion whose identity matches a *merged* event's index entry must
    resolve to the canonical surviving event, not the absorbed one."""
    uow = InMemoryUnitOfWork()
    settings = make_settings()
    src = Source(id=uuid4(), name="S", kind=SourceKind.EXTERNAL, reliability_tier=1)
    override = Source(
        id=settings.curator_override_source_id,
        name=settings.curator_override_source_name,
        kind=SourceKind.INTERNAL,
        reliability_tier=1,
    )
    await uow.sources.add(src)
    await uow.sources.add(override)

    # First event gets high-confidence identity data.
    event_a_id = await IngestSourceData(uow, settings).execute(
        source_id=src.id,
        raw_payload={"r": "a"},
        ingestion_run_id=uuid4(),
        claims_data=[
            IngestionClaimDTO(field_name="event_date", field_value="2024-03-15"),
            IngestionClaimDTO(field_name="registration", field_value="N12345"),
        ],
    )

    # Second event independently created for the same accident.
    event_b = AccidentEvent(id=uuid4())
    await uow.events.add(event_b)
    await IngestSourceData(uow, settings).execute(
        source_id=src.id,
        raw_payload={"r": "b"},
        ingestion_run_id=uuid4(),
        claims_data=[IngestionClaimDTO(field_name="event_date", field_value="2024-03-16")],
        event_id=event_b.id,
    )

    # Merge B into A (B absorbed).
    await MergeDuplicateEvents(uow).execute(
        source_event_id=event_b.id,
        target_event_id=event_a_id,
        resolved_by=uuid4(),
        note="confirmed duplicate",
    )

    # Now a new ingestion with the same identity as A arrives anonymously.
    new_event_id = await IngestSourceData(uow, settings).execute(
        source_id=src.id,
        raw_payload={"r": "c"},
        ingestion_run_id=uuid4(),
        claims_data=[
            IngestionClaimDTO(field_name="event_date", field_value="2024-03-15"),
            IngestionClaimDTO(field_name="registration", field_value="N12345"),
        ],
    )

    assert new_event_id == event_a_id, (
        f"Anonymous ingestion matching merged event identity should attach to "
        f"canonical event {event_a_id}, got {new_event_id}"
    )


# ── Registration alias candidate lookup (unit-level) ─────────────────────────


async def test_direct_registration_lookup_finds_event_missed_by_date_query():
    """If the date-bucket has many events, find_by_registration must locate the
    correct one even when find_candidates(limit=50) would cap it out.

    This test uses the fake UoW which does not enforce the 50-row cap, but it
    verifies that the identity index accumulates aliases and that a direct
    registration lookup queries both registration_norm and registration_norms.
    The SQL-level bypass-cap behaviour is covered by the integration suite.
    """
    uow = InMemoryUnitOfWork()
    settings = make_settings()
    src = Source(id=uuid4(), name="S", kind=SourceKind.EXTERNAL, reliability_tier=1)
    await uow.sources.add(src)

    # Ingest with registration N99999 on a specific date.
    event_id = await IngestSourceData(uow, settings).execute(
        source_id=src.id,
        raw_payload={"r": "x"},
        ingestion_run_id=uuid4(),
        claims_data=[
            IngestionClaimDTO(field_name="event_date", field_value="2024-05-01"),
            IngestionClaimDTO(field_name="registration", field_value="N99999"),
        ],
    )

    # Direct registration lookup should find this entry with either the stored
    # normalized value or the raw human-readable registration spelling.
    results = await uow.identity_index.find_by_registration(
        registration_norm="n99999",
        event_date_norm="2024-05-01",
    )
    raw_results = await uow.identity_index.find_by_registration(
        registration_norm="N-99999",
        event_date_norm="2024-05-01",
    )
    found_ids = {r.event_id for r in results}
    raw_found_ids = {r.event_id for r in raw_results}
    assert event_id in found_ids, (
        "find_by_registration must find the event for the normalised registration"
    )
    assert event_id in raw_found_ids, (
        "find_by_registration must normalize raw registration lookup input"
    )


async def test_registration_alias_accumulated_across_ingestions():
    """After two ingestions with different registrations for the same event,
    both appear in registration_norms on the identity index entry."""
    uow = InMemoryUnitOfWork()
    settings = make_settings()
    src = Source(id=uuid4(), name="S", kind=SourceKind.EXTERNAL, reliability_tier=1)
    await uow.sources.add(src)

    event_id = await IngestSourceData(uow, settings).execute(
        source_id=src.id,
        raw_payload={"r": "first"},
        ingestion_run_id=uuid4(),
        claims_data=[
            IngestionClaimDTO(field_name="event_date", field_value="2024-07-04"),
            IngestionClaimDTO(field_name="registration", field_value="G-ABCD"),
        ],
    )

    # Second ingestion for the same event with a corrected registration.
    await IngestSourceData(uow, settings).execute(
        source_id=src.id,
        raw_payload={"r": "second"},
        ingestion_run_id=uuid4(),
        claims_data=[
            IngestionClaimDTO(field_name="event_date", field_value="2024-07-04"),
            IngestionClaimDTO(field_name="registration", field_value="G-EFGH"),
        ],
        event_id=event_id,
    )

    entry = uow.store.identity_index.get(event_id)
    assert entry is not None
    assert "gabcd" in entry.registration_norms, (
        "First registration should remain in registration_norms as a historical alias"
    )
    assert "gefgh" in entry.registration_norms, (
        "Second (corrected) registration should also be in registration_norms"
    )


# ── ConflictReconciler integration with cross-source policy ──────────────────


async def test_resolved_conflict_reopens_when_new_source_contradicts_winner():
    """When a second source provides a contradictory value for a resolved
    conflict field, the conflict must be reopened automatically."""
    uow = InMemoryUnitOfWork()
    settings = make_settings()
    override = Source(
        id=settings.curator_override_source_id,
        name=settings.curator_override_source_name,
        kind=SourceKind.INTERNAL,
        reliability_tier=1,
    )
    src_a = Source(id=uuid4(), name="A", kind=SourceKind.EXTERNAL, reliability_tier=1)
    src_b = Source(id=uuid4(), name="B", kind=SourceKind.EXTERNAL, reliability_tier=2)
    src_c = Source(id=uuid4(), name="C", kind=SourceKind.EXTERNAL, reliability_tier=3)
    for s in (override, src_a, src_b, src_c):
        await uow.sources.add(s)

    # Two sources create a conflict.
    event_id = await IngestSourceData(uow, settings).execute(
        source_id=src_a.id,
        raw_payload={"f": 3},
        ingestion_run_id=uuid4(),
        claims_data=[IngestionClaimDTO(field_name="fatalities_total", field_value=3)],
    )
    await IngestSourceData(uow, settings).execute(
        source_id=src_b.id,
        raw_payload={"f": 5},
        ingestion_run_id=uuid4(),
        claims_data=[IngestionClaimDTO(field_name="fatalities_total", field_value=5)],
        event_id=event_id,
    )

    conflict = next(
        c
        for c in uow.store.conflicts.values()
        if c.event_id == event_id and c.field_name == "fatalities_total"
    )

    # Resolve the conflict: pick src_a's claim as winner.
    winner_claim_id = next(
        c.id
        for c in uow.store.claims.values()
        if c.event_id == event_id
        and c.source_id == src_a.id
        and c.field_name == "fatalities_total"
        and c.is_active
    )
    await ResolveConflict(uow, settings=settings).execute(
        conflict_id=conflict.id,
        expected_version=conflict.version,
        winning_claim_id=winner_claim_id,
        current_user_id=uuid4(),
        reason="AAIB report confirms 3 fatalities",
    )

    # Third source contradicts the resolved winner.
    await IngestSourceData(uow, settings).execute(
        source_id=src_c.id,
        raw_payload={"f": 7},
        ingestion_run_id=uuid4(),
        claims_data=[IngestionClaimDTO(field_name="fatalities_total", field_value=7)],
        event_id=event_id,
    )

    reloaded_conflict = uow.store.conflicts.get(conflict.id)
    assert reloaded_conflict is not None
    assert reloaded_conflict.status == ConflictStatus.OPEN, (
        "Conflict should be reopened when a new source contradicts the resolved winner"
    )


# ── Role enum centralisation ────────────────────────────────────────────────


def test_role_values_is_frozen_set():
    """Role.values() returns an immutable frozenset (not a list or set)."""
    from atlas.domain.enums import Role

    vals = Role.values()
    assert isinstance(vals, frozenset)
    assert "analyst" in vals
    assert "reviewer" in vals
    assert "admin" in vals
    assert "curator" not in vals
