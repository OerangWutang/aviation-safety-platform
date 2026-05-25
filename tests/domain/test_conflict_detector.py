from uuid import uuid4

from atlas.domain.entities import Claim
from atlas.domain.enums import ClaimType
from atlas.domain.services.conflict_detector import ConflictDetector, normalize_value


def make_claim(event_id, field_name, value, claim_type=ClaimType.RAW):
    return Claim(
        event_id=event_id,
        source_id=uuid4(),
        field_name=field_name,
        field_value=value,
        claim_type=claim_type,
    )


def test_normalize_int_float_string_are_equal():
    assert normalize_value(5) == normalize_value(5.0) == normalize_value("5")


def test_normalize_bool_not_coerced_to_int():
    assert normalize_value(True) != normalize_value(1)


def test_normalize_none_is_none():
    assert normalize_value(None) is None


def test_normalize_string_collapses_case_and_whitespace():
    assert normalize_value("  Amsterdam   Schiphol ") == normalize_value("amsterdam schiphol")


def test_conflict_detector_creates_conflict_when_active_claims_disagree():
    event_id = uuid4()
    claims = [
        make_claim(event_id, "fatalities_total", 5),
        make_claim(event_id, "fatalities_total", 6),
    ]

    conflicts = ConflictDetector().detect(claims)

    assert len(conflicts) == 1
    assert conflicts[0].field_name == "fatalities_total"
    assert set(conflicts[0].claim_ids) == {claims[0].id, claims[1].id}


def test_detect_no_conflict_for_int_vs_float_same_value():
    event_id = uuid4()
    claims = [
        make_claim(event_id, "fatalities_total", 5),
        make_claim(event_id, "fatalities_total", 5.0),
    ]
    assert ConflictDetector().detect(claims) == []


def test_detect_no_conflict_for_string_vs_int_same_value():
    event_id = uuid4()
    claims = [
        make_claim(event_id, "fatalities_total", "5"),
        make_claim(event_id, "fatalities_total", 5),
    ]
    assert ConflictDetector().detect(claims) == []


def test_conflict_detector_does_not_create_conflict_for_normalized_same_string():
    event_id = uuid4()
    claims = [
        make_claim(event_id, "location", "  Amsterdam   Schiphol "),
        make_claim(event_id, "location", "amsterdam schiphol"),
    ]
    assert ConflictDetector().detect(claims) == []


def test_conflict_detector_ignores_superseded_claims():
    event_id = uuid4()
    claims = [
        make_claim(event_id, "fatalities_total", 5),
        make_claim(event_id, "fatalities_total", 6, ClaimType.SUPERSEDED),
    ]
    assert ConflictDetector().detect(claims) == []


def test_conflict_detector_separates_events():
    claims = [
        make_claim(uuid4(), "fatalities_total", 5),
        make_claim(uuid4(), "fatalities_total", 6),
    ]
    assert ConflictDetector().detect(claims) == []
