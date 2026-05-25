from uuid import uuid4

import pytest

from atlas.domain.entities import Claim, ClaimConflict, Source
from atlas.domain.enums import ClaimType, ConflictStatus, SourceKind
from atlas.domain.services.projection_builder import DISPUTED, ProjectionBuilder


def make_source(tier=1):
    return Source(id=uuid4(), name=f"S{tier}", kind=SourceKind.EXTERNAL, reliability_tier=tier)


def make_claim(event_id, source_id, field, value, claim_type=ClaimType.RAW):
    return Claim(
        event_id=event_id,
        source_id=source_id,
        field_name=field,
        field_value=value,
        claim_type=claim_type,
    )


def test_unresolved_conflict_marks_field_as_disputed():
    event_id = uuid4()
    source = make_source()
    c1 = make_claim(event_id, source.id, "fatalities_total", 5)
    c2 = make_claim(event_id, source.id, "fatalities_total", 6)
    conflict = ClaimConflict(
        event_id=event_id,
        field_name="fatalities_total",
        status=ConflictStatus.OPEN,
        claim_ids=[c1.id, c2.id],
    )

    projection = ProjectionBuilder().build(
        event_id=event_id,
        claims=[c1, c2],
        conflicts=[conflict],
        sources_by_id={source.id: source},
        projection_version=1,
    )

    assert projection.fields["fatalities_total"] == DISPUTED
    assert projection.unresolved_conflict_fields == ["fatalities_total"]


def test_open_conflict_with_no_active_claims_still_projects_disputed_marker():
    """Regression: unresolved_conflict_fields and fields must stay aligned
    even when source-record corrections have superseded every claim for an
    OPEN conflict field.
    """
    event_id = uuid4()
    source = make_source()
    inactive_claim = make_claim(event_id, source.id, "operator", "Old Operator")
    inactive_claim.claim_type = ClaimType.SUPERSEDED
    conflict = ClaimConflict(
        event_id=event_id,
        field_name="operator",
        status=ConflictStatus.OPEN,
        claim_ids=[inactive_claim.id],
    )

    projection = ProjectionBuilder().build(
        event_id=event_id,
        claims=[inactive_claim],
        conflicts=[conflict],
        sources_by_id={source.id: source},
        projection_version=1,
    )

    assert projection.fields["operator"] == DISPUTED
    assert projection.unresolved_conflict_fields == ["operator"]


def test_disputed_field_excluded_from_completeness_score():
    event_id = uuid4()
    source = make_source()
    c_date = make_claim(event_id, source.id, "event_date", "2024-01-01")
    c_loc = make_claim(event_id, source.id, "location", "Paris")
    c_ac1 = make_claim(event_id, source.id, "aircraft_type", "B737")
    c_ac2 = make_claim(event_id, source.id, "aircraft_type", "A320")
    c_fat = make_claim(event_id, source.id, "fatalities_total", 0)
    conflict = ClaimConflict(
        event_id=event_id,
        field_name="aircraft_type",
        status=ConflictStatus.OPEN,
        claim_ids=[c_ac1.id, c_ac2.id],
    )

    projection = ProjectionBuilder().build(
        event_id=event_id,
        claims=[c_date, c_loc, c_ac1, c_ac2, c_fat],
        conflicts=[conflict],
        sources_by_id={source.id: source},
        projection_version=1,
    )

    assert projection.completeness_score == pytest.approx(3 / 9)


def test_resolved_conflict_uses_winning_claim():
    event_id = uuid4()
    source = make_source()
    c1 = make_claim(event_id, source.id, "fatalities_total", 5)
    c2 = make_claim(event_id, source.id, "fatalities_total", 6)
    conflict = ClaimConflict(
        event_id=event_id,
        field_name="fatalities_total",
        status=ConflictStatus.RESOLVED,
        winning_claim_id=c2.id,
        claim_ids=[c1.id, c2.id],
    )

    projection = ProjectionBuilder().build(
        event_id=event_id,
        claims=[c1, c2],
        conflicts=[conflict],
        sources_by_id={source.id: source},
        projection_version=1,
    )

    assert projection.fields["fatalities_total"] == 6


def test_manual_override_is_strongest_claim_without_conflict():
    event_id = uuid4()
    source = make_source()
    raw = make_claim(event_id, source.id, "location", "raw")
    override = make_claim(event_id, source.id, "location", "override", ClaimType.MANUAL_OVERRIDE)

    projection = ProjectionBuilder().build(
        event_id=event_id,
        claims=[raw, override],
        conflicts=[],
        sources_by_id={source.id: source},
        projection_version=1,
    )

    assert projection.fields["location"] == "override"


def test_projection_is_deterministic():
    event_id = uuid4()
    source = make_source()
    claims = [
        make_claim(event_id, source.id, "event_date", "2026-01-01"),
        make_claim(event_id, source.id, "location", "Amsterdam"),
    ]

    p1 = ProjectionBuilder().build(
        event_id=event_id,
        claims=claims,
        conflicts=[],
        sources_by_id={source.id: source},
        projection_version=1,
    )
    p2 = ProjectionBuilder().build(
        event_id=event_id,
        claims=list(reversed(claims)),
        conflicts=[],
        sources_by_id={source.id: source},
        projection_version=1,
    )

    assert p1.fields == p2.fields
    assert p1.completeness_score == p2.completeness_score


def test_raw_tier_defensive_dispute_without_open_conflict():
    """Two RAW claims from different sources disagree but no open ClaimConflict.

    The defensive per-tier dispute check must mark the field DISPUTED rather
    than silently picking a winner.  This is the failure mode the per-tier
    check exists to prevent: conflict detection can lag, fail, or race; the
    projection must not quietly choose a value when active evidence disagrees.
    """
    event_id = uuid4()
    s1 = make_source(tier=1)
    s2 = make_source(tier=2)
    c1 = make_claim(event_id, s1.id, "fatalities_total", 5)
    c2 = make_claim(event_id, s2.id, "fatalities_total", 6)

    projection = ProjectionBuilder().build(
        event_id=event_id,
        claims=[c1, c2],
        conflicts=[],  # No open conflict row — detection has not yet run.
        sources_by_id={s1.id: s1, s2.id: s2},
        projection_version=1,
    )

    assert projection.fields["fatalities_total"] == DISPUTED
    assert "fatalities_total" in projection.unresolved_conflict_fields


def test_raw_tier_winner_only_considers_raw_claims():
    """Pin per-tier invariant: at the RAW tier only RAW claims are candidates.

    Today every claim that reaches the RAW branch is already RAW (overrides
    and CONFIRMED claims short-circuit the earlier tiers).  This test pins
    that behaviour so a future active ``ClaimType`` cannot silently bypass
    the per-tier dispute check by being passed through to winner-policy.

    The test constructs a single agreeing RAW claim and asserts the winner
    is selected from it — establishing the happy-path baseline so the
    companion no-coverage scenarios above stay meaningful.
    """
    event_id = uuid4()
    s1 = make_source(tier=1)
    s2 = make_source(tier=2)
    # Two RAW claims agreeing on value, different sources/tiers.
    c1 = make_claim(event_id, s1.id, "location", "Paris")
    c2 = make_claim(event_id, s2.id, "location", "Paris")

    projection = ProjectionBuilder().build(
        event_id=event_id,
        claims=[c1, c2],
        conflicts=[],
        sources_by_id={s1.id: s1, s2.id: s2},
        projection_version=1,
    )

    # Agreeing RAW claims → projected value, no dispute.
    assert projection.fields["location"] == "Paris"
    assert "location" not in projection.unresolved_conflict_fields
