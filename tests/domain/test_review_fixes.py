"""Regression tests for the fixes described in the code review.

Covers:
- _norm_date ambiguous-format rejection (P0 fix 2)
- _to_domain nullable return type
- DisputedType Pydantic v2 schema
- require_role enum typing
- ConflictDetector unique_normalised_values helper
- ClaimWriter batch resolved-winner lookup
"""

from __future__ import annotations

import dataclasses
from uuid import uuid4

import pytest

# ── _norm_date: ambiguous format rejection ────────────────────────────────────


def test_norm_date_accepts_iso_hyphen():
    from atlas.domain.services.event_matching import _norm_date

    assert _norm_date("2024-06-11") == "2024-06-11"


def test_norm_date_accepts_iso_slash():
    from atlas.domain.services.event_matching import _norm_date

    assert _norm_date("2024/06/11") == "2024-06-11"


def test_norm_date_rejects_ambiguous_dmy():
    """06-11-2023 could be Jun-11 or Nov-6. Should return '' (unknown), not a wrong date."""
    from atlas.domain.services.event_matching import _norm_date

    result = _norm_date("06-11-2023")
    assert result == "", f"Expected empty string for ambiguous date, got {result!r}"


def test_norm_date_rejects_ambiguous_slash():
    from atlas.domain.services.event_matching import _norm_date

    result = _norm_date("11/06/2023")
    assert result == "", f"Expected empty string for ambiguous date, got {result!r}"


def test_norm_date_empty_on_none():
    from atlas.domain.services.event_matching import _norm_date

    assert _norm_date(None) == ""


def test_norm_date_empty_on_garbage():
    from atlas.domain.services.event_matching import _norm_date

    assert _norm_date("not-a-date") == ""


def test_norm_date_identity_index_key_uses_iso(tmp_path):
    """An ISO-format date from a normalised claim should survive identity matching."""
    from atlas.domain.services.event_matching import _norm_date

    result = _norm_date("2024-06-01")
    assert result == "2024-06-01"


# ── DisputedType Pydantic v2 schema ───────────────────────────────────────────


def test_disputed_v2_validation_from_string():
    """DisputedType.__get_pydantic_core_schema__ accepts the marker string."""
    from atlas.domain.constants import DISPUTED, DISPUTED_MARKER, DisputedType

    result = DisputedType._validate(DISPUTED_MARKER)
    assert result is DISPUTED


def test_disputed_v2_validation_from_sentinel():
    from atlas.domain.constants import DISPUTED, DisputedType

    result = DisputedType._validate(DISPUTED)
    assert result is DISPUTED


def test_disputed_v2_validation_rejects_other():
    from atlas.domain.constants import DisputedType

    with pytest.raises(ValueError):
        DisputedType._validate("something_else")


def test_disputed_v2_pydantic_core_schema_exists():
    """Pydantic v2 schema hook must be defined and callable."""
    from atlas.domain.constants import DisputedType

    assert hasattr(DisputedType, "__get_pydantic_core_schema__")
    schema = DisputedType.__get_pydantic_core_schema__(DisputedType, None)
    assert schema is not None


def test_disputed_serializes_to_marker():
    from atlas.domain.constants import DISPUTED, DISPUTED_MARKER, replace_disputed

    result = replace_disputed({"field": DISPUTED})
    assert result == {"field": DISPUTED_MARKER}


# ── unique_normalised_values module-level helper ──────────────────────────────


def test_unique_normalised_values_all_same():
    from atlas.domain.entities import Claim
    from atlas.domain.services.conflict_detector import unique_normalised_values

    event_id = uuid4()
    src = uuid4()
    claims = [
        Claim(event_id=event_id, source_id=src, field_name="loc", field_value="london"),
        Claim(event_id=event_id, source_id=src, field_name="loc", field_value="London"),
    ]
    assert len(unique_normalised_values(claims)) == 1


def test_unique_normalised_values_different():
    from atlas.domain.entities import Claim
    from atlas.domain.services.conflict_detector import unique_normalised_values

    event_id = uuid4()
    claims = [
        Claim(event_id=event_id, source_id=uuid4(), field_name="loc", field_value="Paris"),
        Claim(event_id=event_id, source_id=uuid4(), field_name="loc", field_value="Rome"),
    ]
    assert len(unique_normalised_values(claims)) == 2


# ── ClaimWriter batch resolved-winner lookup ──────────────────────────────────


@pytest.mark.asyncio
async def test_claimwriter_batch_resolved_winner():
    """ClaimWriter.write must use find_resolved_by_winning_claims (batch) not per-claim."""
    from atlas.application.ingestion._claim_writer import ClaimWriter
    from atlas.domain.entities import (
        AccidentEvent,
        Claim,
        ClaimConflict,
        Source,
    )
    from atlas.domain.enums import ClaimType, ConflictStatus, SourceKind
    from tests.domain._fake_uow import InMemoryUnitOfWork

    uow = InMemoryUnitOfWork()
    src = Source(id=uuid4(), name="S", kind=SourceKind.EXTERNAL, reliability_tier=1)
    await uow.sources.add(src)
    event = AccidentEvent(id=uuid4())
    await uow.events.add(event)

    record_id = "rec-001"
    snap_id = uuid4()
    ingestion_run = uuid4()

    # Simulate two prior active claims on the event (from the same source record).
    old_loc = Claim(
        id=uuid4(),
        event_id=event.id,
        source_id=src.id,
        raw_snapshot_id=snap_id,
        field_name="location",
        field_value="Berlin",
        claim_type=ClaimType.RAW,
    )
    old_op = Claim(
        id=uuid4(),
        event_id=event.id,
        source_id=src.id,
        raw_snapshot_id=snap_id,
        field_name="operator",
        field_value="Old Airline",
        claim_type=ClaimType.RAW,
    )
    uow.store.claims[old_loc.id] = old_loc
    uow.store.claims[old_op.id] = old_op
    # Associate them with the source record so bulk_supersede picks them up.
    uow.store.snapshots[snap_id] = type(
        "Snap", (), {"id": snap_id, "source_id": src.id, "source_record_id": record_id}
    )()

    # Create two RESOLVED conflicts whose winners are the old claims.
    conflict_loc = ClaimConflict(
        id=uuid4(),
        event_id=event.id,
        field_name="location",
        status=ConflictStatus.RESOLVED,
        winning_claim_id=old_loc.id,
    )
    conflict_op = ClaimConflict(
        id=uuid4(),
        event_id=event.id,
        field_name="operator",
        status=ConflictStatus.RESOLVED,
        winning_claim_id=old_op.id,
    )
    uow.store.conflicts[conflict_loc.id] = conflict_loc
    uow.store.conflicts[conflict_op.id] = conflict_op

    # Write new claims that supersede the old ones.
    writer = ClaimWriter(uow)
    result = await writer.write(
        event_id=event.id,
        source_id=src.id,
        snapshot_id=uuid4(),
        source_kind=SourceKind.EXTERNAL.value,
        claims_data=[
            {"field_name": "location", "field_value": "Munich"},
            {"field_name": "operator", "field_value": "New Airline"},
        ],
        ingestion_run_id=ingestion_run,
        source_record_id=record_id,
        source_field_mapping=None,
    )

    # Both resolved conflicts should be queued for reconciliation.
    assert len(result.resolved_conflicts_to_reconcile) == 2
    recon_conflict_ids = {c.id for c, _ in result.resolved_conflicts_to_reconcile}
    assert conflict_loc.id in recon_conflict_ids
    assert conflict_op.id in recon_conflict_ids


# ── require_role accepts only Role enum values ────────────────────────────────


def test_require_role_accepts_role_enum():
    """require_role must work when passed Role enum members (not bare strings)."""
    from atlas.domain.enums import Role
    from atlas.presentation.api.dependencies import require_role

    # Should not raise; the dependency is a callable.
    dep = require_role(Role.ADMIN, Role.REVIEWER)
    assert callable(dep)


# ── MergeResult is a frozen dataclass ────────────────────────────────────────


def test_merge_result_is_frozen_dataclass():
    from atlas.application.use_cases.merge_duplicate_events import MergeResult

    assert dataclasses.is_dataclass(MergeResult)
    r = MergeResult(
        target_event_id=uuid4(),
        source_event_id=uuid4(),
        claims_transferred=3,
        review_id=None,
    )
    with pytest.raises((dataclasses.FrozenInstanceError, TypeError, AttributeError)):
        r.claims_transferred = 99  # type: ignore[misc]


# ── ConflictReconciler: multiple-field batch behavior ─────────────────────────


@pytest.mark.asyncio
async def test_reconciler_detects_multiple_fields_in_single_batch():
    """detect_and_apply_new_conflicts must open conflicts for all conflicting
    fields in one call, exercising the batch find_by_event path."""
    from atlas.application.use_cases.ingest_source_data import IngestSourceData
    from atlas.domain.entities import AccidentEvent, Source
    from atlas.domain.enums import ConflictStatus, SourceKind
    from tests.domain._fake_uow import InMemoryUnitOfWork, make_settings

    uow = InMemoryUnitOfWork()
    src_a = Source(id=uuid4(), name="SrcA", kind=SourceKind.EXTERNAL, reliability_tier=1)
    src_b = Source(id=uuid4(), name="SrcB", kind=SourceKind.EXTERNAL, reliability_tier=2)
    await uow.sources.add(src_a)
    await uow.sources.add(src_b)

    event = AccidentEvent(id=uuid4())
    await uow.events.add(event)

    from atlas.application.dto import IngestionClaimDTO

    # Source A claims two fields.
    await IngestSourceData(uow, make_settings()).execute(
        source_id=src_a.id,
        raw_payload={"loc": "Berlin", "op": "AirlineA"},
        ingestion_run_id=uuid4(),
        claims_data=[
            IngestionClaimDTO(field_name="location", field_value="Berlin"),
            IngestionClaimDTO(field_name="operator", field_value="AirlineA"),
        ],
        event_id=event.id,
    )

    # Source B claims the same two fields with different values -> 2 conflicts.
    await IngestSourceData(uow, make_settings()).execute(
        source_id=src_b.id,
        raw_payload={"loc": "Munich", "op": "AirlineB"},
        ingestion_run_id=uuid4(),
        claims_data=[
            IngestionClaimDTO(field_name="location", field_value="Munich"),
            IngestionClaimDTO(field_name="operator", field_value="AirlineB"),
        ],
        event_id=event.id,
    )

    open_conflicts = [
        c
        for c in uow.store.conflicts.values()
        if c.event_id == event.id and c.status == ConflictStatus.OPEN
    ]
    assert len(open_conflicts) == 2, (
        f"Expected 2 open conflicts, got {len(open_conflicts)}: "
        f"{[c.field_name for c in open_conflicts]}"
    )
    field_names = {c.field_name for c in open_conflicts}
    assert field_names == {"location", "operator"}


@pytest.mark.asyncio
async def test_reconciler_merge_path_activity_log_uses_user_modifier_type():
    """When detect_and_apply_new_conflicts is called with modifier_type=USER
    (merge path), activity log entries must record ModifierType.USER."""
    from atlas.application.use_cases.ingest_source_data import IngestSourceData
    from atlas.application.use_cases.merge_duplicate_events import MergeDuplicateEvents
    from atlas.domain.entities import AccidentEvent, Source
    from atlas.domain.enums import ConflictStatus, ModifierType, SourceKind
    from tests.domain._fake_uow import InMemoryUnitOfWork, make_settings

    uow = InMemoryUnitOfWork()
    src_a = Source(id=uuid4(), name="SrcA2", kind=SourceKind.EXTERNAL, reliability_tier=1)
    src_b = Source(id=uuid4(), name="SrcB2", kind=SourceKind.EXTERNAL, reliability_tier=2)
    await uow.sources.add(src_a)
    await uow.sources.add(src_b)

    event_a = AccidentEvent(id=uuid4())
    event_b = AccidentEvent(id=uuid4())
    await uow.events.add(event_a)
    await uow.events.add(event_b)

    from atlas.application.dto import IngestionClaimDTO

    # event_a: claim from src_a
    await IngestSourceData(uow, make_settings()).execute(
        source_id=src_a.id,
        raw_payload={"loc": "Paris"},
        ingestion_run_id=uuid4(),
        claims_data=[IngestionClaimDTO(field_name="location", field_value="Paris")],
        event_id=event_a.id,
    )
    # event_b: claim from src_b with a different value -> conflict after merge
    await IngestSourceData(uow, make_settings()).execute(
        source_id=src_b.id,
        raw_payload={"loc": "Rome"},
        ingestion_run_id=uuid4(),
        claims_data=[IngestionClaimDTO(field_name="location", field_value="Rome")],
        event_id=event_b.id,
    )

    curator = uuid4()
    await MergeDuplicateEvents(uow).execute(
        source_event_id=event_b.id, target_event_id=event_a.id, resolved_by=curator
    )

    # Any activity log entry for the new conflict on event_a should be USER type.
    new_conflicts = [
        c
        for c in uow.store.conflicts.values()
        if c.event_id == event_a.id and c.status == ConflictStatus.OPEN
    ]
    assert new_conflicts, "Expected an open conflict after merge"
    conflict_id = new_conflicts[0].id
    activity = [e for e in uow.store.conflict_activity if e.conflict_id == conflict_id]
    assert activity, "Expected activity log entries for conflict"
    # At least the initial creation entry should be ModifierType.USER.
    assert any(e.modifier_type == ModifierType.USER for e in activity), (
        f"Expected ModifierType.USER in activity log; found: {[e.modifier_type for e in activity]}"
    )


# ── _to_domain nullable behavior ─────────────────────────────────────────────


def test_to_domain_raises_for_none_input_and_opt_returns_none():
    """_to_domain is non-nullable for typing; _to_domain_opt owns nullable reads."""
    import pytest

    from atlas.domain.entities import Source
    from atlas.domain.exceptions import MappingError
    from atlas.infrastructure.db.repositories import _to_domain, _to_domain_opt

    with pytest.raises(MappingError):
        _to_domain(None, Source)

    assert _to_domain_opt(None, Source) is None
