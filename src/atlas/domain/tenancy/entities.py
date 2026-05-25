"""Domain entities for the tenancy bounded context.

All entities here represent tenant-private rows.  The single
exception is :class:`Tenant` itself, which is the tenant directory
row — known to admins but not exposed publicly.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from pydantic import Field

from atlas.domain.entities import DomainModel
from atlas.domain.utils import utc_now


class TenantRole(StrEnum):
    """Tenant-side role for a membership or API key.

    OWNER
        Full read and write within the tenant.  Can register sources,
        run ingestions, and edit overlays.

    MEMBER
        Read and write within the tenant.  Cannot register or remove
        sources.

    READ_ONLY
        Read-only within the tenant.  Used for analytics consumers
        and dashboards that should not produce write traffic.

    Note: these are tenant-side roles, orthogonal to the system-side
    :class:`Role` (analyst/reviewer/admin).  An API key has both: a
    system role that governs public reads, and a tenant role that
    governs tenant-scoped operations.
    """

    OWNER = "OWNER"
    MEMBER = "MEMBER"
    READ_ONLY = "READ_ONLY"

    @classmethod
    def values(cls) -> frozenset[str]:
        return frozenset(r.value for r in cls)


class Tenant(DomainModel):
    """The tenant directory row.

    ``slug`` is a stable URL-safe identifier; ``display_name`` is for
    UI.  ``is_active`` allows soft-deactivation without dropping
    membership/key rows that would otherwise need a full cleanup.
    """

    id: UUID = Field(default_factory=uuid4)
    slug: str
    display_name: str
    is_active: bool = True
    created_at: datetime = Field(default_factory=utc_now)


class TenantMembership(DomainModel):
    """A user's membership in a tenant.

    The membership row is the system's record of "user X may act
    inside tenant Y at role Z".  API key tenant-binding piggybacks on
    this: a tenant API key's ``tenant_id`` + ``tenant_role`` should
    match an active membership.  When they diverge (e.g. membership
    was revoked but the key wasn't rotated), the membership is
    authoritative — the key is still valid for system access but
    cannot use tenant routes.
    """

    id: UUID = Field(default_factory=uuid4)
    tenant_id: UUID
    user_id: UUID
    tenant_role: TenantRole
    created_at: datetime = Field(default_factory=utc_now)


class TenantSource(DomainModel):
    """A tenant-private source.

    Mirrors the public ``Source`` shape minimally — kind and
    reliability_tier exist for symmetry with the public projection
    logic in case we ever add tenant-private projections (out of
    scope for Phase 5).  Names are unique *per tenant*: different
    operators can both have a source called "Operations" without
    colliding.
    """

    id: UUID = Field(default_factory=uuid4)
    tenant_id: UUID
    name: str
    kind: str = "EXTERNAL"
    reliability_tier: int = Field(default=1, ge=1)
    created_at: datetime = Field(default_factory=utc_now)


class TenantIngestionRun(DomainModel):
    """A tenant-private ingestion run.

    Created when a tenant submits a batch of claims through the
    tenant-side ingestion path (deferred until Phase 6 / 8 wires the
    write paths).  Phase 5 ships the entity so the migration and
    repository surface are coherent.
    """

    id: UUID = Field(default_factory=uuid4)
    tenant_id: UUID
    tenant_source_id: UUID
    status: str = "running"
    started_at: datetime = Field(default_factory=utc_now)
    finished_at: datetime | None = None


class TenantClaimKind(StrEnum):
    """Discriminator for the source-shape of a tenant claim.

    - ``FOQA`` — Flight Operations Quality Assurance.  Machine-
      generated exceedance events from the operator's FDM tooling.
      Typically high-volume, structured (flap setting, sink rate,
      timestamp).

    - ``ASAP`` — Aviation Safety Action Program.  Although ASAP
      narratives proper live in :class:`TenantSafetyReport`, an
      analyst may extract structured claims from a report
      (e.g. "fatigue mentioned") and store them as tenant claims
      with kind=ASAP for cross-reference.

    - ``OTHER`` — generic tenant-private structured claim.  Catch-
      all for tenant-defined claim types that don't fit the FOQA or
      ASAP buckets.

    Forward-compatibility: adding a new kind requires a migration
    to widen the CHECK constraint AND an update to this enum.  The
    schema test
    :func:`tests/domain/test_migration_orm_consistency` keeps these
    in sync at CI time.
    """

    FOQA = "FOQA"
    ASAP = "ASAP"
    OTHER = "OTHER"


class TenantClaim(DomainModel):
    """A tenant-private claim about a public event.

    Anchored to ``event_id`` (the public canonical event identity) so
    the tenant's private view aligns with public ground truth, but
    stored in the parallel ``tenant_claims`` table so it cannot leak
    into the public projection.

    Phase 6 added:

    - ``claim_kind`` — discriminates FOQA / ASAP-derived / OTHER
      structured claims so a single table can carry all three
      without per-kind subtables.
    - ``confidence`` — 0..1 float for the tenant's own confidence in
      the claim.  Deliberately separate from the public projection's
      completeness band so tenant editorial doesn't get pulled into
      public confidence math.
    """

    id: UUID = Field(default_factory=uuid4)
    tenant_id: UUID
    event_id: UUID
    tenant_source_id: UUID
    tenant_ingestion_run_id: UUID | None = None
    field_name: str
    field_value: Any = None
    claim_kind: TenantClaimKind = TenantClaimKind.OTHER
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    created_at: datetime = Field(default_factory=utc_now)


class TenantEventOverlay(DomainModel):
    """A tenant's free-form overlay on a public event.

    One row per (tenant, event).  Carries notes (Markdown) and a
    free-form JSONB ``overlay_fields`` dict for tenant-private
    structured annotations.

    Phase 5 ships read + idempotent create-or-replace.  A richer
    versioned editorial workflow analogous to the public one
    (Phase 9) is reserved for a later phase if tenants ask for it.
    """

    id: UUID = Field(default_factory=uuid4)
    tenant_id: UUID
    event_id: UUID
    notes_markdown: str | None = None
    overlay_fields: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


# ── Phase 6: ASAP narrative reports + event associations ────────────────────


class TenantIngestionRunStatus(StrEnum):
    """The three lifecycle states of a tenant ingestion run.

    - ``RUNNING`` — the run is open, claims can be appended.
    - ``SUCCEEDED`` — the operator closed the run with a successful
      finalisation.  Claims in this run are immutable after this
      point.
    - ``FAILED`` — the operator closed the run with a failure marker.
      Claims already submitted in the run remain stored (audit
      trail) but are flagged so the tenant's read paths can show
      "this batch was abandoned partway through".
    """

    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class TenantSafetyReportKind(StrEnum):
    """Discriminator for the source of a tenant safety report."""

    FOQA = "FOQA"
    ASAP = "ASAP"
    OTHER = "OTHER"


class TenantSafetyReport(DomainModel):
    """A tenant-private narrative safety report (Phase 6).

    Phase 6's headline data shape: ASAP-style self-reports plus any
    narrative artifact a tenant wants to capture without forcing it
    into the structured-claim shape.

    Hard rule: this entity is **never** exposed on any public
    surface.  Atlas's job is to store it, version it, and make it
    available to authorised tenant readers only.  The router layer
    enforces this by routing every safety-report read through the
    tenant prefix; no public router ever touches this table.

    ``deidentified_attested`` is the operator's signed declaration
    that the narrative has been deidentified before submission.
    Atlas does best-effort PII stripping on top (see
    :mod:`atlas.application.services.deidentification`) but the
    operator is the authoritative deidentifier — this column is the
    operator's record.
    """

    id: UUID = Field(default_factory=uuid4)
    tenant_id: UUID
    report_kind: TenantSafetyReportKind
    narrative_markdown: str
    deidentified_attested: bool = False
    external_report_ref: str | None = None
    submitter_user_id: UUID
    created_at: datetime = Field(default_factory=utc_now)


class TenantEventAssociationKind(StrEnum):
    """The editorial relationship between tenant evidence and a
    public event.

    - ``RELATED`` — the analyst believes there's some correlation
      worth noting.  The weakest claim; used as the default.
    - ``CONTRIBUTED_TO`` — the analyst believes the tenant's
      private evidence describes a factor that contributed to the
      public event.
    - ``PRECEDED`` — the tenant's private evidence describes
      something that happened before the public event and may be
      a leading indicator.
    """

    RELATED = "RELATED"
    CONTRIBUTED_TO = "CONTRIBUTED_TO"
    PRECEDED = "PRECEDED"


class TenantEventAssociation(DomainModel):
    """An explicit "this tenant evidence is associated with this
    public event" claim.

    Exactly one of ``claim_id`` / ``safety_report_id`` is set.  The
    schema-level CHECK enforces this; the entity's validator does
    too.

    Why this is a separate table from claims/reports:

    1. An analyst may attach the same safety report to multiple
       events (a fatigue-related ASAP report might bear on three
       separate approaches) or none (a general operational
       concern).
    2. The association itself is editorial — an analyst chooses to
       make the connection — and deserves its own audit row
       independent of the underlying evidence.
    """

    id: UUID = Field(default_factory=uuid4)
    tenant_id: UUID
    event_id: UUID
    claim_id: UUID | None = None
    safety_report_id: UUID | None = None
    association_kind: TenantEventAssociationKind = TenantEventAssociationKind.RELATED
    note: str | None = None
    created_by_user_id: UUID
    created_at: datetime = Field(default_factory=utc_now)

    def model_post_init(self, __context: Any) -> None:
        # The "exactly one of {claim_id, safety_report_id} set" rule
        # is checked at the schema level by the CHECK constraint;
        # checking it here too keeps in-memory tests honest and
        # gives use cases a clearer error than an IntegrityError.
        has_claim = self.claim_id is not None
        has_report = self.safety_report_id is not None
        if has_claim == has_report:
            raise ValueError(
                "TenantEventAssociation requires exactly one of "
                "claim_id or safety_report_id to be set"
            )


# ── Echo cross-reference results ─────────────────────────────────────────────


class CrossrefResultStatus(StrEnum):
    PENDING = "PENDING"
    COMPLETE = "COMPLETE"
    FAILED = "FAILED"


class TenantCrossrefResult(DomainModel):
    """Tenant-private Echo cross-reference result set.

    Persists the ranked ``PrecedentMatch`` list produced for one hazard
    source (a ``TenantSafetyReport`` or a ``TenantClaim``).  Stored as
    JSONB so the rich match structure (components, shared sets, display
    fields) travels as a single column without a per-match child table.

    Hard invariants:
    - ``tenant_id`` is always set; RLS enforces tenant isolation at the
      DB level (migration 046).
    - ``matches_json`` is the serialised ``list[PrecedentMatch]`` — a
      stable format contract defined in ``CROSSREF_ENGINE.md``.
    - ``status`` is the only mutable column; the match payload itself
      is written once and never updated (a re-run creates a new row).
    """

    id: UUID = Field(default_factory=uuid4)
    tenant_id: UUID
    # The private hazard that was cross-referenced.  Exactly one of
    # {safety_report_id, claim_id} is set; the CHECK constraint enforces it.
    safety_report_id: UUID | None = None
    claim_id: UUID | None = None
    status: CrossrefResultStatus = CrossrefResultStatus.PENDING
    # Serialised list[PrecedentMatch] — written once on COMPLETE.
    matches_json: list[dict[str, Any]] = Field(default_factory=list)
    # Echo config snapshot so results remain interpretable if weights change.
    matcher_config_json: dict[str, Any] = Field(default_factory=dict)
    match_count: int = 0
    requested_at: datetime = Field(default_factory=utc_now)
    completed_at: datetime | None = None
    error_detail: str | None = None

    def model_post_init(self, __context: Any) -> None:
        has_report = self.safety_report_id is not None
        has_claim = self.claim_id is not None
        if has_report == has_claim:
            raise ValueError(
                "TenantCrossrefResult requires exactly one of "
                "safety_report_id or claim_id to be set"
            )
