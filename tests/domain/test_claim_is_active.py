from uuid import uuid4

from atlas.domain.entities import Claim
from atlas.domain.enums import ClaimType


def make_claim(claim_type: ClaimType) -> Claim:
    return Claim(
        event_id=uuid4(),
        source_id=uuid4(),
        field_name="fatalities_total",
        field_value=5,
        claim_type=claim_type,
    )


def test_raw_claim_is_active():
    assert make_claim(ClaimType.RAW).is_active is True


def test_confirmed_claim_is_active():
    assert make_claim(ClaimType.CONFIRMED).is_active is True


def test_manual_override_claim_is_active():
    assert make_claim(ClaimType.MANUAL_OVERRIDE).is_active is True


def test_superseded_claim_is_not_active():
    assert make_claim(ClaimType.SUPERSEDED).is_active is False


def test_active_values_returns_frozenset():
    assert isinstance(ClaimType.active_values(), frozenset)


def test_active_values_excludes_superseded():
    assert ClaimType.SUPERSEDED.value not in ClaimType.active_values()


def test_active_values_includes_all_non_superseded():
    expected = frozenset(
        {
            ClaimType.RAW.value,
            ClaimType.CONFIRMED.value,
            ClaimType.MANUAL_OVERRIDE.value,
        }
    )
    assert ClaimType.active_values() == expected


def test_supersede_marks_claim_inactive():
    claim = make_claim(ClaimType.RAW)
    replacement_id = uuid4()

    claim.supersede(replacement_id)

    assert claim.claim_type == ClaimType.SUPERSEDED
    assert claim.superseded_by_claim_id == replacement_id
    assert claim.is_active is False
    assert claim.can_win() is False


def test_is_active_and_can_win_are_intentionally_separate_concepts():
    claim = make_claim(ClaimType.RAW)

    assert claim.is_active == (claim.claim_type != ClaimType.SUPERSEDED)
    assert claim.can_win() == (
        claim.claim_type
        in {
            ClaimType.RAW,
            ClaimType.CONFIRMED,
            ClaimType.MANUAL_OVERRIDE,
        }
    )
