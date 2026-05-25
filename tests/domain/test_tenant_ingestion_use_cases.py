"""Use-case tests for Phase 6 tenant ingestion.

Pins:

1. **Three-layer isolation**: cross-tenant calls fail at the use
   case level, not only at the router (the router gate is tested
   in the API test file).
2. **Ingestion run state machine**: a non-RUNNING run rejects
   appends and re-finalisation.
3. **Batch cap**: oversized batches are rejected before any partial
   insert.
4. **Safety report contract**: ``deidentified_attested=True`` is
   required; the scrubber runs and minimum word count is enforced;
   the report and the optional event association land in one UoW.
5. **Tenant evidence read** groups claims by kind, scopes safety
   reports to those associated with the event, and rejects
   cross-tenant probes.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from atlas.application.services.deidentification import (
    MIN_NARRATIVE_WORDS,
    run_deidentification,
)
from atlas.application.use_cases.tenant_ingestion import (
    MAX_CLAIMS_PER_BATCH,
    CompleteTenantIngestionRun,
    CompleteTenantIngestionRunInput,
    IncomingClaim,
    ListTenantEvidenceForEvent,
    OpenTenantIngestionRun,
    OpenTenantIngestionRunInput,
    SubmitTenantClaimsBatch,
    SubmitTenantClaimsBatchInput,
    SubmitTenantSafetyReport,
    SubmitTenantSafetyReportInput,
)
from atlas.domain.entities import AccidentEvent, ProjectedAccidentRecord
from atlas.domain.tenancy.entities import (
    Tenant,
    TenantClaimKind,
    TenantEventAssociation,
    TenantEventAssociationKind,
    TenantIngestionRunStatus,
    TenantRole,
    TenantSafetyReportKind,
    TenantSource,
)
from atlas.domain.tenancy.exceptions import (
    CrossTenantAccessError,
    DeidentificationRequiredError,
    TenantClaimBatchTooLargeError,
    TenantIngestionRunClosedError,
    TenantIngestionRunNotFoundError,
    TenantSourceNotFoundError,
)
from tests.domain._fake_uow import InMemoryUnitOfWork

# ── Helpers ──────────────────────────────────────────────────────────────────


async def _seed_tenant(uow: InMemoryUnitOfWork) -> tuple[UUID, UUID]:
    """Seed one tenant + one source.  Returns (tenant_id, source_id)."""
    tenant = Tenant(slug="acme-airlines", display_name="Acme Airlines")
    uow.store.tenancy.tenants[tenant.id] = tenant
    source = TenantSource(
        tenant_id=tenant.id,
        name="primary",
        kind="FOQA_EXPORT",
    )
    uow.store.tenancy.sources[source.id] = source
    return tenant.id, source.id


def _seed_event(uow: InMemoryUnitOfWork) -> UUID:
    event = AccidentEvent()
    uow.store.events[event.id] = event
    uow.store.projections[event.id] = ProjectedAccidentRecord(
        event_id=event.id, fields={}, completeness_score=0.5
    )
    return event.id


def _owner(role: TenantRole = TenantRole.OWNER) -> str:
    return role.value


# ── Open / submit / complete happy path ─────────────────────────────────────


class TestIngestionRunHappyPath:
    async def test_open_run_succeeds(self) -> None:
        uow = InMemoryUnitOfWork()
        tenant_id, source_id = await _seed_tenant(uow)
        run = await OpenTenantIngestionRun(uow).execute(
            OpenTenantIngestionRunInput(
                tenant_id=tenant_id,
                caller_tenant_id=tenant_id,
                caller_tenant_role=_owner(),
                tenant_source_id=source_id,
            )
        )
        assert run.status == TenantIngestionRunStatus.RUNNING.value
        assert run.tenant_id == tenant_id
        assert run.tenant_source_id == source_id

    async def test_submit_then_complete(self) -> None:
        uow = InMemoryUnitOfWork()
        tenant_id, source_id = await _seed_tenant(uow)
        event_id = _seed_event(uow)
        run = await OpenTenantIngestionRun(uow).execute(
            OpenTenantIngestionRunInput(
                tenant_id=tenant_id,
                caller_tenant_id=tenant_id,
                caller_tenant_role=_owner(),
                tenant_source_id=source_id,
            )
        )

        result = await SubmitTenantClaimsBatch(uow).execute(
            SubmitTenantClaimsBatchInput(
                tenant_id=tenant_id,
                caller_tenant_id=tenant_id,
                caller_tenant_role=_owner(),
                run_id=run.id,
                claims=[
                    IncomingClaim(
                        event_id=event_id,
                        field_name="exceedance:flap_speed",
                        field_value={"observed": 220, "limit": 200},
                        claim_kind=TenantClaimKind.FOQA,
                        confidence=0.9,
                    ),
                    IncomingClaim(
                        event_id=event_id,
                        field_name="exceedance:sink_rate",
                        field_value=1800,
                        claim_kind=TenantClaimKind.FOQA,
                    ),
                ],
            )
        )
        assert result.inserted_count == 2

        completed = await CompleteTenantIngestionRun(uow).execute(
            CompleteTenantIngestionRunInput(
                tenant_id=tenant_id,
                caller_tenant_id=tenant_id,
                caller_tenant_role=_owner(),
                run_id=run.id,
                final_status=TenantIngestionRunStatus.SUCCEEDED,
            )
        )
        assert completed.status == TenantIngestionRunStatus.SUCCEEDED.value
        assert completed.finished_at is not None


# ── State machine ───────────────────────────────────────────────────────────


class TestIngestionRunStateMachine:
    async def test_submit_to_closed_run_409(self) -> None:
        uow = InMemoryUnitOfWork()
        tenant_id, source_id = await _seed_tenant(uow)
        event_id = _seed_event(uow)
        run = await OpenTenantIngestionRun(uow).execute(
            OpenTenantIngestionRunInput(
                tenant_id=tenant_id,
                caller_tenant_id=tenant_id,
                caller_tenant_role=_owner(),
                tenant_source_id=source_id,
            )
        )
        await CompleteTenantIngestionRun(uow).execute(
            CompleteTenantIngestionRunInput(
                tenant_id=tenant_id,
                caller_tenant_id=tenant_id,
                caller_tenant_role=_owner(),
                run_id=run.id,
                final_status=TenantIngestionRunStatus.SUCCEEDED,
            )
        )
        with pytest.raises(TenantIngestionRunClosedError):
            await SubmitTenantClaimsBatch(uow).execute(
                SubmitTenantClaimsBatchInput(
                    tenant_id=tenant_id,
                    caller_tenant_id=tenant_id,
                    caller_tenant_role=_owner(),
                    run_id=run.id,
                    claims=[
                        IncomingClaim(
                            event_id=event_id,
                            field_name="x",
                            field_value=1,
                        )
                    ],
                )
            )

    async def test_complete_does_not_read_tenant_payload_after_commit(self) -> None:
        uow = InMemoryUnitOfWork()
        tenant_id, source_id = await _seed_tenant(uow)
        run = await OpenTenantIngestionRun(uow).execute(
            OpenTenantIngestionRunInput(
                tenant_id=tenant_id,
                caller_tenant_id=tenant_id,
                caller_tenant_role=_owner(),
                tenant_source_id=source_id,
            )
        )

        original_get = uow.tenant_ingestion_runs.get
        original_commit = uow.commit
        committed = False

        async def guarded_get(*, tenant_id: UUID, run_id: UUID):
            if committed:
                raise AssertionError("tenant ingestion run was refetched after commit")
            return await original_get(tenant_id=tenant_id, run_id=run_id)

        async def commit_and_mark() -> None:
            nonlocal committed
            await original_commit()
            committed = True

        uow.tenant_ingestion_runs.get = guarded_get  # type: ignore[method-assign]
        uow.commit = commit_and_mark  # type: ignore[method-assign]

        completed = await CompleteTenantIngestionRun(uow).execute(
            CompleteTenantIngestionRunInput(
                tenant_id=tenant_id,
                caller_tenant_id=tenant_id,
                caller_tenant_role=_owner(),
                run_id=run.id,
                final_status=TenantIngestionRunStatus.SUCCEEDED,
            )
        )

        assert completed.status == TenantIngestionRunStatus.SUCCEEDED.value
        assert completed.finished_at is not None

    async def test_complete_already_closed_run_409(self) -> None:
        uow = InMemoryUnitOfWork()
        tenant_id, source_id = await _seed_tenant(uow)
        run = await OpenTenantIngestionRun(uow).execute(
            OpenTenantIngestionRunInput(
                tenant_id=tenant_id,
                caller_tenant_id=tenant_id,
                caller_tenant_role=_owner(),
                tenant_source_id=source_id,
            )
        )
        await CompleteTenantIngestionRun(uow).execute(
            CompleteTenantIngestionRunInput(
                tenant_id=tenant_id,
                caller_tenant_id=tenant_id,
                caller_tenant_role=_owner(),
                run_id=run.id,
                final_status=TenantIngestionRunStatus.SUCCEEDED,
            )
        )
        with pytest.raises(TenantIngestionRunClosedError):
            await CompleteTenantIngestionRun(uow).execute(
                CompleteTenantIngestionRunInput(
                    tenant_id=tenant_id,
                    caller_tenant_id=tenant_id,
                    caller_tenant_role=_owner(),
                    run_id=run.id,
                    final_status=TenantIngestionRunStatus.FAILED,
                )
            )

    async def test_complete_into_running_rejected(self) -> None:
        uow = InMemoryUnitOfWork()
        tenant_id, source_id = await _seed_tenant(uow)
        run = await OpenTenantIngestionRun(uow).execute(
            OpenTenantIngestionRunInput(
                tenant_id=tenant_id,
                caller_tenant_id=tenant_id,
                caller_tenant_role=_owner(),
                tenant_source_id=source_id,
            )
        )
        with pytest.raises(TenantIngestionRunClosedError):
            await CompleteTenantIngestionRun(uow).execute(
                CompleteTenantIngestionRunInput(
                    tenant_id=tenant_id,
                    caller_tenant_id=tenant_id,
                    caller_tenant_role=_owner(),
                    run_id=run.id,
                    final_status=TenantIngestionRunStatus.RUNNING,
                )
            )

    async def test_unknown_run_404(self) -> None:
        uow = InMemoryUnitOfWork()
        tenant_id, _ = await _seed_tenant(uow)
        with pytest.raises(TenantIngestionRunNotFoundError):
            await CompleteTenantIngestionRun(uow).execute(
                CompleteTenantIngestionRunInput(
                    tenant_id=tenant_id,
                    caller_tenant_id=tenant_id,
                    caller_tenant_role=_owner(),
                    run_id=uuid4(),
                    final_status=TenantIngestionRunStatus.SUCCEEDED,
                )
            )

    async def test_unknown_source_on_open(self) -> None:
        uow = InMemoryUnitOfWork()
        tenant_id, _ = await _seed_tenant(uow)
        with pytest.raises(TenantSourceNotFoundError):
            await OpenTenantIngestionRun(uow).execute(
                OpenTenantIngestionRunInput(
                    tenant_id=tenant_id,
                    caller_tenant_id=tenant_id,
                    caller_tenant_role=_owner(),
                    tenant_source_id=uuid4(),
                )
            )


# ── Batch size cap ──────────────────────────────────────────────────────────


class TestBatchSizeCap:
    async def test_oversize_batch_rejected(self) -> None:
        uow = InMemoryUnitOfWork()
        tenant_id, source_id = await _seed_tenant(uow)
        event_id = _seed_event(uow)
        run = await OpenTenantIngestionRun(uow).execute(
            OpenTenantIngestionRunInput(
                tenant_id=tenant_id,
                caller_tenant_id=tenant_id,
                caller_tenant_role=_owner(),
                tenant_source_id=source_id,
            )
        )
        claims = [
            IncomingClaim(event_id=event_id, field_name=f"f{i}", field_value=i)
            for i in range(MAX_CLAIMS_PER_BATCH + 1)
        ]
        with pytest.raises(TenantClaimBatchTooLargeError):
            await SubmitTenantClaimsBatch(uow).execute(
                SubmitTenantClaimsBatchInput(
                    tenant_id=tenant_id,
                    caller_tenant_id=tenant_id,
                    caller_tenant_role=_owner(),
                    run_id=run.id,
                    claims=claims,
                )
            )
        # And nothing was inserted on the failure path.
        assert len(uow.store.tenancy.claims) == 0

    async def test_empty_batch_noop(self) -> None:
        """Empty batch is legitimate (heartbeat-style); returns
        inserted_count=0 rather than failing."""
        uow = InMemoryUnitOfWork()
        tenant_id, source_id = await _seed_tenant(uow)
        run = await OpenTenantIngestionRun(uow).execute(
            OpenTenantIngestionRunInput(
                tenant_id=tenant_id,
                caller_tenant_id=tenant_id,
                caller_tenant_role=_owner(),
                tenant_source_id=source_id,
            )
        )
        result = await SubmitTenantClaimsBatch(uow).execute(
            SubmitTenantClaimsBatchInput(
                tenant_id=tenant_id,
                caller_tenant_id=tenant_id,
                caller_tenant_role=_owner(),
                run_id=run.id,
                claims=[],
            )
        )
        assert result.inserted_count == 0


# ── Cross-tenant isolation ──────────────────────────────────────────────────


class TestCrossTenantIsolation:
    async def test_cross_tenant_open_rejected(self) -> None:
        uow = InMemoryUnitOfWork()
        tenant_a_id, source_id = await _seed_tenant(uow)
        tenant_b_id = uuid4()
        with pytest.raises(CrossTenantAccessError):
            await OpenTenantIngestionRun(uow).execute(
                OpenTenantIngestionRunInput(
                    tenant_id=tenant_a_id,
                    caller_tenant_id=tenant_b_id,  # caller != path
                    caller_tenant_role=_owner(),
                    tenant_source_id=source_id,
                )
            )

    async def test_cross_tenant_run_lookup_404(self) -> None:
        """Even if a caller knows a target tenant's run id, the
        cross-tenant probe in the run repo returns None.  The use
        case surfaces that as 404, not 403 — same shape as Phase 5's
        existence-leak prevention."""
        uow = InMemoryUnitOfWork()
        tenant_a_id, _source_a = await _seed_tenant(uow)
        # Manually seed a second tenant + run.
        tenant_b = Tenant(slug="b", display_name="B")
        uow.store.tenancy.tenants[tenant_b.id] = tenant_b
        source_b = TenantSource(tenant_id=tenant_b.id, name="x", kind="FOQA")
        uow.store.tenancy.sources[source_b.id] = source_b
        run_b = await OpenTenantIngestionRun(uow).execute(
            OpenTenantIngestionRunInput(
                tenant_id=tenant_b.id,
                caller_tenant_id=tenant_b.id,
                caller_tenant_role=_owner(),
                tenant_source_id=source_b.id,
            )
        )
        # Tenant A tries to complete tenant B's run, claiming it's
        # under their tenant.
        with pytest.raises(TenantIngestionRunNotFoundError):
            await CompleteTenantIngestionRun(uow).execute(
                CompleteTenantIngestionRunInput(
                    tenant_id=tenant_a_id,
                    caller_tenant_id=tenant_a_id,
                    caller_tenant_role=_owner(),
                    run_id=run_b.id,
                    final_status=TenantIngestionRunStatus.SUCCEEDED,
                )
            )

    async def test_read_only_role_rejected_from_writes(self) -> None:
        """READ_ONLY callers cannot open ingestion runs.

        The use case raises FastAPI HTTPException(403); we catch it
        as the bare exception type because the test layer doesn't
        unwrap it the way the router does.
        """
        from fastapi import HTTPException

        uow = InMemoryUnitOfWork()
        tenant_id, source_id = await _seed_tenant(uow)
        with pytest.raises(HTTPException) as exc:
            await OpenTenantIngestionRun(uow).execute(
                OpenTenantIngestionRunInput(
                    tenant_id=tenant_id,
                    caller_tenant_id=tenant_id,
                    caller_tenant_role=TenantRole.READ_ONLY.value,
                    tenant_source_id=source_id,
                )
            )
        assert exc.value.status_code == 403


# ── Safety reports ──────────────────────────────────────────────────────────


_BIG_NARRATIVE = (
    "During approach we observed an unstable descent profile "
    "below 1000 feet that resulted in a go-around per published "
    "stabilised-approach criteria.  The crew handled the recovery "
    "by the book and we filed this report per company policy."
)


class TestSafetyReports:
    async def test_attestation_required(self) -> None:
        uow = InMemoryUnitOfWork()
        tenant_id, _ = await _seed_tenant(uow)
        with pytest.raises(DeidentificationRequiredError):
            await SubmitTenantSafetyReport(uow).execute(
                SubmitTenantSafetyReportInput(
                    tenant_id=tenant_id,
                    caller_tenant_id=tenant_id,
                    caller_tenant_role=_owner(),
                    submitter_user_id=uuid4(),
                    report_kind=TenantSafetyReportKind.ASAP,
                    narrative_markdown=_BIG_NARRATIVE,
                    deidentified_attested=False,
                )
            )

    async def test_too_short_after_scrub_rejected(self) -> None:
        uow = InMemoryUnitOfWork()
        tenant_id, _ = await _seed_tenant(uow)
        with pytest.raises(DeidentificationRequiredError):
            await SubmitTenantSafetyReport(uow).execute(
                SubmitTenantSafetyReportInput(
                    tenant_id=tenant_id,
                    caller_tenant_id=tenant_id,
                    caller_tenant_role=_owner(),
                    submitter_user_id=uuid4(),
                    report_kind=TenantSafetyReportKind.ASAP,
                    narrative_markdown="See attached.",
                    deidentified_attested=True,
                )
            )

    async def test_happy_path_stores_scrubbed_text(self) -> None:
        uow = InMemoryUnitOfWork()
        tenant_id, _ = await _seed_tenant(uow)
        narrative = (
            _BIG_NARRATIVE + " Crew reachable at jdoe@example.com or 555-123-4567. "
            "Tail N12345 was operating."
        )
        result = await SubmitTenantSafetyReport(uow).execute(
            SubmitTenantSafetyReportInput(
                tenant_id=tenant_id,
                caller_tenant_id=tenant_id,
                caller_tenant_role=_owner(),
                submitter_user_id=uuid4(),
                report_kind=TenantSafetyReportKind.ASAP,
                narrative_markdown=narrative,
                deidentified_attested=True,
            )
        )
        # Stored narrative MUST be the scrubbed text — never the raw.
        assert "jdoe@example.com" not in result.report.narrative_markdown
        assert "555-123-4567" not in result.report.narrative_markdown
        assert "N12345" not in result.report.narrative_markdown
        # And the replacements are returned for the operator's audit.
        assert any("jdoe@example.com" in r for r in result.scrub_replacements)
        # No association because we didn't request one.
        assert result.association is None

    async def test_associate_with_event_atomic(self) -> None:
        uow = InMemoryUnitOfWork()
        tenant_id, _ = await _seed_tenant(uow)
        event_id = _seed_event(uow)
        result = await SubmitTenantSafetyReport(uow).execute(
            SubmitTenantSafetyReportInput(
                tenant_id=tenant_id,
                caller_tenant_id=tenant_id,
                caller_tenant_role=_owner(),
                submitter_user_id=uuid4(),
                report_kind=TenantSafetyReportKind.ASAP,
                narrative_markdown=_BIG_NARRATIVE,
                deidentified_attested=True,
                associate_with_event_id=event_id,
                association_kind=TenantEventAssociationKind.CONTRIBUTED_TO,
                association_note="Crew fatigue precursor.",
            )
        )
        assert result.association is not None
        assert result.association.event_id == event_id
        assert result.association.safety_report_id == result.report.id
        assert result.association.association_kind == TenantEventAssociationKind.CONTRIBUTED_TO

    async def test_event_association_validator(self) -> None:
        """Exactly one of (claim_id, safety_report_id) must be set."""
        # Neither set:
        with pytest.raises(ValueError):
            TenantEventAssociation(
                tenant_id=uuid4(),
                event_id=uuid4(),
                created_by_user_id=uuid4(),
            )
        # Both set:
        with pytest.raises(ValueError):
            TenantEventAssociation(
                tenant_id=uuid4(),
                event_id=uuid4(),
                claim_id=uuid4(),
                safety_report_id=uuid4(),
                created_by_user_id=uuid4(),
            )

    async def test_cross_tenant_safety_report_denied(self) -> None:
        uow = InMemoryUnitOfWork()
        tenant_a_id, _ = await _seed_tenant(uow)
        with pytest.raises(CrossTenantAccessError):
            await SubmitTenantSafetyReport(uow).execute(
                SubmitTenantSafetyReportInput(
                    tenant_id=tenant_a_id,
                    caller_tenant_id=uuid4(),  # different tenant
                    caller_tenant_role=_owner(),
                    submitter_user_id=uuid4(),
                    report_kind=TenantSafetyReportKind.ASAP,
                    narrative_markdown=_BIG_NARRATIVE,
                    deidentified_attested=True,
                )
            )


# ── Scrubber unit checks ────────────────────────────────────────────────────


class TestDeidentificationScrubber:
    def test_tail_number_redacted(self) -> None:
        out = run_deidentification("Aircraft N12345AB on takeoff roll")
        assert "N12345AB" not in out.cleaned_text
        assert "[REDACTED:TAIL_NUMBER]" in out.cleaned_text

    def test_email_redacted(self) -> None:
        out = run_deidentification("Contact: pilot.name@airline.com")
        assert "pilot.name@airline.com" not in out.cleaned_text

    def test_phone_redacted(self) -> None:
        out = run_deidentification("Call 555-123-4567 if needed")
        assert "555-123-4567" not in out.cleaned_text

    def test_employee_id_redacted(self) -> None:
        out = run_deidentification("Crew ID 1234567 was on duty")
        assert "1234567" not in out.cleaned_text

    def test_minimum_words_constant(self) -> None:
        # Sanity check that the constant hasn't been silently changed.
        assert MIN_NARRATIVE_WORDS == 20


# ── Tenant evidence read ────────────────────────────────────────────────────


class TestListTenantEvidenceForEvent:
    async def test_groups_by_kind_and_filters_to_tenant(self) -> None:
        uow = InMemoryUnitOfWork()
        tenant_id, source_id = await _seed_tenant(uow)
        event_id = _seed_event(uow)
        run = await OpenTenantIngestionRun(uow).execute(
            OpenTenantIngestionRunInput(
                tenant_id=tenant_id,
                caller_tenant_id=tenant_id,
                caller_tenant_role=_owner(),
                tenant_source_id=source_id,
            )
        )
        await SubmitTenantClaimsBatch(uow).execute(
            SubmitTenantClaimsBatchInput(
                tenant_id=tenant_id,
                caller_tenant_id=tenant_id,
                caller_tenant_role=_owner(),
                run_id=run.id,
                claims=[
                    IncomingClaim(
                        event_id=event_id,
                        field_name="f1",
                        field_value=1,
                        claim_kind=TenantClaimKind.FOQA,
                    ),
                    IncomingClaim(
                        event_id=event_id,
                        field_name="f2",
                        field_value=2,
                        claim_kind=TenantClaimKind.FOQA,
                    ),
                    IncomingClaim(
                        event_id=event_id,
                        field_name="a1",
                        field_value="x",
                        claim_kind=TenantClaimKind.ASAP,
                    ),
                    IncomingClaim(
                        event_id=event_id,
                        field_name="o1",
                        field_value="y",
                        claim_kind=TenantClaimKind.OTHER,
                    ),
                ],
            )
        )
        view = await ListTenantEvidenceForEvent(uow).execute(
            tenant_id=tenant_id,
            caller_tenant_id=tenant_id,
            event_id=event_id,
        )
        assert len(view.foqa_claims) == 2
        assert len(view.asap_claims) == 1
        assert len(view.other_claims) == 1
        assert view.associated_reports == []

    async def test_associated_reports_only(self) -> None:
        """A tenant safety report unrelated to this event must NOT
        appear in the evidence view — only those with an explicit
        association row do."""
        uow = InMemoryUnitOfWork()
        tenant_id, _ = await _seed_tenant(uow)
        event_id = _seed_event(uow)
        other_event_id = _seed_event(uow)

        # File two reports: one associated with our event, one with a
        # different event.
        await SubmitTenantSafetyReport(uow).execute(
            SubmitTenantSafetyReportInput(
                tenant_id=tenant_id,
                caller_tenant_id=tenant_id,
                caller_tenant_role=_owner(),
                submitter_user_id=uuid4(),
                report_kind=TenantSafetyReportKind.ASAP,
                narrative_markdown=_BIG_NARRATIVE,
                deidentified_attested=True,
                associate_with_event_id=event_id,
            )
        )
        await SubmitTenantSafetyReport(uow).execute(
            SubmitTenantSafetyReportInput(
                tenant_id=tenant_id,
                caller_tenant_id=tenant_id,
                caller_tenant_role=_owner(),
                submitter_user_id=uuid4(),
                report_kind=TenantSafetyReportKind.ASAP,
                narrative_markdown=_BIG_NARRATIVE,
                deidentified_attested=True,
                associate_with_event_id=other_event_id,
            )
        )
        view = await ListTenantEvidenceForEvent(uow).execute(
            tenant_id=tenant_id,
            caller_tenant_id=tenant_id,
            event_id=event_id,
        )
        assert len(view.associated_reports) == 1
        assert len(view.associations) == 1
        assert view.associations[0].event_id == event_id

    async def test_cross_tenant_read_denied(self) -> None:
        uow = InMemoryUnitOfWork()
        tenant_a_id, _ = await _seed_tenant(uow)
        with pytest.raises(CrossTenantAccessError):
            await ListTenantEvidenceForEvent(uow).execute(
                tenant_id=tenant_a_id,
                caller_tenant_id=uuid4(),
                event_id=uuid4(),
            )


class TestClaimsEventValidation:
    """SubmitTenantClaimsBatch validates that all event_ids exist before INSERT.

    Without this check, the FK constraint in Postgres would fire an IntegrityError
    that reaches the caller as a 500.  With it, the caller gets a 422 with the
    offending event_ids listed explicitly.
    """

    @pytest.mark.asyncio
    async def test_batch_with_unknown_event_id_raises_domain_error(self) -> None:
        uow = InMemoryUnitOfWork()
        tenant_id, source_id = await _seed_tenant(uow)
        real_event_id = _seed_event(uow)

        # Open a run
        run = await OpenTenantIngestionRun(uow).execute(
            OpenTenantIngestionRunInput(
                tenant_id=tenant_id,
                caller_tenant_id=tenant_id,
                caller_tenant_role=TenantRole.MEMBER.value,
                tenant_source_id=source_id,
            )
        )

        ghost_id = uuid4()  # does not exist in accident_events

        from atlas.domain.tenancy.exceptions import TenantClaimUnknownEventError

        with pytest.raises(TenantClaimUnknownEventError) as exc_info:
            await SubmitTenantClaimsBatch(uow).execute(
                SubmitTenantClaimsBatchInput(
                    tenant_id=tenant_id,
                    caller_tenant_id=tenant_id,
                    caller_tenant_role=TenantRole.MEMBER.value,
                    run_id=run.id,
                    claims=[
                        IncomingClaim(
                            event_id=real_event_id,
                            field_name="altitude",
                            field_value=5000,
                        ),
                        IncomingClaim(
                            event_id=ghost_id,  # the bad one
                            field_name="altitude",
                            field_value=3000,
                        ),
                    ],
                )
            )

        assert ghost_id in exc_info.value.unknown_ids
        assert real_event_id not in exc_info.value.unknown_ids
        assert exc_info.value.code == "TENANT_CLAIM_UNKNOWN_EVENT"

    @pytest.mark.asyncio
    async def test_batch_with_all_valid_events_succeeds(self) -> None:
        uow = InMemoryUnitOfWork()
        tenant_id, source_id = await _seed_tenant(uow)
        event_id_a = _seed_event(uow)
        event_id_b = _seed_event(uow)

        run = await OpenTenantIngestionRun(uow).execute(
            OpenTenantIngestionRunInput(
                tenant_id=tenant_id,
                caller_tenant_id=tenant_id,
                caller_tenant_role=TenantRole.MEMBER.value,
                tenant_source_id=source_id,
            )
        )

        result = await SubmitTenantClaimsBatch(uow).execute(
            SubmitTenantClaimsBatchInput(
                tenant_id=tenant_id,
                caller_tenant_id=tenant_id,
                caller_tenant_role=TenantRole.MEMBER.value,
                run_id=run.id,
                claims=[
                    IncomingClaim(event_id=event_id_a, field_name="altitude", field_value=5000),
                    IncomingClaim(event_id=event_id_b, field_name="altitude", field_value=3000),
                ],
            )
        )
        assert result.inserted_count == 2

    @pytest.mark.asyncio
    async def test_batch_with_multiple_unknown_events_lists_all_in_error(self) -> None:
        uow = InMemoryUnitOfWork()
        tenant_id, source_id = await _seed_tenant(uow)

        run = await OpenTenantIngestionRun(uow).execute(
            OpenTenantIngestionRunInput(
                tenant_id=tenant_id,
                caller_tenant_id=tenant_id,
                caller_tenant_role=TenantRole.MEMBER.value,
                tenant_source_id=source_id,
            )
        )

        ghost_a, ghost_b = uuid4(), uuid4()

        from atlas.domain.tenancy.exceptions import TenantClaimUnknownEventError

        with pytest.raises(TenantClaimUnknownEventError) as exc_info:
            await SubmitTenantClaimsBatch(uow).execute(
                SubmitTenantClaimsBatchInput(
                    tenant_id=tenant_id,
                    caller_tenant_id=tenant_id,
                    caller_tenant_role=TenantRole.MEMBER.value,
                    run_id=run.id,
                    claims=[
                        IncomingClaim(event_id=ghost_a, field_name="f", field_value=1),
                        IncomingClaim(event_id=ghost_b, field_name="f", field_value=2),
                    ],
                )
            )

        assert exc_info.value.unknown_ids == {ghost_a, ghost_b}

    @pytest.mark.asyncio
    async def test_find_existing_ids_returns_only_present_events(self) -> None:
        """Verify FakeAccidentEventRepository.find_existing_ids directly."""
        uow = InMemoryUnitOfWork()
        real_id = _seed_event(uow)
        ghost_id = uuid4()

        result = await uow.events.find_existing_ids([real_id, ghost_id])
        assert result == {real_id}

    @pytest.mark.asyncio
    async def test_find_existing_ids_empty_input_returns_empty_set(self) -> None:
        uow = InMemoryUnitOfWork()
        result = await uow.events.find_existing_ids([])
        assert result == set()
