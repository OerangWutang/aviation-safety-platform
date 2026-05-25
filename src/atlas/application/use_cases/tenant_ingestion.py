"""Tenant ingestion use cases (Phase 6).

Five use cases composing the FOQA/ASAP write path on top of the
Phase 5 tenant repos:

- :class:`OpenTenantIngestionRun` — create a new run in RUNNING.
- :class:`SubmitTenantClaimsBatch` — append a batch of claims to a
  RUNNING run.
- :class:`CompleteTenantIngestionRun` — transition a run to
  SUCCEEDED or FAILED.
- :class:`SubmitTenantSafetyReport` — file an ASAP-style report,
  optionally associated with a public event in the same atomic
  unit.
- :class:`ListTenantEvidenceForEvent` — read the tenant's
  structured + narrative + association evidence for an event.

Three layers of isolation, identical to Phase 5:

1. Auth gate (``require_tenant_membership``) in the router.
2. Use-case check (``caller_tenant_id == path tenant_id``).
3. Repository (every method takes ``tenant_id`` as a required kwarg).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from atlas.application.services.deidentification import (
    DeidentificationResult,
    assert_acceptable_narrative,
    run_deidentification,
)
from atlas.application.services.metering import MeteringService
from atlas.application.unit_of_work import UnitOfWork
from atlas.domain.metering.entities import MetricKind
from atlas.domain.tenancy.entities import (
    TenantClaim,
    TenantClaimKind,
    TenantEventAssociation,
    TenantEventAssociationKind,
    TenantIngestionRun,
    TenantIngestionRunStatus,
    TenantRole,
    TenantSafetyReport,
    TenantSafetyReportKind,
)
from atlas.domain.tenancy.exceptions import (
    CrossTenantAccessError,
    DeidentificationRequiredError,
    TenantClaimBatchTooLargeError,
    TenantClaimUnknownEventError,
    TenantIngestionRunClosedError,
    TenantIngestionRunNotFoundError,
    TenantNotFoundError,
    TenantSourceNotFoundError,
)
from atlas.domain.utils import utc_now

logger = logging.getLogger(__name__)


# Operational ceiling on per-batch claim count.  Chosen so a single
# INSERT-batch comfortably fits under Postgres's practical statement
# size limit (~1MB serialised), with headroom for JSONB field_value
# payloads.  A chatty FOQA exporter that wants to ship 10k claims
# splits across 10 calls.
MAX_CLAIMS_PER_BATCH: int = 1000


# Only OWNER and MEMBER can write tenant ingestion data.  READ_ONLY
# is for analytics consumers; allowing it to write would defeat the
# role.  Surfaced as a constant so the router and use case agree.
_WRITE_TENANT_ROLES = frozenset({TenantRole.OWNER.value, TenantRole.MEMBER.value})


def _require_write_role(caller_tenant_role: str) -> None:
    """Raise HTTP 403 if the caller cannot write tenant data.

    Use cases raise FastAPI HTTPException directly (mirroring Phase 5)
    so the role check produces the same wire shape whether the gate
    fires at the auth dependency or here.  Defence in depth — the
    auth dependency in the router enforces the same rule, but having
    it in the use case lets CLI callers and worker dispatchers
    inherit the same gate without re-stating it.
    """
    if caller_tenant_role not in _WRITE_TENANT_ROLES:
        from fastapi import HTTPException

        raise HTTPException(
            status_code=403,
            detail={
                "code": "INSUFFICIENT_TENANT_ROLE",
                "message": ("Tenant ingestion requires OWNER or MEMBER role"),
            },
        )


# ── Open ingestion run ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class OpenTenantIngestionRunInput:
    tenant_id: UUID
    caller_tenant_id: UUID
    caller_tenant_role: str
    tenant_source_id: UUID


class OpenTenantIngestionRun:
    """Start a new tenant ingestion run.

    Verifies the source belongs to the tenant before creating the
    run — otherwise an attacker who knew a target tenant's source
    id could open ingestion runs against it via their own tenant's
    URL (the cross-tenant access check would catch that path-vs-key
    mismatch separately, but layering helps).
    """

    def __init__(self, uow: UnitOfWork):
        self._uow = uow

    async def execute(self, input: OpenTenantIngestionRunInput) -> TenantIngestionRun:
        if input.caller_tenant_id != input.tenant_id:
            raise CrossTenantAccessError(
                caller_tenant_id=input.caller_tenant_id,
                target_tenant_id=input.tenant_id,
            )
        _require_write_role(input.caller_tenant_role)
        # Layer 3: repository takes tenant_id; cross-tenant probe
        # returns None.
        source = await self._uow.tenant_sources.get(
            tenant_id=input.tenant_id, source_id=input.tenant_source_id
        )
        if source is None:
            raise TenantSourceNotFoundError(
                f"Tenant source {input.tenant_source_id} not found in tenant {input.tenant_id}"
            )
        run = TenantIngestionRun(
            tenant_id=input.tenant_id,
            tenant_source_id=input.tenant_source_id,
            status=TenantIngestionRunStatus.RUNNING.value,
        )
        await self._uow.tenant_ingestion_runs.add(tenant_id=input.tenant_id, run=run)
        await self._uow.commit()
        return run


# ── Submit claims batch ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class IncomingClaim:
    """One claim as it arrives over the wire.

    Distinct from :class:`TenantClaim` because the wire shape carries
    the tenant context implicitly (via the path) and lacks the
    server-managed fields (``id``, ``created_at``, ``tenant_id``,
    ``tenant_ingestion_run_id``).  The use case maps incoming to
    persisted in one place so the conversion stays auditable.
    """

    event_id: UUID
    field_name: str
    field_value: Any
    claim_kind: TenantClaimKind = TenantClaimKind.OTHER
    confidence: float | None = None


@dataclass(frozen=True)
class SubmitTenantClaimsBatchInput:
    tenant_id: UUID
    caller_tenant_id: UUID
    caller_tenant_role: str
    run_id: UUID
    claims: list[IncomingClaim]


@dataclass(frozen=True)
class SubmitTenantClaimsBatchResult:
    inserted_count: int


class SubmitTenantClaimsBatch:
    """Append a batch of claims to a RUNNING ingestion run."""

    def __init__(self, uow: UnitOfWork):
        self._uow = uow

    async def execute(self, input: SubmitTenantClaimsBatchInput) -> SubmitTenantClaimsBatchResult:
        if input.caller_tenant_id != input.tenant_id:
            raise CrossTenantAccessError(
                caller_tenant_id=input.caller_tenant_id,
                target_tenant_id=input.tenant_id,
            )
        _require_write_role(input.caller_tenant_role)

        if len(input.claims) > MAX_CLAIMS_PER_BATCH:
            raise TenantClaimBatchTooLargeError(
                f"Batch has {len(input.claims)} claims; maximum is {MAX_CLAIMS_PER_BATCH} per call."
            )
        if not input.claims:
            # Empty batch is a no-op; we don't fail it because
            # heartbeat-style "are you still there?" calls are
            # legitimate.
            return SubmitTenantClaimsBatchResult(inserted_count=0)

        run = await self._uow.tenant_ingestion_runs.get(
            tenant_id=input.tenant_id, run_id=input.run_id
        )
        if run is None:
            raise TenantIngestionRunNotFoundError(
                f"Ingestion run {input.run_id} not found in tenant {input.tenant_id}"
            )
        # Status is a string on the entity (Phase 5 ships it as str
        # for backwards-compat; Phase 6 enum coerces to the same
        # value).  Compare on the canonical string.
        if run.status != TenantIngestionRunStatus.RUNNING.value:
            raise TenantIngestionRunClosedError(
                f"Ingestion run {input.run_id} is {run.status!r}; "
                f"cannot append claims to a closed run."
            )

        persisted: list[TenantClaim] = []
        # Validate all event_ids exist in the public corpus before the bulk
        # INSERT.  A single WHERE id = ANY(:ids) round-trip is cheaper than N
        # individual gets and gives a clean 422 with the offending UUIDs instead
        # of an IntegrityError 500 from the FK constraint.
        unique_event_ids = list({c.event_id for c in input.claims})
        existing_ids = await self._uow.events.find_existing_ids(unique_event_ids)
        unknown = set(unique_event_ids) - existing_ids
        if unknown:
            raise TenantClaimUnknownEventError(unknown_ids=unknown)

        for incoming in input.claims:
            persisted.append(
                TenantClaim(
                    tenant_id=input.tenant_id,
                    event_id=incoming.event_id,
                    tenant_source_id=run.tenant_source_id,
                    tenant_ingestion_run_id=run.id,
                    field_name=incoming.field_name,
                    field_value=incoming.field_value,
                    claim_kind=incoming.claim_kind,
                    confidence=incoming.confidence,
                )
            )
        await self._uow.tenant_claims.add_many(tenant_id=input.tenant_id, claims=persisted)
        # Meter: one usage event per claim ingested.  Recorded in
        # the same UoW as the claims so the meter and the action are
        # atomic.
        await MeteringService(self._uow).record(
            metric_kind=MetricKind.TENANT_CLAIM_INGESTED,
            tenant_id=input.tenant_id,
            user_id=None,
            resource_id=run.id,
            quantity=len(persisted),
        )
        await self._uow.commit()
        return SubmitTenantClaimsBatchResult(inserted_count=len(persisted))


# ── Complete ingestion run ──────────────────────────────────────────────────


@dataclass(frozen=True)
class CompleteTenantIngestionRunInput:
    tenant_id: UUID
    caller_tenant_id: UUID
    caller_tenant_role: str
    run_id: UUID
    final_status: TenantIngestionRunStatus


class CompleteTenantIngestionRun:
    """Transition a RUNNING run to SUCCEEDED or FAILED.

    One-way door: a non-RUNNING run rejects further state changes.
    The terminal state is the operator's authoritative record of
    what happened with the batch.
    """

    def __init__(self, uow: UnitOfWork):
        self._uow = uow

    async def execute(self, input: CompleteTenantIngestionRunInput) -> TenantIngestionRun:
        if input.caller_tenant_id != input.tenant_id:
            raise CrossTenantAccessError(
                caller_tenant_id=input.caller_tenant_id,
                target_tenant_id=input.tenant_id,
            )
        _require_write_role(input.caller_tenant_role)
        if input.final_status == TenantIngestionRunStatus.RUNNING:
            # Symmetry with the one-way-door rule: completing into
            # RUNNING is nonsensical.
            raise TenantIngestionRunClosedError(
                "Cannot 'complete' an ingestion run into RUNNING; use SUCCEEDED or FAILED."
            )
        run = await self._uow.tenant_ingestion_runs.get(
            tenant_id=input.tenant_id, run_id=input.run_id
        )
        if run is None:
            raise TenantIngestionRunNotFoundError(
                f"Ingestion run {input.run_id} not found in tenant {input.tenant_id}"
            )
        if run.status != TenantIngestionRunStatus.RUNNING.value:
            raise TenantIngestionRunClosedError(
                f"Ingestion run {input.run_id} is already {run.status!r}; cannot re-finalise."
            )
        moment = utc_now()
        await self._uow.tenant_ingestion_runs.update_status(
            tenant_id=input.tenant_id,
            run_id=input.run_id,
            status=input.final_status,
            finished_at=moment,
        )
        # Meter: one event per completed run, regardless of final
        # status (SUCCEEDED or FAILED both count as a completed run
        # for billing — the operator did the work either way).
        await MeteringService(self._uow).record(
            metric_kind=MetricKind.TENANT_INGESTION_RUN_COMPLETED,
            tenant_id=input.tenant_id,
            user_id=None,
            resource_id=input.run_id,
        )
        updated = run.model_copy(update={"status": input.final_status.value, "finished_at": moment})
        await self._uow.commit()
        # Do not refetch after commit.  Tenant HTTP connections use a
        # transaction-local PostgreSQL RLS GUC; a post-commit query can start a
        # fresh transaction before the context is re-established in non-SQL
        # test doubles or future UoW implementations.  The row was already
        # loaded and the intended terminal state is known, so return that
        # authoritative domain snapshot instead.
        return updated


# ── Submit safety report ────────────────────────────────────────────────────


@dataclass(frozen=True)
class SubmitTenantSafetyReportInput:
    tenant_id: UUID
    caller_tenant_id: UUID
    caller_tenant_role: str
    submitter_user_id: UUID
    report_kind: TenantSafetyReportKind
    narrative_markdown: str
    deidentified_attested: bool
    external_report_ref: str | None = None
    # Optional event association created in the same UoW so the
    # report and the association land together or not at all.
    associate_with_event_id: UUID | None = None
    association_kind: TenantEventAssociationKind = TenantEventAssociationKind.RELATED
    association_note: str | None = None


@dataclass(frozen=True)
class SubmitTenantSafetyReportResult:
    report: TenantSafetyReport
    association: TenantEventAssociation | None
    scrub_replacements: list[str]


class SubmitTenantSafetyReport:
    """File a tenant safety report.

    The operator MUST set ``deidentified_attested=True``.  Without
    that flag Atlas refuses the submission entirely — we treat the
    flag as the operator's signed declaration that their internal
    deidentification step has run.

    Atlas runs its own best-effort scrubber on top (see
    :mod:`atlas.application.services.deidentification`), enforces a
    minimum word count after scrubbing, and stores the **scrubbed**
    text — never the raw narrative.  The scrubber's replacements
    are returned to the caller for the operator's audit trail.
    """

    def __init__(self, uow: UnitOfWork):
        self._uow = uow

    async def execute(self, input: SubmitTenantSafetyReportInput) -> SubmitTenantSafetyReportResult:
        if input.caller_tenant_id != input.tenant_id:
            raise CrossTenantAccessError(
                caller_tenant_id=input.caller_tenant_id,
                target_tenant_id=input.tenant_id,
            )
        _require_write_role(input.caller_tenant_role)
        if not input.deidentified_attested:
            raise DeidentificationRequiredError(
                "Safety report submission requires "
                "deidentified_attested=True.  The operator's safety "
                "office must attest that the narrative has been "
                "deidentified before submission."
            )

        # Two-step scrub: first a non-raising run to capture the
        # replacements for audit, then the gate that enforces the
        # minimum-word count after scrubbing.
        preview: DeidentificationResult = run_deidentification(input.narrative_markdown)
        cleaned_narrative = assert_acceptable_narrative(input.narrative_markdown)

        # If the caller asked for an event association, verify the
        # event exists in the public canonical store *first* so the
        # report and the association land atomically (or not at all).
        if input.associate_with_event_id is not None:
            projection = await self._uow.projections.get(input.associate_with_event_id)
            if projection is None:
                raise TenantNotFoundError(
                    f"No public event {input.associate_with_event_id} to associate with."
                )

        report = TenantSafetyReport(
            tenant_id=input.tenant_id,
            report_kind=input.report_kind,
            narrative_markdown=cleaned_narrative,
            deidentified_attested=True,
            external_report_ref=input.external_report_ref,
            submitter_user_id=input.submitter_user_id,
        )
        await self._uow.tenant_safety_reports.add(tenant_id=input.tenant_id, report=report)

        association: TenantEventAssociation | None = None
        if input.associate_with_event_id is not None:
            association = TenantEventAssociation(
                tenant_id=input.tenant_id,
                event_id=input.associate_with_event_id,
                safety_report_id=report.id,
                association_kind=input.association_kind,
                note=input.association_note,
                created_by_user_id=input.submitter_user_id,
            )
            await self._uow.tenant_event_associations.add(
                tenant_id=input.tenant_id, association=association
            )

        # Meter: one event per filed safety report.
        await MeteringService(self._uow).record(
            metric_kind=MetricKind.TENANT_REPORT_FILED,
            tenant_id=input.tenant_id,
            user_id=input.submitter_user_id,
            resource_id=report.id,
        )
        await self._uow.commit()
        return SubmitTenantSafetyReportResult(
            report=report,
            association=association,
            scrub_replacements=preview.replacements,
        )


# ── Tenant evidence for an event ────────────────────────────────────────────


@dataclass(frozen=True)
class TenantEvidenceView:
    """Composite read: what does this tenant know about this event?

    Returns claims (split by kind for UI grouping), safety reports
    (only those associated with this event, via the associations
    table), and the association rows themselves so the UI can
    render the editorial linkages.
    """

    event_id: UUID
    foqa_claims: list[TenantClaim]
    asap_claims: list[TenantClaim]
    other_claims: list[TenantClaim]
    associated_reports: list[TenantSafetyReport]
    associations: list[TenantEventAssociation]


class ListTenantEvidenceForEvent:
    """Read the tenant's private evidence for a public event."""

    def __init__(self, uow: UnitOfWork):
        self._uow = uow

    async def execute(
        self,
        *,
        tenant_id: UUID,
        caller_tenant_id: UUID,
        event_id: UUID,
    ) -> TenantEvidenceView:
        if caller_tenant_id != tenant_id:
            raise CrossTenantAccessError(
                caller_tenant_id=caller_tenant_id,
                target_tenant_id=tenant_id,
            )

        foqa = await self._uow.tenant_claims.list_for_event_by_kind(
            tenant_id=tenant_id,
            event_id=event_id,
            claim_kind=TenantClaimKind.FOQA,
        )
        asap = await self._uow.tenant_claims.list_for_event_by_kind(
            tenant_id=tenant_id,
            event_id=event_id,
            claim_kind=TenantClaimKind.ASAP,
        )
        other = await self._uow.tenant_claims.list_for_event_by_kind(
            tenant_id=tenant_id,
            event_id=event_id,
            claim_kind=TenantClaimKind.OTHER,
        )
        associations = await self._uow.tenant_event_associations.list_for_event(
            tenant_id=tenant_id, event_id=event_id
        )
        # Pull only the safety reports that are associated with this
        # event.  We don't surface every tenant safety report on
        # every event read — that would defeat the editorial
        # association semantics.
        report_ids = {a.safety_report_id for a in associations if a.safety_report_id is not None}
        associated_reports: list[TenantSafetyReport] = []
        for rid in report_ids:
            report = await self._uow.tenant_safety_reports.get(tenant_id=tenant_id, report_id=rid)
            if report is not None:
                associated_reports.append(report)

        await self._uow.rollback()
        return TenantEvidenceView(
            event_id=event_id,
            foqa_claims=foqa,
            asap_claims=asap,
            other_claims=other,
            associated_reports=associated_reports,
            associations=associations,
        )


__all__ = [
    "MAX_CLAIMS_PER_BATCH",
    "CompleteTenantIngestionRun",
    "CompleteTenantIngestionRunInput",
    "IncomingClaim",
    "ListTenantEvidenceForEvent",
    "OpenTenantIngestionRun",
    "OpenTenantIngestionRunInput",
    "SubmitTenantClaimsBatch",
    "SubmitTenantClaimsBatchInput",
    "SubmitTenantClaimsBatchResult",
    "SubmitTenantSafetyReport",
    "SubmitTenantSafetyReportInput",
    "SubmitTenantSafetyReportResult",
    "TenantEvidenceView",
]
