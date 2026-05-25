from datetime import UTC, datetime, timedelta
from uuid import uuid4

from atlas.domain.entities import Claim, Source
from atlas.domain.enums import ClaimType, SourceKind
from atlas.domain.services.winner_policy import WinnerPolicy


def source(tier):
    return Source(id=uuid4(), name=f"S{tier}", kind=SourceKind.EXTERNAL, reliability_tier=tier)


def claim(source_id, value, claim_type=ClaimType.RAW, created_at=None):
    return Claim(
        event_id=uuid4(),
        source_id=source_id,
        field_name="field",
        field_value=value,
        claim_type=claim_type,
        created_at=created_at or datetime.now(UTC),
    )


def test_manual_override_beats_confirmed_and_raw():
    s1 = source(1)
    s2 = source(1)
    raw = claim(s1.id, "raw", ClaimType.CONFIRMED)
    override = claim(s2.id, "override", ClaimType.MANUAL_OVERRIDE)

    winner = WinnerPolicy().choose_winner([raw, override], {s1.id: s1, s2.id: s2})

    assert winner == override


def test_lower_reliability_tier_wins_with_same_claim_type():
    trusted = source(1)
    weak = source(5)
    c1 = claim(trusted.id, "trusted")
    c2 = claim(weak.id, "weak")

    winner = WinnerPolicy().choose_winner([c2, c1], {trusted.id: trusted, weak.id: weak})

    assert winner == c1


def test_older_claim_breaks_tie():
    s = source(1)
    old = claim(s.id, "old", created_at=datetime.now(UTC) - timedelta(days=1))
    new = claim(s.id, "new", created_at=datetime.now(UTC))

    winner = WinnerPolicy().choose_winner([old, new], {s.id: s})

    assert winner == old
