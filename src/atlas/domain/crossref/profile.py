"""Echo: build a :class:`HazardProfile` from a private hazard report.

Pure and deterministic.  v1 takes the analyst's / FOQA system's *structured*
inputs (NTSB cause categories, FAR part, aircraft category, severity) plus a
**scrubbed** narrative, and reduces them to the normalised matching signals.

Deliberate scope line
----------------------
v1 does **not** try to infer NTSB causal categories from free narrative text
with a hand-rolled keyword table - that would dress up guesswork as the Board's
taxonomy.  Category inference is an explicit extension seam (see
``CROSSREF_ENGINE.md``): a future enricher (Orion entities / an LLM classifier
behind the existing model seam) can populate ``finding_categories`` from text.
Until then, the lexical ``terms`` carry the narrative signal and the structured
categories come from whoever actually knows them.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping

from atlas.domain.crossref.entities import HazardProfile

# Minimal, dependency-free English stopword set + aviation filler that adds no
# discriminating signal.  Kept small on purpose; this is a token filter, not an
# NLP pipeline.
_STOPWORDS: frozenset[str] = frozenset(
    """
    a an the and or but if then else of to in on at by for with without from into
    over under again further once is are was were be been being have has had do does
    did this that these those it its as not no nor so than too very can will just
    during about above below up down out off who whom which what when where why how
    pilot aircraft airplane flight flew flying plane reported report during near
    """.split()
)

_TOKEN_RE = re.compile(r"[a-z][a-z0-9\-]{2,}")  # ≥3 chars, starts with a letter


def normalize_terms(text: str | None) -> frozenset[str]:
    """Lowercase, tokenise, drop stopwords/short tokens.  Deterministic."""
    if not text:
        return frozenset()
    tokens = _TOKEN_RE.findall(text.lower())
    return frozenset(t for t in tokens if t not in _STOPWORDS)


def category_key(category_no: str | None, subcategory_no: str | None) -> str | None:
    """NTSB cause taxonomy key in ``"CC.SS"`` form (or ``"CC"`` if no subcat)."""
    cat = (category_no or "").strip()
    if not cat:
        return None
    sub = (subcategory_no or "").strip()
    return f"{cat}.{sub}" if sub else cat


def categories_from_finding_items(items: Iterable[Mapping[str, object]]) -> frozenset[str]:
    """Extract ``"CC.SS"`` keys from importer-shaped ``causal_findings`` items."""
    keys: set[str] = set()
    for it in items:
        key = category_key(
            str(it.get("category_no") or "") or None,
            str(it.get("subcategory_no") or "") or None,
        )
        if key:
            keys.add(key)
    return frozenset(keys)


def normalize_severity(value: str | None) -> str | None:
    """Map an injury/severity label to ``{fatal, serious, minor, none}``."""
    if not value:
        return None
    v = value.strip().lower()
    for band in ("fatal", "serious", "minor", "none"):
        if band in v:
            return band
    return None


def build_hazard_profile(
    *,
    finding_categories: Iterable[str] = (),
    far_part: str | None = None,
    aircraft_category: str | None = None,
    severity: str | None = None,
    scrubbed_narrative: str | None = None,
    extra_terms: Iterable[str] = (),
) -> HazardProfile:
    """Assemble a :class:`HazardProfile` from structured inputs + scrubbed text.

    ``scrubbed_narrative`` must already be deidentified by the caller; Echo never
    sees raw private narrative (the system's deidentification service runs
    upstream).  Only derived tokens are retained.
    """
    terms = normalize_terms(scrubbed_narrative) | frozenset(
        t for t in (s.strip().lower() for s in extra_terms) if t
    )
    return HazardProfile(
        finding_categories=frozenset(c.strip() for c in finding_categories if c and c.strip()),
        far_part=(far_part or None),
        aircraft_category=(aircraft_category or None),
        severity=normalize_severity(severity),
        terms=terms,
    )
