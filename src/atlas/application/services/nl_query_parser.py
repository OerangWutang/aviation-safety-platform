"""Deterministic NL query parser (Phase 7).

A rule-based parser that extracts structured filters from free-text
queries.  Stdlib-only — no model dependency.

The parser operates in passes, each consuming substrings of the
input and recording what was matched:

1. **Date phrases** — "2023", "last quarter", "Jan-Mar 2024",
   "before 2020", "after 2018", "between 2015 and 2020".
2. **Fatality predicates** — "fatal", "non-fatal", "more than 100
   fatalities", "killed 200".
3. **HFACS category mentions** — matched against the
   ``hfacs_categories`` taxonomy loaded once per parse.
4. **SHELO factor classes** — matched against the four-element
   enum.
5. **Operator and aircraft phrases** — matched against the
   alias lists below.
6. **Free-text remainder** — whatever's left, routed to FTS.

Each pass consumes the matched substring so subsequent passes don't
double-match.  The resulting ``ParsedQuery.confidence`` is the
fraction of the original (non-stop-word) tokens that were claimed
by a structured pass.

Extension seam
--------------

The parser exposes ``index_for_embeddings(query)`` as a no-op stub.
A future Phase 7.5 can swap the deterministic passes for an
embedding-based parse without changing the parser's signature or
the downstream orchestrator.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import date, datetime

from atlas.domain.causality.entities import HfacsCategory
from atlas.domain.nl_search.entities import ParsedQuery

# ── Stop-word list for confidence calculation ───────────────────────────────
#
# We exclude common short words from the confidence denominator so a
# query like "the 737 in 2023" gets credit for matching "737" and
# "2023" even though "the" and "in" remain in the free-text
# remainder.  Conservative list — we'd rather under-credit than
# over-credit.

_STOP_WORDS = frozenset(
    {
        "a",
        "an",
        "the",
        "and",
        "or",
        "of",
        "in",
        "on",
        "at",
        "to",
        "for",
        "with",
        "by",
        "from",
        "is",
        "was",
        "were",
        "be",
        "been",
        "being",
        "i",
        "we",
        "you",
        "they",
        "show",
        "find",
        "list",
        "tell",
        "me",
        "what",
        "which",
        "where",
        "when",
        "give",
        "search",
        "fetch",
        "get",
    }
)


# ── Aircraft and operator alias lists ───────────────────────────────────────
#
# Small curated set for Phase 7.  In production these would be
# generated from the projected accident corpus; for now hand-coded.
# The parser matches case-insensitively on word boundaries.

_AIRCRAFT_ALIASES: dict[str, str] = {
    "737": "Boeing 737",
    "boeing 737": "Boeing 737",
    "747": "Boeing 747",
    "boeing 747": "Boeing 747",
    "777": "Boeing 777",
    "787": "Boeing 787",
    "a320": "Airbus A320",
    "a330": "Airbus A330",
    "a350": "Airbus A350",
    "a380": "Airbus A380",
    "atr 72": "ATR 72",
    "dash 8": "Bombardier Dash 8",
    "dash-8": "Bombardier Dash 8",
    "embraer 175": "Embraer E175",
    "e175": "Embraer E175",
}

_OPERATOR_ALIASES: dict[str, str] = {
    "delta": "Delta Air Lines",
    "united": "United Airlines",
    "american": "American Airlines",
    "southwest": "Southwest Airlines",
    "british airways": "British Airways",
    "lufthansa": "Lufthansa",
    "air france": "Air France",
    "klm": "KLM",
    "emirates": "Emirates",
    "qantas": "Qantas",
}


# ── Date parsing ────────────────────────────────────────────────────────────


_MONTH_NAMES = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}


@dataclass
class _Match:
    """One substring claimed by a parser pass.

    Tracked so the remainder pass can elide claimed regions from
    the free-text output without disturbing the offsets of
    overlapping matches.
    """

    start: int
    end: int


@dataclass
class _ParseState:
    """Accumulated state across parser passes.

    Each pass appends to ``matches`` (so the remainder pass can
    elide them) and sets the appropriate field on ``parsed``.
    """

    parsed: ParsedQuery = field(default_factory=ParsedQuery)
    matches: list[_Match] = field(default_factory=list)


def _consume(state: _ParseState, start: int, end: int) -> None:
    state.matches.append(_Match(start=start, end=end))


def _remaining_text(query: str, state: _ParseState) -> str:
    """Build the free-text remainder by replacing claimed regions
    with whitespace and collapsing runs.

    We don't delete characters because positional information is
    helpful in error messages; whitespace is harmless to FTS.
    """
    chars = list(query)
    for m in state.matches:
        for i in range(m.start, m.end):
            if 0 <= i < len(chars):
                chars[i] = " "
    return re.sub(r"\s+", " ", "".join(chars)).strip()


# ── Pass 1: explicit year ranges ────────────────────────────────────────────


_YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")
_BEFORE_RE = re.compile(r"\bbefore\s+(\d{4})\b", re.IGNORECASE)
_AFTER_RE = re.compile(r"\bafter\s+(\d{4})\b", re.IGNORECASE)
_BETWEEN_RE = re.compile(r"\bbetween\s+(\d{4})\s+and\s+(\d{4})\b", re.IGNORECASE)
_MONTH_RANGE_RE = re.compile(
    r"\b(jan|january|feb|february|mar|march|apr|april|may|jun|june|"
    r"jul|july|aug|august|sep|sept|september|oct|october|nov|november|"
    r"dec|december)\s*-\s*"
    r"(jan|january|feb|february|mar|march|apr|april|may|jun|june|"
    r"jul|july|aug|august|sep|sept|september|oct|october|nov|november|"
    r"dec|december)\s+(\d{4})\b",
    re.IGNORECASE,
)


def _parse_dates(query: str, state: _ParseState) -> None:
    # Highest specificity first: month ranges, between, before/after,
    # bare years.  Earlier matches consume the text; later passes
    # won't re-match the same substring.
    for m in _MONTH_RANGE_RE.finditer(query):
        # Skip if this region already consumed.
        if any(c.start <= m.start() < c.end for c in state.matches):
            continue
        m1 = _MONTH_NAMES.get(m.group(1).lower())
        m2 = _MONTH_NAMES.get(m.group(2).lower())
        year = int(m.group(3))
        if m1 and m2:
            state.parsed = state.parsed.model_copy(
                update={
                    "event_date_from": date(year, m1, 1),
                    "event_date_to": date(
                        year,
                        m2,
                        # Cheap end-of-month: 28 covers all months
                        # without leap-year fuss.  The filter
                        # downstream is inclusive-ish; missing 2-3
                        # days for end-of-month edge cases is
                        # acceptable for NL search.
                        28,
                    ),
                }
            )
            _consume(state, m.start(), m.end())

    for m in _BETWEEN_RE.finditer(query):
        if any(c.start <= m.start() < c.end for c in state.matches):
            continue
        y1 = int(m.group(1))
        y2 = int(m.group(2))
        lo, hi = min(y1, y2), max(y1, y2)
        state.parsed = state.parsed.model_copy(
            update={
                "event_date_from": date(lo, 1, 1),
                "event_date_to": date(hi, 12, 31),
            }
        )
        _consume(state, m.start(), m.end())

    for m in _BEFORE_RE.finditer(query):
        if any(c.start <= m.start() < c.end for c in state.matches):
            continue
        year = int(m.group(1))
        state.parsed = state.parsed.model_copy(update={"event_date_to": date(year - 1, 12, 31)})
        _consume(state, m.start(), m.end())

    for m in _AFTER_RE.finditer(query):
        if any(c.start <= m.start() < c.end for c in state.matches):
            continue
        year = int(m.group(1))
        state.parsed = state.parsed.model_copy(update={"event_date_from": date(year + 1, 1, 1)})
        _consume(state, m.start(), m.end())

    for m in _YEAR_RE.finditer(query):
        if any(c.start <= m.start() < c.end for c in state.matches):
            continue
        # Bare year: filter to that calendar year.  If both
        # event_date_from and event_date_to are already set from a
        # broader range, skip — the more specific phrase wins.
        if state.parsed.event_date_from is not None and state.parsed.event_date_to is not None:
            continue
        year = int(m.group(0))
        state.parsed = state.parsed.model_copy(
            update={
                "event_date_from": date(year, 1, 1),
                "event_date_to": date(year, 12, 31),
            }
        )
        _consume(state, m.start(), m.end())


# ── Pass 2: fatality predicates ─────────────────────────────────────────────


_FATAL_ONLY_RE = re.compile(r"\bfatal(?!\s*-?\s*non)\b", re.IGNORECASE)
_NON_FATAL_RE = re.compile(r"\bnon[\s-]?fatal\b", re.IGNORECASE)
_MORE_THAN_RE = re.compile(
    r"\b(?:more than|over|exceeding|above)\s+(\d{1,5})\s+"
    r"(?:fatalit(?:y|ies)|deaths?|killed)\b",
    re.IGNORECASE,
)
_FEWER_THAN_RE = re.compile(
    r"\b(?:fewer than|less than|under|below)\s+(\d{1,5})\s+"
    r"(?:fatalit(?:y|ies)|deaths?|killed)\b",
    re.IGNORECASE,
)


def _parse_fatalities(query: str, state: _ParseState) -> None:
    # Order matters: ranged predicates first so they take precedence
    # over the bare "fatal" / "non-fatal" matches.
    for m in _MORE_THAN_RE.finditer(query):
        if any(c.start <= m.start() < c.end for c in state.matches):
            continue
        state.parsed = state.parsed.model_copy(update={"fatalities_min": int(m.group(1)) + 1})
        _consume(state, m.start(), m.end())

    for m in _FEWER_THAN_RE.finditer(query):
        if any(c.start <= m.start() < c.end for c in state.matches):
            continue
        state.parsed = state.parsed.model_copy(update={"fatalities_max": int(m.group(1)) - 1})
        _consume(state, m.start(), m.end())

    for m in _NON_FATAL_RE.finditer(query):
        if any(c.start <= m.start() < c.end for c in state.matches):
            continue
        state.parsed = state.parsed.model_copy(update={"non_fatal_only": True})
        _consume(state, m.start(), m.end())

    for m in _FATAL_ONLY_RE.finditer(query):
        if any(c.start <= m.start() < c.end for c in state.matches):
            continue
        # Skip if already marked non-fatal (overlapping match).
        if state.parsed.non_fatal_only:
            continue
        state.parsed = state.parsed.model_copy(update={"fatal_only": True})
        _consume(state, m.start(), m.end())


# ── Pass 3: aircraft + operator aliases ─────────────────────────────────────


def _parse_aliases(
    query: str,
    state: _ParseState,
    *,
    aliases: dict[str, str],
    field_name: str,
) -> None:
    # Longest aliases first so "boeing 737" beats "737".  Stable
    # sort ensures determinism.
    sorted_aliases = sorted(aliases.items(), key=lambda kv: len(kv[0]), reverse=True)
    for alias, canonical in sorted_aliases:
        # Word-bounded, case-insensitive match.  The ``\b`` on the
        # right is omitted for aliases containing spaces because
        # ``re.escape`` keeps the spaces intact; the left ``\b`` is
        # enough to prevent "737" matching mid-"3737".
        pattern = re.compile(rf"\b{re.escape(alias)}\b", re.IGNORECASE)
        for m in pattern.finditer(query):
            if any(c.start <= m.start() < c.end for c in state.matches):
                continue
            # Only set the field if it's not already populated by a
            # more specific (longer) alias on this pass.
            current = getattr(state.parsed, field_name)
            if current is not None:
                continue
            state.parsed = state.parsed.model_copy(update={field_name: canonical})
            _consume(state, m.start(), m.end())
            break  # one match per alias per pass


# ── Pass 4: HFACS category mentions ─────────────────────────────────────────


def _parse_hfacs_categories(
    query: str,
    state: _ParseState,
    *,
    categories: list[HfacsCategory],
) -> None:
    """Match the ``name`` field of each HFACS category as a phrase.

    Case-insensitive substring match.  Cheap to evaluate (the
    taxonomy is <30 rows) and gives analysts "supervision failures"
    routing into the ``SUPERVISION`` tier filter.
    """
    codes: list[str] = []
    for cat in categories:
        # The category names are stable phrases like "Crew Resource
        # Management" — match as a case-insensitive whole phrase.
        pattern = re.compile(rf"\b{re.escape(cat.name)}\b", re.IGNORECASE)
        for m in pattern.finditer(query):
            if any(c.start <= m.start() < c.end for c in state.matches):
                continue
            if cat.code not in codes:
                codes.append(cat.code)
            _consume(state, m.start(), m.end())
            break
    if codes:
        state.parsed = state.parsed.model_copy(update={"hfacs_category_codes": codes})


# ── Pass 5: SHELO factor classes ────────────────────────────────────────────


_SHELO_KEYWORDS: dict[str, list[str]] = {
    "SOFTWARE": ["software", "firmware", "FMS", "FADEC"],
    "HARDWARE": ["hardware", "engine", "airframe", "structural"],
    "ENVIRONMENT": ["weather", "icing", "turbulence", "windshear"],
    "LIVEWARE": ["pilot", "crew", "fatigue", "human"],
}


def _parse_shelo(query: str, state: _ParseState) -> None:
    classes: list[str] = []
    for shelo_class, keywords in _SHELO_KEYWORDS.items():
        for kw in keywords:
            pattern = re.compile(rf"\b{re.escape(kw)}\b", re.IGNORECASE)
            for m in pattern.finditer(query):
                if any(c.start <= m.start() < c.end for c in state.matches):
                    continue
                if shelo_class not in classes:
                    classes.append(shelo_class)
                _consume(state, m.start(), m.end())
                break
    if classes:
        state.parsed = state.parsed.model_copy(update={"shelo_factor_classes": classes})


# ── Confidence calculation ──────────────────────────────────────────────────


def _compute_confidence(query: str, state: _ParseState) -> float:
    """How much of the non-stop-word content did the parser claim?

    Word-level: tokens (non-stop-word, non-empty) covered by at
    least one match count as matched.  Confidence = matched / total.
    A query made entirely of stop words yields 0.0 (nothing to
    match in the first place; the orchestrator can fall back to
    FTS on the raw text).
    """
    tokens = list(re.finditer(r"\b\w+\b", query))
    significant = [t for t in tokens if t.group(0).lower() not in _STOP_WORDS]
    if not significant:
        return 0.0
    matched = 0
    for t in significant:
        if any(c.start <= t.start() < c.end for c in state.matches):
            matched += 1
    return matched / len(significant)


# ── Public API ──────────────────────────────────────────────────────────────


def parse_nl_query(
    query: str,
    *,
    hfacs_categories: list[HfacsCategory],
) -> ParsedQuery:
    """Parse a free-text query into structured filters.

    Idempotent and deterministic: same input always yields same
    output.  No model dependency, no network calls.

    The caller supplies the HFACS taxonomy so the parser can be
    used without a UoW handle (testing, batch analysis, etc.).
    """
    state = _ParseState()
    _parse_dates(query, state)
    _parse_fatalities(query, state)
    _parse_aliases(query, state, aliases=_AIRCRAFT_ALIASES, field_name="aircraft_type")
    _parse_aliases(query, state, aliases=_OPERATOR_ALIASES, field_name="operator")
    _parse_hfacs_categories(query, state, categories=hfacs_categories)
    _parse_shelo(query, state)
    confidence = _compute_confidence(query, state)
    remainder = _remaining_text(query, state)
    state.parsed = state.parsed.model_copy(
        update={
            "free_text_remainder": remainder,
            "confidence": confidence,
        }
    )
    return state.parsed


def query_hash_for(query: str) -> str:
    """Stable, lowercased SHA256 hex of a query string.

    Used for the ``query_hash`` column in ``nl_query_log`` so
    analytics can group repeats without re-hashing on read.
    """
    return hashlib.sha256(query.lower().strip().encode("utf-8")).hexdigest()


def hour_bucket_for(when: datetime) -> datetime:
    """Floor a datetime to its hour boundary, preserving tzinfo."""
    return when.replace(minute=0, second=0, microsecond=0)


# ── Extension seam for Phase 7.5 (embeddings) ───────────────────────────────


def index_for_embeddings(query: str) -> None:
    """Stub for a future embedding-based parser.

    Phase 7 ships a deterministic parser; a Phase 7.5 would replace
    or augment the rule passes with an embedding similarity search
    against a vector store.  The function signature exists so the
    orchestrator's call site doesn't change.

    Currently a no-op.
    """
    _ = query  # intentional: this is a placeholder


__all__ = [
    "hour_bucket_for",
    "index_for_embeddings",
    "parse_nl_query",
    "query_hash_for",
]
