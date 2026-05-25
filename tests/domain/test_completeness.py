from atlas.domain.constants import DISPUTED
from atlas.domain.services.completeness import CompletenessCalculator


def test_score_is_float():
    score = CompletenessCalculator().score({"event_date": "2026-01-01"})
    assert isinstance(score, float)
    assert score == 1 / 9


def test_score_complete_record():
    score = CompletenessCalculator().score(
        {
            "event_date": "2026-01-01",
            "location": "Amsterdam",
            "aircraft_type": "B738",
            "fatalities_total": 0,
            "injuries_total": 0,
            "operator": "KLM",
            "registration": "PH-BXO",
            "flight_phase": "landing",
            "narrative": "No injuries.",
        }
    )
    assert score == 1.0


def test_disputed_field_does_not_count_toward_score():
    score = CompletenessCalculator().score({"event_date": DISPUTED})
    assert score == 0.0


def test_mixed_disputed_and_filled():
    score = CompletenessCalculator().score({"event_date": "2026-01-01", "location": DISPUTED})
    assert score == 1 / 9


def test_none_value_does_not_count():
    score = CompletenessCalculator().score({"event_date": None})
    assert score == 0.0
