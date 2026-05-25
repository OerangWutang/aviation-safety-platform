"""Echo: build the public precedent corpus and run cross-reference matching."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from atlas.domain.crossref.entities import HazardProfile, PrecedentMatch, PrecedentRecord
from atlas.domain.crossref.profile import (
    build_hazard_profile,
    categories_from_finding_items,
    normalize_severity,
    normalize_terms,
)
from atlas.domain.services.echo_matcher import PrecedentMatcher


def precedent_record_from_ntsb_claims(event_id: str, claims: Mapping[str, Any]) -> PrecedentRecord:
    """Build a :class:`PrecedentRecord` from the canonical NTSB claim vocabulary.

    ``claims`` is the ``{field_name: field_value}`` view of an event's claims -
    exactly the shape the NTSB importer emits.  Public data only.
    """
    findings = claims.get("causal_findings") or []
    finding_cats = (
        categories_from_finding_items(findings) if isinstance(findings, list) else frozenset()
    )

    # Terms from the public probable-cause + factual narratives.
    text_parts = [
        str(claims.get("probable_cause_narrative") or ""),
        str(claims.get("factual_narrative") or ""),
    ]
    terms = normalize_terms(" ".join(p for p in text_parts if p))

    location = (
        ", ".join(
            p
            for p in (
                str(claims.get("location_city") or ""),
                str(claims.get("location_state") or ""),
            )
            if p
        )
        or None
    )
    aircraft = (
        " ".join(
            p
            for p in (
                str(claims.get("aircraft_make") or ""),
                str(claims.get("aircraft_model") or ""),
            )
            if p
        )
        or None
    )
    pc = claims.get("probable_cause_narrative")

    return PrecedentRecord(
        event_id=event_id,
        finding_categories=finding_cats,
        far_part=(claims.get("far_part") or None),
        aircraft_category=(claims.get("aircraft_category") or None),
        severity=normalize_severity(claims.get("highest_injury_level")),
        terms=terms,
        display_occurred_on=(claims.get("occurred_on") or None),
        display_location=location,
        display_aircraft=aircraft,
        display_probable_cause=(str(pc)[:300] if pc else None),
    )


def cross_reference(
    profile: HazardProfile,
    precedent_corpus: Iterable[PrecedentRecord],
    *,
    matcher: PrecedentMatcher | None = None,
    limit: int = 20,
) -> list[PrecedentMatch]:
    """Rank a precedent corpus against a hazard profile.

    Pure orchestration: pass an already-loaded corpus and get ranked,
    explainable precedent matches back.  The DB-backed use case wraps this with
    tenant-scoped reads and tenant-private persistence (see module docstring).
    """
    matcher = matcher or PrecedentMatcher()
    return matcher.rank(profile, precedent_corpus, limit=limit)


__all__ = [
    "build_hazard_profile",
    "cross_reference",
    "precedent_record_from_ntsb_claims",
]
