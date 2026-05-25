"""Echo: rank public precedents against a hazard profile.

Pure, deterministic, explainable.  No embeddings, no network, no model calls in
v1 - the score is a transparent weighted blend of three signals an analyst can
re-derive by hand:

1. **Cause-taxonomy overlap** (the spine) - Jaccard over NTSB ``CC.SS`` keys.
2. **Structured-attribute agreement** - FAR part / aircraft category / severity.
3. **Lexical overlap** - overlap coefficient over scrubbed-narrative terms.

Only components for which the *profile* actually has data contribute, and the
weights are renormalised over those - so a hazard with no coded categories is
matched on attributes + text rather than being silently penalised.

The blended ``score`` is a similarity in ``[0, 1]`` and is banded into
:class:`EvidenceSupport`.  It is **not** a probability; see the entities module.
A semantic re-ranker can be layered on later behind :class:`PrecedentRanker`
without changing callers (see ``CROSSREF_ENGINE.md``).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Protocol

from atlas.domain.crossref.entities import (
    EvidenceSupport,
    HazardProfile,
    MatchComponent,
    PrecedentMatch,
    PrecedentRecord,
)


def _jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


def _overlap_coefficient(a: frozenset[str], b: frozenset[str]) -> float:
    # |A∩B| / min(|A|,|B|): robust when a short hazard meets a long narrative.
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


@dataclass(frozen=True)
class MatcherWeights:
    finding_categories: float = 0.5
    attributes: float = 0.2
    lexical: float = 0.3


@dataclass(frozen=True)
class SupportThresholds:
    """Similarity cutoffs for the coarse band.  Not probabilities."""

    strong: float = 0.60
    moderate: float = 0.35
    weak: float = 0.15

    def band(self, score: float) -> EvidenceSupport:
        if score >= self.strong:
            return EvidenceSupport.STRONG
        if score >= self.moderate:
            return EvidenceSupport.MODERATE
        if score >= self.weak:
            return EvidenceSupport.WEAK
        return EvidenceSupport.NONE


class PrecedentRanker(Protocol):
    """Seam for an alternative/secondary ranker (e.g. embedding similarity)."""

    def rank(
        self, profile: HazardProfile, records: Iterable[PrecedentRecord], *, limit: int
    ) -> list[PrecedentMatch]: ...


class PrecedentMatcher:
    """Deterministic structured + lexical precedent matcher (Echo v1)."""

    def __init__(
        self,
        weights: MatcherWeights | None = None,
        thresholds: SupportThresholds | None = None,
    ) -> None:
        self._w = weights or MatcherWeights()
        self._t = thresholds or SupportThresholds()

    def rank(
        self,
        profile: HazardProfile,
        records: Iterable[PrecedentRecord],
        *,
        limit: int = 20,
        min_support: EvidenceSupport = EvidenceSupport.WEAK,
    ) -> list[PrecedentMatch]:
        if profile.is_empty():
            return []
        floor = _support_rank(min_support)
        scored = [self.score_one(profile, r) for r in records]
        kept = [m for m in scored if _support_rank(m.support) >= floor and m.score > 0.0]
        # Deterministic order: score desc, then event_id asc as a stable tiebreak.
        kept.sort(key=lambda m: (-m.score, m.event_id))
        return kept[:limit]

    def score_one(self, profile: HazardProfile, record: PrecedentRecord) -> PrecedentMatch:
        components: list[MatchComponent] = []

        # 1. cause-taxonomy overlap
        if profile.finding_categories:
            shared_cats = profile.finding_categories & record.finding_categories
            cat_score = _jaccard(profile.finding_categories, record.finding_categories)
            components.append(
                MatchComponent(
                    name="finding_categories",
                    weight=self._w.finding_categories,
                    score=cat_score,
                    detail=(
                        f"{len(shared_cats)} shared cause categories"
                        if shared_cats
                        else "no shared cause categories"
                    ),
                )
            )
        else:
            shared_cats = frozenset()

        # 2. structured-attribute agreement (only attributes the profile asserts)
        attr_pairs = [
            ("far_part", profile.far_part, record.far_part),
            ("aircraft_category", profile.aircraft_category, record.aircraft_category),
            ("severity", profile.severity, record.severity),
        ]
        asserted = [(n, p, r) for (n, p, r) in attr_pairs if p is not None]
        if asserted:
            matches = [n for (n, p, r) in asserted if r is not None and p == r]
            attr_score = len(matches) / len(asserted)
            components.append(
                MatchComponent(
                    name="attributes",
                    weight=self._w.attributes,
                    score=attr_score,
                    detail=("matched: " + ", ".join(matches))
                    if matches
                    else "no attribute matches",
                )
            )

        # 3. lexical overlap
        if profile.terms:
            shared_terms = profile.terms & record.terms
            lex_score = _overlap_coefficient(profile.terms, record.terms)
            components.append(
                MatchComponent(
                    name="lexical",
                    weight=self._w.lexical,
                    score=lex_score,
                    detail=f"{len(shared_terms)} shared terms",
                )
            )
        else:
            shared_terms = frozenset()

        # Blend over applicable weights only (renormalise).
        applicable = sum(c.weight for c in components)
        score = sum(c.weight * c.score for c in components) / applicable if applicable > 0 else 0.0

        return PrecedentMatch(
            event_id=record.event_id,
            score=round(score, 4),
            support=self._t.band(score),
            components=tuple(components),
            shared_finding_categories=shared_cats,
            shared_terms=shared_terms,
            display_occurred_on=record.display_occurred_on,
            display_location=record.display_location,
            display_aircraft=record.display_aircraft,
            display_probable_cause=record.display_probable_cause,
        )


_SUPPORT_ORDER: Sequence[EvidenceSupport] = (
    EvidenceSupport.NONE,
    EvidenceSupport.WEAK,
    EvidenceSupport.MODERATE,
    EvidenceSupport.STRONG,
)


def _support_rank(support: EvidenceSupport) -> int:
    return _SUPPORT_ORDER.index(support)
