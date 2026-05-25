"""Echo: private-hazard ↔ public-precedent cross-reference (domain entities).

Echo answers one question, defensibly: *"Which public accidents resemble this
private hazard report, and exactly why?"*  It is the engine behind the platform's
headline promise - turning an operator's internal hazard into proactive risk
intelligence by surfacing the public investigative record that rhymes with it.

Epistemic stance (load-bearing, not decoration)
-----------------------------------------------
Echo produces **precedent / evidence support**, never a probability of
recurrence.  A strong match means "the public record contains closely analogous
cases", not "this is likely to happen to you".  Concretely:

* :class:`PrecedentMatch` has **no probability field** and never will.  It
  carries a similarity ``score`` in ``[0, 1]`` and a coarse
  :class:`EvidenceSupport` band, both explicitly labelled as *similarity*.
* Every match is **explainable**: the :class:`MatchComponent` breakdown and the
  shared categories/terms say *why* a precedent surfaced, so an analyst can
  audit it rather than trust a black box.

Boundary stance
---------------
A :class:`HazardProfile` is a **reduced, derived** representation of a private
report - normalised taxonomy keys, structured attributes, and lexical tokens.
It deliberately does **not** carry the raw private narrative, so the matching
core can never accidentally emit private text into a result or a shared index.
:class:`PrecedentRecord` is built from **public** data only.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class EvidenceSupport(StrEnum):
    """Coarse band describing how strongly public precedent resembles a hazard.

    This is *precedent support*, kept deliberately distinct from:
    * the public projection's ``confidence_band`` (data completeness), and
    * ``TenantClaim.confidence`` (the tenant's own confidence in their claim).

    It is **not** a probability of the hazard occurring.
    """

    STRONG = "STRONG"
    MODERATE = "MODERATE"
    WEAK = "WEAK"
    NONE = "NONE"


@dataclass(frozen=True)
class HazardProfile:
    """Normalised, privacy-reduced query derived from a private hazard report.

    Built by :mod:`atlas.application.crossref.precedent_index` from structured
    analyst inputs plus a *scrubbed* narrative.  Carries only derived signals -
    no raw narrative - so it is safe to log and to pass around.

    Fields
    ------
    finding_categories : NTSB cause taxonomy keys in ``"CC.SS"`` form
        (category_no.subcategory_no).  The defensible matching spine.
    far_part / aircraft_category / severity : structured attributes; ``None``
        when unknown (an unknown attribute is skipped, never penalised).
    terms : normalised lexical tokens from the scrubbed narrative.
    """

    finding_categories: frozenset[str] = frozenset()
    far_part: str | None = None
    aircraft_category: str | None = None
    severity: str | None = None
    terms: frozenset[str] = frozenset()

    def is_empty(self) -> bool:
        """A profile with no usable signal cannot match anything."""
        return not (
            self.finding_categories
            or self.terms
            or self.far_part
            or self.aircraft_category
            or self.severity
        )


@dataclass(frozen=True)
class PrecedentRecord:
    """A public accident, indexed for matching.  Public data only.

    The ``display_*`` fields exist purely to render a surfaced precedent back to
    the analyst; the matching uses the normalised signal fields.
    """

    event_id: str
    finding_categories: frozenset[str] = frozenset()
    far_part: str | None = None
    aircraft_category: str | None = None
    severity: str | None = None
    terms: frozenset[str] = frozenset()
    # Presentation-only (never used in scoring).
    display_occurred_on: str | None = None
    display_location: str | None = None
    display_aircraft: str | None = None
    display_probable_cause: str | None = None


@dataclass(frozen=True)
class MatchComponent:
    """One scored, weighted contributor to a match - the explainability unit."""

    name: str
    weight: float
    score: float  # 0..1 within this component
    detail: str


@dataclass(frozen=True)
class PrecedentMatch:
    """A public precedent surfaced for a hazard, with the reason it surfaced.

    ``score`` is a similarity measure in ``[0, 1]``; ``support`` is its coarse
    band.  Neither is a probability of recurrence.  There is intentionally no
    probability field.
    """

    event_id: str
    score: float
    support: EvidenceSupport
    components: tuple[MatchComponent, ...]
    shared_finding_categories: frozenset[str] = frozenset()
    shared_terms: frozenset[str] = frozenset()
    # Presentation-only passthrough from the matched PrecedentRecord.
    display_occurred_on: str | None = None
    display_location: str | None = None
    display_aircraft: str | None = None
    display_probable_cause: str | None = None
