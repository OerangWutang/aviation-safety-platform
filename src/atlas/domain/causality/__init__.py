"""Causality bounded context (Phase 4).

Two parallel sub-models, both attached to a public event:

- **HFACS** — Human Factors Analysis and Classification System.
  A four-tier taxonomy (organizational/supervision/preconditions/
  unsafe acts) that an analyst attributes to an event.  Phase 4
  ships the taxonomy as seed data and the per-event attribution
  surface.

- **SHELO** — Software, Hardware, Environment, Liveware, Other.
  A small per-event graph of contributory factor nodes with typed
  interaction edges between them.

Neither model carries its own state machine: visibility of a HFACS
attribution or SHELO factor inherits from the parent
``PublicEventPage``.  A DRAFT event's causal data is 404 on the
public surface; a RETRACTED event's is 410.
"""

from __future__ import annotations

from atlas.domain.causality.entities import (
    EventHfacsAttribution,
    HfacsCategory,
    HfacsSubcategory,
    HfacsTier,
    SheloClass,
    SheloFactor,
    SheloFactorInteraction,
    SheloInteractionKind,
)
from atlas.domain.causality.exceptions import (
    HfacsAttributionConflictError,
    HfacsAttributionNotFoundError,
    HfacsCategoryNotFoundError,
    HfacsSubcategoryNotFoundError,
    SheloFactorConflictError,
    SheloFactorInteractionConflictError,
    SheloFactorInteractionSameNodeError,
    SheloFactorNotFoundError,
)

__all__ = [
    "EventHfacsAttribution",
    "HfacsAttributionConflictError",
    "HfacsAttributionNotFoundError",
    "HfacsCategory",
    "HfacsCategoryNotFoundError",
    "HfacsSubcategory",
    "HfacsSubcategoryNotFoundError",
    "HfacsTier",
    "SheloClass",
    "SheloFactor",
    "SheloFactorConflictError",
    "SheloFactorInteraction",
    "SheloFactorInteractionConflictError",
    "SheloFactorInteractionSameNodeError",
    "SheloFactorNotFoundError",
    "SheloInteractionKind",
]
