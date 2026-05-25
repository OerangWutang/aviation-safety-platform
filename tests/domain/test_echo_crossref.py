"""Unit tests for the Echo cross-reference domain core (profile + matcher).

Pure and deterministic - no corpus, no database.
"""

from __future__ import annotations

from atlas.domain.crossref.entities import (
    EvidenceSupport,
    HazardProfile,
    PrecedentMatch,
    PrecedentRecord,
)
from atlas.domain.crossref.profile import (
    build_hazard_profile,
    categories_from_finding_items,
    category_key,
    normalize_severity,
    normalize_terms,
)
from atlas.domain.services.echo_matcher import (
    MatcherWeights,
    PrecedentMatcher,
    SupportThresholds,
)

# ----------------------------- profile --------------------------------------- #


def test_normalize_terms_drops_stopwords_and_short_tokens():
    terms = normalize_terms("The pilot lost directional control on the runway")
    assert "directional" in terms and "control" in terms and "runway" in terms
    assert "the" not in terms  # stopword
    assert "pilot" not in terms  # aviation filler stopword
    assert "on" not in terms  # short/stopword


def test_normalize_terms_is_deterministic_set():
    assert normalize_terms("crosswind crosswind landing") == frozenset({"crosswind", "landing"})


def test_category_key_and_extraction():
    assert category_key("01", "06") == "01.06"
    assert category_key("02", None) == "02"
    assert category_key("", "") is None
    items = [
        {"category_no": "01", "subcategory_no": "06"},
        {"category_no": "02", "subcategory_no": "04"},
    ]
    assert categories_from_finding_items(items) == frozenset({"01.06", "02.04"})


def test_normalize_severity_bands():
    assert normalize_severity("Fatal") == "fatal"
    assert normalize_severity("Serious injury") == "serious"
    assert normalize_severity("None") == "none"
    assert normalize_severity("") is None


def test_build_profile_excludes_raw_narrative():
    p = build_hazard_profile(
        scrubbed_narrative="veered off the runway", far_part="Part 91: General Aviation"
    )
    # Only derived tokens survive; the profile has no narrative attribute at all.
    assert not hasattr(p, "narrative")
    assert "runway" in p.terms and "veered" in p.terms
    assert p.far_part == "Part 91: General Aviation"


# ----------------------------- matcher --------------------------------------- #


def _profile() -> HazardProfile:
    return build_hazard_profile(
        finding_categories=["01.06"],
        far_part="Part 91: General Aviation",
        severity="none",
        scrubbed_narrative="lost directional control on landing and veered off the runway",
    )


def test_empty_profile_yields_no_matches():
    assert PrecedentMatcher().rank(HazardProfile(), [PrecedentRecord(event_id="x")]) == []


def test_strong_match_ranks_above_weak():
    strong = PrecedentRecord(
        event_id="strong",
        finding_categories=frozenset({"01.06"}),
        far_part="Part 91: General Aviation",
        severity="none",
        terms=normalize_terms("lost directional control on landing and veered off the runway"),
    )
    weak = PrecedentRecord(
        event_id="weak",
        finding_categories=frozenset({"09.99"}),
        far_part="Part 121",
        severity="fatal",
        terms=normalize_terms("engine fire after takeoff"),
    )
    matches = PrecedentMatcher().rank(_profile(), [weak, strong])
    assert matches[0].event_id == "strong"
    assert matches[0].support == EvidenceSupport.STRONG
    assert matches[0].score >= matches[-1].score


def test_renormalises_when_profile_lacks_categories():
    # Profile with only lexical signal must still score on lexical alone.
    p = build_hazard_profile(scrubbed_narrative="bird strike shattered the windshield")
    rec = PrecedentRecord(
        event_id="r", terms=normalize_terms("a bird strike shattered the windshield")
    )
    [m] = PrecedentMatcher().rank(p, [rec])
    assert m.score > 0.9  # pure lexical, near-identical
    assert {c.name for c in m.components} == {"lexical"}


def test_deterministic_tiebreak_by_event_id():
    rec_a = PrecedentRecord(event_id="aaa", terms=normalize_terms("runway landing control"))
    rec_b = PrecedentRecord(event_id="bbb", terms=normalize_terms("runway landing control"))
    matches = PrecedentMatcher().rank(_profile(), [rec_b, rec_a])
    assert [m.event_id for m in matches] == ["aaa", "bbb"]  # equal score -> id asc


def test_min_support_filter():
    rec = PrecedentRecord(
        event_id="r", terms=normalize_terms("totally unrelated maintenance paperwork")
    )
    # No overlap with the profile -> filtered out at WEAK floor.
    assert PrecedentMatcher().rank(_profile(), [rec], min_support=EvidenceSupport.WEAK) == []


def test_match_has_no_probability_and_bounded_scores():
    rec = PrecedentRecord(
        event_id="r",
        finding_categories=frozenset({"01.06"}),
        terms=normalize_terms("directional control runway"),
    )
    [m] = PrecedentMatcher().rank(_profile(), [rec])
    # Epistemic guard: no dedicated probability field (display_probable_cause
    # is a public text passthrough, not a numeric probability estimate).
    prob_fields = [
        f
        for f in PrecedentMatch.__dataclass_fields__
        if f in {"probability", "recurrence_probability", "risk_probability"}
    ]
    assert prob_fields == [], "PrecedentMatch must not carry a numeric probability field"
    assert 0.0 <= m.score <= 1.0
    assert all(0.0 <= c.score <= 1.0 for c in m.components)


def test_thresholds_band_mapping():
    t = SupportThresholds()
    assert t.band(0.7) == EvidenceSupport.STRONG
    assert t.band(0.4) == EvidenceSupport.MODERATE
    assert t.band(0.2) == EvidenceSupport.WEAK
    assert t.band(0.05) == EvidenceSupport.NONE


def test_custom_weights_change_ranking():
    # Two records: one matches categories only, one matches lexical only.
    cat_only = PrecedentRecord(event_id="cat", finding_categories=frozenset({"01.06"}))
    lex_only = PrecedentRecord(
        event_id="lex", terms=normalize_terms("directional control landing runway veered")
    )
    p = _profile()
    cat_heavy = PrecedentMatcher(
        MatcherWeights(finding_categories=0.9, attributes=0.05, lexical=0.05)
    )
    lex_heavy = PrecedentMatcher(
        MatcherWeights(finding_categories=0.05, attributes=0.05, lexical=0.9)
    )
    assert cat_heavy.rank(p, [cat_only, lex_only])[0].event_id == "cat"
    assert lex_heavy.rank(p, [cat_only, lex_only])[0].event_id == "lex"
