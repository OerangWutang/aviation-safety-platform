"""Causality domain entities (Phase 4).

Two sub-models — HFACS and SHELO — sharing this module because they
live in the same conceptual context ("structured causal claims
about an event") even though their schemas are independent.

Design notes
------------

- All entities are immutable Pydantic ``DomainModel`` instances.
  Updates produce a new instance via ``model_copy``.

- HFACS attributions and SHELO factors both carry ``version`` for
  optimistic concurrency.  An analyst attaching attributions
  concurrently with another analyst should not silently clobber.

- The HFACS taxonomy itself (``HfacsCategory``, ``HfacsSubcategory``)
  has no ``version`` because it's reference data; updates happen via
  schema migration, not application code.

- SHELO interactions are stored as typed edges with a small enum of
  kinds.  The model permits cycles at the storage level — sometimes
  a real causal graph has them — and surfaces them to a reviewer
  rather than rejecting at INSERT.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import UUID, uuid4

from pydantic import Field, model_validator

from atlas.domain.entities import DomainModel
from atlas.domain.utils import utc_now

# ── HFACS ────────────────────────────────────────────────────────────────────


class HfacsTier(StrEnum):
    """The four HFACS tiers.

    Stored as a string column for human-readable filtering.  Order
    in the standard literature is top-down (organisational →
    unsafe-act); the StrEnum's declaration order mirrors that.
    """

    ORGANIZATIONAL = "ORGANIZATIONAL"
    SUPERVISION = "SUPERVISION"
    PRECONDITIONS = "PRECONDITIONS"
    UNSAFE_ACTS = "UNSAFE_ACTS"


class HfacsCategory(DomainModel):
    """One row of the HFACS category reference table.

    ``code`` is the stable join key (e.g. "PRE-CRM") that external
    tooling joins on.  ``tier_code`` is the 3-letter prefix
    (ORG/SUP/PRE/ACT) for grouped UI rendering.

    ``is_custom`` is the extension flag for operator-defined
    categories.  Phase 4 ships only the standard set with
    ``is_custom=False``.
    """

    id: UUID = Field(default_factory=uuid4)
    tier_code: str = Field(min_length=1, max_length=4)
    code: str = Field(min_length=1, max_length=20)
    tier: HfacsTier
    name: str = Field(min_length=1, max_length=200)
    description: str
    is_custom: bool = False
    created_at: datetime = Field(default_factory=utc_now)


class HfacsSubcategory(DomainModel):
    """Optional leaf-level row under a category.

    Phase 4 ships an empty subcategory table; operators populate it
    on demand for fine-grained attribution.  An attribution can be
    category-level (subcategory_id NULL) or subcategory-level —
    never both for the same (event, category) pair.
    """

    id: UUID = Field(default_factory=uuid4)
    category_id: UUID
    code: str = Field(min_length=1, max_length=40)
    name: str = Field(min_length=1, max_length=200)
    description: str | None = None
    is_custom: bool = False
    created_at: datetime = Field(default_factory=utc_now)


class EventHfacsAttribution(DomainModel):
    """The editorial claim that this event manifested this HFACS bucket.

    Carries ``confidence`` (0..1) — the analyst's own assessment of
    how strongly the evidence supports the attribution.  A
    confidence of 1.0 is "this is unambiguously what happened";
    0.5 is "this is one plausible reading".  Phase 4 doesn't
    aggregate confidence across attributions — that's a Phase 7+
    concern.

    ``subcategory_id`` is optional: an attribution can be
    category-only (the analyst knows it's a CRM failure but doesn't
    want to commit to which subcategory) or subcategory-specific.
    """

    id: UUID = Field(default_factory=uuid4)
    event_id: UUID
    category_id: UUID
    subcategory_id: UUID | None = None
    confidence: float = Field(ge=0.0, le=1.0)
    note: str | None = None
    editor_user_id: UUID
    version: int = Field(default=1, ge=1)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


# ── SHELO ────────────────────────────────────────────────────────────────────


class SheloClass(StrEnum):
    """The five SHELO factor classes.

    The taxonomy is from the SHELO model in aviation human-factors
    literature: factors are classified by what *kind* of element of
    the operational system contributed.
    """

    SOFTWARE = "SOFTWARE"
    HARDWARE = "HARDWARE"
    ENVIRONMENT = "ENVIRONMENT"
    LIVEWARE = "LIVEWARE"
    OTHER = "OTHER"


class SheloInteractionKind(StrEnum):
    """The four interaction kinds between SHELO factors.

    - ``PRECONDITION`` — source factor's existence was a necessary
      precondition for target factor's manifestation.
    - ``AGGRAVATED`` — source factor made target factor worse than
      it would have been alone.
    - ``MITIGATED`` — source factor reduced target factor's severity
      or likelihood.
    - ``MASKED`` — source factor made target factor harder to detect
      or attribute.
    """

    PRECONDITION = "PRECONDITION"
    AGGRAVATED = "AGGRAVATED"
    MITIGATED = "MITIGATED"
    MASKED = "MASKED"


class SheloFactor(DomainModel):
    """One contributory factor on an event.

    ``label`` is the human-readable description ("right engine
    FADEC software fault"); ``factor_class`` is the SHELO bucket
    (SOFTWARE in that example).  Factors are event-local — a
    "FADEC fault" on event A is a different row from the same fault
    on event B, even if the underlying cause is the same.  Phase 4
    doesn't try to model cross-event factor identity.
    """

    id: UUID = Field(default_factory=uuid4)
    event_id: UUID
    factor_class: SheloClass
    label: str = Field(min_length=1, max_length=300)
    description: str | None = None
    editor_user_id: UUID
    version: int = Field(default=1, ge=1)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class SheloFactorInteraction(DomainModel):
    """A typed edge between two factors on the same event.

    The entity-level validator pins:

    - source and target must differ (no self-loops);
    - both factors live on the same event (validated at the use case
      level since we don't carry both factors here, just their IDs).

    Cycles in the per-event graph are *permitted* at the schema and
    entity level because real causal models sometimes contain mutual
    feedback loops (A aggravated B which masked A's detectability).
    The editorial workflow surfaces cycles to a reviewer.
    """

    id: UUID = Field(default_factory=uuid4)
    event_id: UUID
    source_factor_id: UUID
    target_factor_id: UUID
    interaction_kind: SheloInteractionKind
    note: str | None = None
    editor_user_id: UUID
    created_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def _no_self_loop(self) -> SheloFactorInteraction:
        # Schema enforces this via CHECK; doing the check in the entity
        # too gives us a typed error path for in-memory tests rather
        # than a raw IntegrityError.
        if self.source_factor_id == self.target_factor_id:
            raise ValueError(
                "SheloFactorInteraction.source_factor_id must differ from target_factor_id"
            )
        return self
