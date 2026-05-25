"""Pydantic schemas for the Phase 4 causality routers.

Two surfaces — public reads (taxonomy + per-event composite) and
editorial writes (HFACS attributions, SHELO factors and
interactions).  All schemas carry ``extra='forbid'``.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class _CausalityModel(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)


# ── Taxonomy ────────────────────────────────────────────────────────────────


class HfacsSubcategoryItem(_CausalityModel):
    id: UUID
    code: str
    name: str
    description: str | None = None
    is_custom: bool


class HfacsCategoryItem(_CausalityModel):
    id: UUID
    tier_code: str
    code: str
    tier: str
    name: str
    description: str
    is_custom: bool
    subcategories: list[HfacsSubcategoryItem]


class HfacsTaxonomyResponse(_CausalityModel):
    categories: list[HfacsCategoryItem]


# ── HFACS attributions ──────────────────────────────────────────────────────


class HfacsAttributionItem(_CausalityModel):
    id: UUID
    event_id: UUID
    category_id: UUID
    category_code: str
    category_name: str
    category_tier: str
    subcategory_id: UUID | None = None
    subcategory_code: str | None = None
    subcategory_name: str | None = None
    confidence: float
    note: str | None = None
    editor_user_id: UUID
    version: int
    created_at: datetime
    updated_at: datetime


class EventHfacsResponse(_CausalityModel):
    event_id: UUID
    attributions: list[HfacsAttributionItem]


class AttachHfacsAttributionRequest(_CausalityModel):
    category_id: UUID
    subcategory_id: UUID | None = None
    confidence: float = Field(ge=0.0, le=1.0)
    note: str | None = Field(default=None, max_length=4000)


class UpdateHfacsAttributionRequest(_CausalityModel):
    expected_version: int = Field(ge=1)
    confidence: float = Field(ge=0.0, le=1.0)
    note: str | None = Field(default=None, max_length=4000)


# ── SHELO ───────────────────────────────────────────────────────────────────


class SheloFactorItem(_CausalityModel):
    id: UUID
    event_id: UUID
    factor_class: str
    label: str
    description: str | None = None
    editor_user_id: UUID
    version: int
    created_at: datetime
    updated_at: datetime


class SheloFactorInteractionItem(_CausalityModel):
    id: UUID
    event_id: UUID
    source_factor_id: UUID
    target_factor_id: UUID
    interaction_kind: str
    note: str | None = None
    editor_user_id: UUID
    created_at: datetime


class EventSheloResponse(_CausalityModel):
    event_id: UUID
    factors: list[SheloFactorItem]
    interactions: list[SheloFactorInteractionItem]


class AttachSheloFactorRequest(_CausalityModel):
    factor_class: str
    label: str = Field(min_length=1, max_length=300)
    description: str | None = Field(default=None, max_length=8000)


class UpdateSheloFactorRequest(_CausalityModel):
    expected_version: int = Field(ge=1)
    factor_class: str
    label: str = Field(min_length=1, max_length=300)
    description: str | None = Field(default=None, max_length=8000)


class AttachSheloInteractionRequest(_CausalityModel):
    source_factor_id: UUID
    target_factor_id: UUID
    interaction_kind: str
    note: str | None = Field(default=None, max_length=4000)
