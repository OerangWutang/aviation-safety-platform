"""Tenancy bounded context (Phase 5).

Tenant-private data lives in *parallel* tables, not as a column on
existing public tables.  This makes accidental contamination of
public projections impossible by construction: a query against the
public ``claims`` table cannot return a tenant row because there are
no tenant rows in that table.

Isolation invariants (also pinned by tests):

1. Public read paths (``/public/*``, ``/search/*``, ``/accidents``,
   ``/provenance``) never query the ``tenant_*`` tables.
2. Tenant repositories take ``tenant_id`` as a *required* parameter
   on every method.  A router that forgets to pass it raises
   TypeError, not silent leakage.
3. Cross-tenant access is denied at the auth dependency
   (``require_tenant_membership``), at the use case (verifies the
   ``CurrentTenantUser.tenant_id`` matches the path), and at the
   repository (the WHERE clause).  Three independent layers.
"""

from __future__ import annotations

from atlas.domain.tenancy.entities import (
    Tenant,
    TenantClaim,
    TenantClaimKind,
    TenantEventAssociation,
    TenantEventAssociationKind,
    TenantEventOverlay,
    TenantIngestionRun,
    TenantIngestionRunStatus,
    TenantMembership,
    TenantRole,
    TenantSafetyReport,
    TenantSafetyReportKind,
    TenantSource,
)
from atlas.domain.tenancy.exceptions import (
    CrossTenantAccessError,
    DeidentificationRequiredError,
    NotATenantApiKeyError,
    TenantClaimBatchTooLargeError,
    TenantInactiveError,
    TenantIngestionRunClosedError,
    TenantIngestionRunNotFoundError,
    TenantNotFoundError,
    TenantSourceAlreadyExistsError,
    TenantSourceNotFoundError,
)

__all__ = [
    "CrossTenantAccessError",
    "DeidentificationRequiredError",
    "NotATenantApiKeyError",
    "Tenant",
    "TenantClaim",
    "TenantClaimBatchTooLargeError",
    "TenantClaimKind",
    "TenantEventAssociation",
    "TenantEventAssociationKind",
    "TenantEventOverlay",
    "TenantInactiveError",
    "TenantIngestionRun",
    "TenantIngestionRunClosedError",
    "TenantIngestionRunNotFoundError",
    "TenantIngestionRunStatus",
    "TenantMembership",
    "TenantNotFoundError",
    "TenantRole",
    "TenantSafetyReport",
    "TenantSafetyReportKind",
    "TenantSource",
    "TenantSourceAlreadyExistsError",
    "TenantSourceNotFoundError",
]
