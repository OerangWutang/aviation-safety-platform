"""Metering bounded context (Phase 8).

Per-tenant, per-day usage accounting derived from the write-side
actions of earlier phases.  Phase 8 ships units only — pricing and
invoicing live in the operator's external billing system.
"""

from __future__ import annotations

from atlas.domain.metering.entities import (
    METRIC_KINDS,
    MetricKind,
    UsageDailyRollup,
    UsageEvent,
    UsageSummaryRow,
)

# Sentinel UUID used to encode "no tenant" in the daily rollup
# table's natural key.  Importable so the use cases, fakes, and SQL
# repo agree on the same value.
NO_TENANT_SENTINEL = "00000000-0000-0000-0000-000000000000"

__all__ = [
    "METRIC_KINDS",
    "NO_TENANT_SENTINEL",
    "MetricKind",
    "UsageDailyRollup",
    "UsageEvent",
    "UsageSummaryRow",
]
