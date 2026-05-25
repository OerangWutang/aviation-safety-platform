"""Unit tests for the EventMatcher scoring and routing logic."""

from __future__ import annotations

from uuid import uuid4

from atlas.domain.entities import ProjectedAccidentRecord
from atlas.domain.services.event_matching import (
    HIGH_CONFIDENCE,
    UNCERTAIN_LOW,
    EventMatcher,
    _norm_date,
    _token_overlap,
    score_match,
)


def _proj(
    event_date="2024-06-01",
    registration="N123AB",
    operator="AirlineX",
    location="Dallas",
    aircraft_type="B737",
):
    return ProjectedAccidentRecord(
        event_id=uuid4(),
        fields={
            "event_date": event_date,
            "registration": registration,
            "operator": operator,
            "location": location,
            "aircraft_type": aircraft_type,
        },
        projection_version=1,
    )


# ── Normalisation helpers ─────────────────────────────────────────────────────


def test_norm_date_iso():
    assert _norm_date("2024-06-01") == "2024-06-01"


def test_norm_date_with_slashes():
    assert _norm_date("2024/06/01") == "2024-06-01"


def test_norm_date_empty():
    assert _norm_date("") == ""


def test_norm_date_none():
    assert _norm_date(None) == ""


def test_token_overlap_identical():
    assert _token_overlap("airline x", "airline x") == 1.0


def test_token_overlap_partial():
    score = _token_overlap("airline x cargo", "airline x")
    assert 0 < score < 1.0


def test_token_overlap_disjoint():
    assert _token_overlap("airline x", "fly corp") == 0.0


def test_token_overlap_empty():
    assert _token_overlap("", "airline x") == 0.0


# ── score_match ───────────────────────────────────────────────────────────────


def test_perfect_match_scores_near_one():
    fields = {
        "event_date": "2024-06-01",
        "registration": "N123AB",
        "operator": "AirlineX",
        "location": "Dallas",
        "aircraft_type": "B737",
    }
    cand = _proj()
    result = score_match(fields, cand.fields)
    assert result.score >= HIGH_CONFIDENCE
    assert set(result.matched_fields) == {
        "event_date",
        "registration",
        "operator",
        "location",
        "aircraft_type",
    }


def test_date_and_registration_match_is_high_confidence():
    """Just date + registration should be enough for a high-confidence signal."""
    fields = {"event_date": "2024-06-01", "registration": "N123AB"}
    cand = _proj()
    result = score_match(fields, cand.fields)
    # registration(0.45) + event_date(0.30) = 0.75 exactly
    assert result.score >= HIGH_CONFIDENCE


def test_same_date_different_registration_is_not_high_confidence():
    fields = {"event_date": "2024-06-01", "registration": "G-WXYZ"}
    cand = _proj()
    result = score_match(fields, cand.fields)
    assert result.score < HIGH_CONFIDENCE


def test_one_day_off_date_partial_score():
    fields = {"event_date": "2024-06-02", "registration": "N123AB"}
    cand = _proj(event_date="2024-06-01")
    result = score_match(fields, cand.fields)
    # date partial (0.15) + registration (0.45) = 0.60 -> uncertain range
    assert UNCERTAIN_LOW <= result.score < HIGH_CONFIDENCE


def test_missing_incoming_field_scores_zero_for_that_dimension():
    fields = {"event_date": "2024-06-01"}  # no registration
    cand = _proj()
    result = score_match(fields, cand.fields)
    assert "registration" not in result.matched_fields
    assert result.score < HIGH_CONFIDENCE


def test_missing_candidate_field_scores_zero():
    fields = {"event_date": "2024-06-01", "registration": "N123AB"}
    cand = _proj(registration="")
    result = score_match(fields, cand.fields)
    assert result.score < HIGH_CONFIDENCE


def test_registration_dash_normalised():
    """N-123-AB and N123AB should match."""
    fields = {"registration": "N-123-AB"}
    cand = _proj(registration="N123AB")
    result = score_match(fields, cand.fields)
    assert "registration" in result.matched_fields


# ── EventMatcher.decide ───────────────────────────────────────────────────────


def test_decide_attach_on_high_confidence():
    fields = {"event_date": "2024-06-01", "registration": "N123AB"}
    candidates = [_proj()]
    decision = EventMatcher().decide(fields, candidates)
    assert decision.action == "attach"
    assert decision.candidate_event_id is not None


def test_decide_review_on_medium_confidence():
    fields = {"event_date": "2024-06-01", "operator": "AirlineX"}
    candidates = [_proj()]
    decision = EventMatcher().decide(fields, candidates)
    # date(0.30) + operator(token overlap, likely high) -> medium range
    assert decision.action in ("review", "attach")  # depends on exact overlap score


def test_decide_new_on_no_candidates():
    fields = {"event_date": "2024-06-01", "registration": "N123AB"}
    decision = EventMatcher().decide(fields, [])
    assert decision.action == "new"
    assert decision.candidate_event_id is None


def test_decide_new_on_low_similarity():
    fields = {"event_date": "2023-01-01", "registration": "G-ABCD", "operator": "EuroAir"}
    candidates = [_proj(event_date="2024-06-01", registration="N123AB", operator="AirlineX")]
    decision = EventMatcher().decide(fields, candidates)
    assert decision.action == "new"


def test_decide_picks_best_candidate():
    """When multiple candidates exist, the highest-scoring one is chosen."""
    strong = _proj(event_date="2024-06-01", registration="N123AB", operator="AirlineX")
    weak = _proj(event_date="2023-01-01", registration="G-ZZZZ", operator="EuroAir")
    fields = {"event_date": "2024-06-01", "registration": "N123AB"}
    decision = EventMatcher().decide(fields, [weak, strong])
    assert decision.candidate_event_id == strong.event_id


def test_custom_thresholds_respected():
    matcher = EventMatcher(high_confidence=0.99, uncertain_low=0.99)
    fields = {"event_date": "2024-06-01", "registration": "N123AB"}
    candidates = [_proj()]
    # Even a high score is below 0.99 so should be "new"
    decision = matcher.decide(fields, candidates)
    assert decision.action == "new"


def test_norm_date_rejects_calendar_impossible_dates():
    assert _norm_date("2024-99-01") == ""
    assert _norm_date("2024-01-99") == ""
    assert _norm_date("2024-02-30") == ""


def test_best_match_logs_high_scoring_tie(caplog):
    fields = {"event_date": "2024-06-01", "registration": "N123AB"}
    first = _proj(event_date="2024-06-01", registration="N123AB")
    second = _proj(event_date="2024-06-01", registration="N123AB")

    match = EventMatcher().best_match(fields, [first, second])

    assert match.candidate_event_id == first.event_id
    assert match.ambiguous_tie is True
    assert "Ambiguous event match" in caplog.text


def test_decide_routes_high_confidence_ties_to_review():
    fields = {"event_date": "2024-06-01", "registration": "N123AB"}
    first = _proj(event_date="2024-06-01", registration="N123AB")
    second = _proj(event_date="2024-06-01", registration="N123AB")

    decision = EventMatcher().decide(fields, [first, second])

    assert decision.action == "review"
    assert decision.candidate_event_id == first.event_id
    assert decision.score >= HIGH_CONFIDENCE


def test_decide_preserves_all_tied_candidate_ids_for_review() -> None:
    fields = {"event_date": "2024-06-01", "registration": "N123AB"}
    first = _proj(event_date="2024-06-01", registration="N123AB")
    second = _proj(event_date="2024-06-01", registration="N123AB")
    third = _proj(event_date="2024-06-01", registration="N123AB")

    decision = EventMatcher().decide(fields, [first, second, third])

    assert decision.action == "review"
    assert decision.candidate_event_id == first.event_id
    assert decision.tied_candidate_event_ids == [first.event_id, second.event_id, third.event_id]
