"""Tenancy-layer exceptions.

These map to HTTP responses via the global handlers in
``atlas.presentation.api.app``.  :class:`CrossTenantAccessError`
deserves explicit attention: it's a 403 (forbidden), not a 404, even
though a 404 would marginally reduce information leakage about
tenant existence.  We choose 403 because tenant routes are always
authenticated and the caller knows their own tenant; pretending the
other tenant doesn't exist would obscure access-control bugs in a
caller's UI more than it would protect anything.
"""

from __future__ import annotations

from uuid import UUID

from atlas.domain.exceptions import AtlasError, DomainValidationError, NotFoundError


class TenantNotFoundError(NotFoundError):
    """Raised when a tenant id does not resolve."""

    code = "TENANT_NOT_FOUND"


class TenantInactiveError(AtlasError):
    """Raised when a request targets a tenant whose ``is_active=false``.

    Surfaced as HTTP 403 so the caller sees this as an access denial
    rather than a missing resource.  Reactivation is an admin
    operation; the API does not expose it from this layer.
    """

    code = "TENANT_INACTIVE"


class CrossTenantAccessError(AtlasError):
    """Raised when an authenticated tenant caller targets a different tenant.

    Distinct from generic 403 so the audit log can highlight the
    cross-tenant intent.  The response body never reveals the target
    tenant's display_name.
    """

    code = "CROSS_TENANT_ACCESS"

    def __init__(self, *, caller_tenant_id: UUID, target_tenant_id: UUID) -> None:
        self.caller_tenant_id = caller_tenant_id
        self.target_tenant_id = target_tenant_id
        super().__init__("API key is not authorised to access this tenant")


class NotATenantApiKeyError(AtlasError):
    """Raised when a system-only API key tries to use a tenant route.

    System API keys (no ``tenant_id`` binding) can read public data
    but cannot use tenant-scoped routes.  This guards against
    accidentally privileging an admin key to inspect tenant data
    without an explicit membership.
    """

    code = "NOT_A_TENANT_API_KEY"


class TenantSourceAlreadyExistsError(DomainValidationError):
    """Raised when a tenant tries to register a source with a name
    already taken within their tenant."""

    code = "TENANT_SOURCE_ALREADY_EXISTS"

    def __init__(self, *, tenant_id: UUID, name: str) -> None:
        self.tenant_id = tenant_id
        self.name = name
        super().__init__(f"Tenant source named {name!r} already exists")


# ── Phase 6 ────────────────────────────────────────────────────────────────


class TenantSourceNotFoundError(NotFoundError):
    """Raised when a tenant source id doesn't resolve under the
    caller's tenant.  Same access-control posture as the existing
    cross-tenant probe: returning 404 on a miss keeps the response
    shape consistent."""

    code = "TENANT_SOURCE_NOT_FOUND"


class TenantIngestionRunNotFoundError(NotFoundError):
    """Raised when a run id doesn't resolve under the caller's
    tenant."""

    code = "TENANT_INGESTION_RUN_NOT_FOUND"


class TenantIngestionRunClosedError(AtlasError):
    """Raised on attempts to append claims to a run that has
    already been finalised (succeeded or failed).

    Surfaced as HTTP 409 — the run's lifecycle is not a 404 (the
    run exists), and not a 422 (the request body is fine).  The
    state is the conflict.
    """

    code = "TENANT_INGESTION_RUN_CLOSED"


class TenantClaimBatchTooLargeError(DomainValidationError):
    """A single batch exceeds the per-batch claim cap.

    Surfaced as 422.  The cap is operational — we choose it small
    enough that a single transaction stays under Postgres's
    practical statement-size limits, but large enough that a
    chatty client doesn't have to micro-batch.
    """

    code = "TENANT_CLAIM_BATCH_TOO_LARGE"


class DeidentificationRequiredError(DomainValidationError):
    """A safety report submission is missing the deidentification
    attestation flag, or failed Atlas's best-effort PII screen.

    Surfaced as 422.  Carries enough detail in the message for an
    operator's UI to render a specific corrective prompt.
    """

    code = "DEIDENTIFICATION_REQUIRED"


class TenantClaimUnknownEventError(DomainValidationError):
    """One or more event_ids in a tenant claims batch do not exist.

    Surfaced as 422.  The operator is trying to attach claims to events
    that are not in the Atlas public corpus.  Either the event has not
    been ingested yet, or the UUID is wrong.

    ``unknown_ids`` carries the offending UUIDs so the operator can
    identify which claims need correction without re-submitting the
    whole batch.
    """

    code = "TENANT_CLAIM_UNKNOWN_EVENT"

    def __init__(self, unknown_ids: set[UUID]) -> None:
        ids_preview = ", ".join(str(i) for i in sorted(unknown_ids, key=str)[:5])
        suffix = f" (and {len(unknown_ids) - 5} more)" if len(unknown_ids) > 5 else ""
        super().__init__(
            f"Batch contains {len(unknown_ids)} unknown event_id(s): {ids_preview}{suffix}. "
            "Each event must exist in the Atlas public corpus before claims can be attached."
        )
        self.unknown_ids = unknown_ids
