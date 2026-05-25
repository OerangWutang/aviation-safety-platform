"""Event matching service: decides whether incoming claims describe a known accident.

Background
----------
When an ingestion request arrives without an ``event_id``, the system must
answer: "Is this the same real-world accident as an existing event, or is it
new?"  Getting this wrong in either direction has costly consequences:

- False negative (missed match): two events for the same accident, each with
  partial evidence.  The conflict engine will never see the contradiction
  because it only operates within one event's claim set.
- False positive (wrong match): claims from different accidents are merged
  under one event_id, potentially corrupting the record.

The matcher uses a weighted-field scoring approach.  Individual field scores
are clipped to [0, 1] and combined into a single ``score`` in [0, 1].

Thresholds
----------
- ``score >= HIGH_CONFIDENCE``  -> auto-attach to the matching event (no new
  event created, no curator review needed).
- ``UNCERTAIN_LOW <= score < HIGH_CONFIDENCE``  -> create new event + queue a
  ``PendingDuplicateReview`` for curator resolution.
- ``score < UNCERTAIN_LOW``  -> create new event, no review.

Field weights
-------------
``registration`` is weighted highest because an aircraft registration is nearly
unique per accident (the same registration rarely appears in two accidents on
the same date unless they are the same accident).  ``event_date`` is a strong
secondary signal.  Textual fields (``operator``, ``location``) are fuzzy and
weighted lower because their format varies across sources.

Normalisation
-------------
All values are lowercased and stripped before comparison.  Dates are normalised
to ``YYYY-MM-DD``.  A missing field on either side scores 0 for that dimension,
so sparse incoming claims do not accidentally match well-populated events.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any

# ── Thresholds ────────────────────────────────────────────────────────────────

HIGH_CONFIDENCE: float = 0.75  # auto-attach; no new event, no review
UNCERTAIN_LOW: float = 0.40  # new event + PendingDuplicateReview

logger = logging.getLogger(__name__)

# ── Weights (must sum to 1.0) ─────────────────────────────────────────────────

_WEIGHTS: dict[str, float] = {
    "registration": 0.45,  # near-unique per accident; strong signal
    "event_date": 0.30,  # exact date match strongly suggests same event
    "operator": 0.10,  # varies across sources, lower weight
    "location": 0.08,  # free-text, noisy
    "aircraft_type": 0.07,  # informative but many accidents share the same type
}

assert abs(sum(_WEIGHTS.values()) - 1.0) < 1e-9, "Weights must sum to 1.0"


# ── Result types ──────────────────────────────────────────────────────────────


@dataclass
class MatchResult:
    score: float
    matched_fields: list[str] = field(default_factory=list)
    candidate_event_id: Any = None  # UUID | None
    ambiguous_tie: bool = False
    tied_candidate_event_ids: list[Any] = field(default_factory=list)


@dataclass
class MatchDecision:
    action: str  # "attach" | "review" | "new"
    score: float
    matched_fields: list[str]
    candidate_event_id: Any = None
    tied_candidate_event_ids: list[Any] = field(default_factory=list)


# ── Normalisation helpers ─────────────────────────────────────────────────────


def _norm(value: Any) -> str:
    """Lowercase, strip, collapse whitespace."""
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value).lower().strip())


def _norm_date(value: Any) -> str:
    """Normalise to YYYY-MM-DD or return '' on failure.

    Only unambiguous ISO-8601 dates (``YYYY-MM-DD`` / ``YYYY/MM/DD``) are
    accepted.  The old ``DD-MM-YYYY`` branch was removed because it is
    indistinguishable from ``MM-DD-YYYY`` at the syntax level, and silently
    choosing day-first caused wrong identity-index keys for US-formatted
    source feeds.

    By the time incoming claims reach the identity matcher they should already
    be ISO-normalised by ``ClaimWriter``'s source-specific normalizer.  If an
    unnormalised value arrives here, it scores 0 on the date dimension (same
    as a missing date) rather than producing a quietly wrong match.
    """
    if value is None:
        return ""
    s = _norm(value)
    # Accept YYYY-MM-DD or YYYY/MM/DD (unambiguous year-first form only), but
    # validate calendar legality before forwarding it into identity matching.
    if re.match(r"^\d{4}[-/]\d{2}[-/]\d{2}$", s):
        parts = re.split(r"[-/]", s)
        candidate = f"{parts[0]}-{parts[1]}-{parts[2]}"
        try:
            date.fromisoformat(candidate)
            return candidate
        except ValueError:
            return ""
    # Try Python date parsing as a final fallback (handles YYYY-MM-DD already
    # covered above, but also any format date.fromisoformat accepts).
    try:
        return str(date.fromisoformat(s))
    except ValueError:
        return ""  # unknown / ambiguous format -> treat as missing


def _date_score(a: str, b: str) -> float:
    """1.0 for exact date match, 0.5 for ±1 day, 0 otherwise."""
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    try:
        da, db = date.fromisoformat(a), date.fromisoformat(b)
        return 0.5 if abs((da - db).days) == 1 else 0.0
    except ValueError:
        return 0.0


def _token_overlap(a: str, b: str) -> float:
    """Jaccard overlap on word tokens; handles variant spellings of operators."""
    if not a or not b:
        return 0.0
    ta = set(a.split())
    tb = set(b.split())
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


# ── Scorer ────────────────────────────────────────────────────────────────────


def _field_score(field_name: str, incoming_val: Any, candidate_val: Any) -> float:
    """Compute per-field similarity in [0, 1]."""
    iv = _norm(incoming_val)
    cv = _norm(candidate_val)
    if not iv or not cv:
        return 0.0
    if field_name == "event_date":
        return _date_score(_norm_date(iv), _norm_date(cv))
    if field_name == "registration":
        # Exact match only; registrations differ per source (N123AB vs N-123-AB).
        return 1.0 if re.sub(r"[-/\s]", "", iv) == re.sub(r"[-/\s]", "", cv) else 0.0
    # operator, location, aircraft_type: token overlap
    return _token_overlap(iv, cv)


def score_match(
    incoming_fields: dict[str, Any],
    candidate_fields: dict[str, Any],
) -> MatchResult:
    """Score how well ``incoming_fields`` matches ``candidate_fields``.

    Returns a ``MatchResult`` with the weighted score and the list of fields
    that contributed to the match.  Only fields present in ``_WEIGHTS`` are
    considered.

    Historical registration alias scoring
    --------------------------------------
    When a candidate ``EventIdentityIndex`` exposes a ``registration_norms``
    key (a list of non-primary historical aliases), this function checks
    whether the incoming registration is among them.  A match contributes
    ``0.5 * _WEIGHTS["registration"]`` (0.225) rather than the full 1.0
    weight.  Combined with a date match (0.30), the typical total is 0.525,
    which lies in the UNCERTAIN_LOW..HIGH_CONFIDENCE range - enough to queue a
    duplicate review but not enough to trigger an auto-attach.

    This prevents corrected-away or historically-conflicting registrations from
    silently attaching future ingestions to the wrong canonical event.
    """
    total = 0.0
    matched: list[str] = []
    for fname, weight in _WEIGHTS.items():
        inc_val = incoming_fields.get(fname)
        can_val = candidate_fields.get(fname)
        fs = _field_score(fname, inc_val, can_val)
        if fs > 0:
            matched.append(fname)
        total += weight * fs

    # Check historical registration aliases at half registration weight.
    # Only applied when the primary registration field did not already match,
    # to avoid double-counting.
    if "registration" not in matched:
        aliases: list[str] = candidate_fields.get("registration_norms", [])
        if aliases:
            inc_reg = incoming_fields.get("registration")
            if inc_reg:
                iv_norm = re.sub(r"[-/\s]", "", _norm(inc_reg))
                if iv_norm and iv_norm in aliases:
                    total = round(total + 0.5 * _WEIGHTS["registration"], 4)
                    matched.append("registration_alias")

    return MatchResult(score=round(total, 4), matched_fields=matched)


# ── Main matcher ──────────────────────────────────────────────────────────────


class EventMatcher:
    """Scores a set of incoming claim fields against candidate projected records.

    Usage::

        matcher = EventMatcher()
        decision = matcher.decide(
            incoming_fields={"event_date": "2024-06-01", "registration": "N123AB"},
            candidates=[proj1, proj2, ...],
        )
        if decision.action == "attach":
            event_id = decision.candidate_event_id
        elif decision.action == "review":
            # create new event + PendingDuplicateReview
            ...
        else:
            # create new event
            ...

    The ``candidates`` list should be pre-filtered to a small window (e.g.
    events whose projected ``event_date`` is within ±1 day of the incoming
    date) to keep this in-memory comparison cheap.
    """

    def __init__(
        self,
        high_confidence: float = HIGH_CONFIDENCE,
        uncertain_low: float = UNCERTAIN_LOW,
    ) -> None:
        self._high = high_confidence
        self._uncertain = uncertain_low

    def best_match(
        self,
        incoming_fields: dict[str, Any],
        candidates: list[Any],  # list[ProjectedAccidentRecord]
    ) -> MatchResult:
        """Return the best-scoring candidate (or score=0 if no candidates).

        Tie detection is explicit: if two candidates share the same top score,
        the first candidate is still returned for audit context, but the result
        is marked ``ambiguous_tie``. ``decide`` then routes sufficiently strong
        ties to curator review instead of silently attaching to whichever row
        happened to be ordered first.
        """
        best = MatchResult(score=0.0)
        best_tied_candidate_event_ids: list[Any] = []
        for candidate in candidates:
            result = score_match(incoming_fields, candidate.fields)
            candidate_match = MatchResult(
                score=result.score,
                matched_fields=result.matched_fields,
                candidate_event_id=candidate.event_id,
            )
            if candidate_match.score > best.score:
                best = candidate_match
                best_tied_candidate_event_ids = [candidate_match.candidate_event_id]
            elif candidate_match.score == best.score and candidate_match.score > 0:
                if not best_tied_candidate_event_ids and best.candidate_event_id is not None:
                    best_tied_candidate_event_ids = [best.candidate_event_id]
                best_tied_candidate_event_ids.append(candidate_match.candidate_event_id)

        if len(best_tied_candidate_event_ids) > 1 and best.score >= self._uncertain:
            best.ambiguous_tie = True
            # Preserve all top-scoring candidate ids for the review-creation
            # layer. De-duplicate defensively without changing candidate order.
            best.tied_candidate_event_ids = list(dict.fromkeys(best_tied_candidate_event_ids))
            logger.warning(
                "Ambiguous event match: top score %.2f shared by events %s; "
                "routing to duplicate review instead of auto-attaching.",
                best.score,
                [str(event_id) for event_id in best.tied_candidate_event_ids],
            )
        return best

    def decide(
        self,
        incoming_fields: dict[str, Any],
        candidates: list[Any],  # list[ProjectedAccidentRecord]
    ) -> MatchDecision:
        """Return a routing decision for the incoming fields vs known events."""
        match = self.best_match(incoming_fields, candidates)
        if match.score >= self._high and not match.ambiguous_tie:
            return MatchDecision(
                action="attach",
                score=match.score,
                matched_fields=match.matched_fields,
                candidate_event_id=match.candidate_event_id,
                tied_candidate_event_ids=match.tied_candidate_event_ids,
            )
        if match.score >= self._uncertain:
            return MatchDecision(
                action="review",
                score=match.score,
                matched_fields=match.matched_fields,
                candidate_event_id=match.candidate_event_id,
                tied_candidate_event_ids=match.tied_candidate_event_ids,
            )
        return MatchDecision(
            action="new",
            score=match.score,
            matched_fields=match.matched_fields,
        )
