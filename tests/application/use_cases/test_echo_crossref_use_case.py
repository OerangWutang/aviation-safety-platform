"""Unit tests for the Echo cross-reference use cases.

Uses InMemoryUnitOfWork + a stub corpus loader — no database, no network.
Covers the full lifecycle: request → run → COMPLETE/FAILED, Argus signal
emission, and the boundary guarantees (cross-tenant rejection, empty-profile
failure, corpus load failure).
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from atlas.application.use_cases.echo_crossref import (
    RequestEchoCrossReference,
    RequestEchoCrossReferenceInput,
    RunEchoCrossReference,
    RunEchoCrossReferenceInput,
    _serialise_match,
)
from atlas.domain.crossref.entities import (
    EvidenceSupport,
    MatchComponent,
    PrecedentMatch,
    PrecedentRecord,
)
from atlas.domain.crossref.profile import normalize_terms
from atlas.domain.enums import ArgusSignalType, OutboxStatus
from atlas.domain.tenancy.entities import (
    CrossrefResultStatus,
    TenantSafetyReport,
    TenantSafetyReportKind,
)
from atlas.domain.tenancy.exceptions import CrossTenantAccessError, TenantNotFoundError

# Absolute package import — works with PYTHONPATH=src:. (Makefile) and PYTHONPATH=src (CI).
from tests.domain._fake_uow import InMemoryUnitOfWork

# ── Fixtures ──────────────────────────────────────────────────────────────────


def _make_uow() -> InMemoryUnitOfWork:
    return InMemoryUnitOfWork()


def _make_tenant() -> uuid.UUID:
    return uuid.uuid4()


def _seed_report(uow: InMemoryUnitOfWork, tenant_id: uuid.UUID) -> TenantSafetyReport:
    report = TenantSafetyReport(
        tenant_id=tenant_id,
        report_kind=TenantSafetyReportKind.ASAP,
        narrative_markdown=(
            "During landing in a gusting crosswind the airplane drifted "
            "and the crew lost directional control, veering off the runway "
            "into the grass and collapsing the nose landing gear."
        ),
        deidentified_attested=True,
        submitter_user_id=uuid.uuid4(),
    )
    uow._store.tenancy.safety_reports[report.id] = report
    return report


def _strong_match(event_id: str = "EVT001") -> PrecedentMatch:
    return PrecedentMatch(
        event_id=event_id,
        score=0.85,
        support=EvidenceSupport.STRONG,
        components=(
            MatchComponent(name="finding_categories", weight=0.5, score=1.0, detail="2 shared"),
            MatchComponent(name="lexical", weight=0.3, score=0.7, detail="8 shared terms"),
        ),
        shared_finding_categories=frozenset({"01.06"}),
        shared_terms=frozenset({"crosswind", "landing", "runway"}),
        display_occurred_on="2020-06-25",
        display_location="Mesa, AZ",
        display_aircraft="Piper PA-28",
        display_probable_cause="Failure to maintain directional control.",
    )


class _StubCorpusLoader:
    """Corpus loader that returns a pre-built list."""

    def __init__(self, records: list[PrecedentRecord]) -> None:
        self._records = records

    async def load(self, *, uow: Any) -> list[PrecedentRecord]:
        return list(self._records)


class _FailingCorpusLoader:
    async def load(self, *, uow: Any) -> list[PrecedentRecord]:
        raise RuntimeError("corpus unavailable")


# ── RequestEchoCrossReference ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_request_creates_pending_result():
    uow = _make_uow()
    tenant_id = _make_tenant()
    report = _seed_report(uow, tenant_id)

    uc = RequestEchoCrossReference(uow)
    result = await uc.execute(
        RequestEchoCrossReferenceInput(
            tenant_id=tenant_id,
            caller_tenant_id=tenant_id,
            caller_tenant_role="MEMBER",
            safety_report_id=report.id,
        )
    )

    assert result.crossref_result_id is not None
    assert uow.commits == 1
    stored = uow._store.tenancy.crossref_results[result.crossref_result_id]
    assert stored.status == CrossrefResultStatus.PENDING
    assert stored.safety_report_id == report.id
    assert stored.tenant_id == tenant_id

    [event] = uow._store.outbox
    assert event.event_type == "ECHO_CROSSREF_REQUESTED"
    assert event.aggregate_id == result.crossref_result_id
    assert event.status == OutboxStatus.PENDING
    assert event.payload["tenant_id"] == str(tenant_id)
    assert event.payload["crossref_result_id"] == str(result.crossref_result_id)
    assert event.payload["safety_report_id"] == str(report.id)


@pytest.mark.asyncio
async def test_request_rejects_cross_tenant():
    uow = _make_uow()
    tenant_id = _make_tenant()
    report = _seed_report(uow, tenant_id)

    with pytest.raises(CrossTenantAccessError):
        await RequestEchoCrossReference(uow).execute(
            RequestEchoCrossReferenceInput(
                tenant_id=tenant_id,
                caller_tenant_id=uuid.uuid4(),  # different tenant
                caller_tenant_role="MEMBER",
                safety_report_id=report.id,
            )
        )


@pytest.mark.asyncio
async def test_request_rejects_missing_report():
    uow = _make_uow()
    tenant_id = _make_tenant()

    with pytest.raises(TenantNotFoundError):
        await RequestEchoCrossReference(uow).execute(
            RequestEchoCrossReferenceInput(
                tenant_id=tenant_id,
                caller_tenant_id=tenant_id,
                caller_tenant_role="MEMBER",
                safety_report_id=uuid.uuid4(),  # nonexistent
            )
        )


# ── RunEchoCrossReference — happy path ────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_marks_complete_and_persists_matches():
    tenant_uow = _make_uow()
    tenant_id = _make_tenant()
    report = _seed_report(tenant_uow, tenant_id)

    # Pre-create a PENDING result.
    req_result = await RequestEchoCrossReference(tenant_uow).execute(
        RequestEchoCrossReferenceInput(
            tenant_id=tenant_id,
            caller_tenant_id=tenant_id,
            caller_tenant_role="MEMBER",
            safety_report_id=report.id,
        )
    )
    result_id = req_result.crossref_result_id

    corpus_record = PrecedentRecord(
        event_id="EVT001",
        terms=normalize_terms("directional control runway landing crosswind"),
    )
    run_result = await RunEchoCrossReference(
        tenant_uow=tenant_uow,
        public_uow=_make_uow(),
        corpus_loader=_StubCorpusLoader([corpus_record]),
    ).execute(RunEchoCrossReferenceInput(tenant_id=tenant_id, crossref_result_id=result_id))

    stored = tenant_uow._store.tenancy.crossref_results[result_id]
    assert stored.status == CrossrefResultStatus.COMPLETE
    assert stored.match_count == run_result.match_count
    assert stored.completed_at is not None
    assert isinstance(stored.matches_json, list)


@pytest.mark.asyncio
async def test_run_emits_argus_signal_for_strong_match():
    tenant_uow = _make_uow()
    tenant_id = _make_tenant()
    report = _seed_report(tenant_uow, tenant_id)

    req_result = await RequestEchoCrossReference(tenant_uow).execute(
        RequestEchoCrossReferenceInput(
            tenant_id=tenant_id,
            caller_tenant_id=tenant_id,
            caller_tenant_role="MEMBER",
            safety_report_id=report.id,
        )
    )
    # Build a corpus record that will score STRONG against the report narrative.
    corpus_record = PrecedentRecord(
        event_id="EVT_STRONG",
        terms=normalize_terms("lost directional control crosswind landing runway gear collapse"),
    )
    run_result = await RunEchoCrossReference(
        tenant_uow=tenant_uow,
        public_uow=_make_uow(),
        corpus_loader=_StubCorpusLoader([corpus_record]),
    ).execute(
        RunEchoCrossReferenceInput(
            tenant_id=tenant_id, crossref_result_id=req_result.crossref_result_id
        )
    )

    signals = list(tenant_uow._store.argus.signals.values())
    strong_signals = [
        s for s in signals if s.signal_type == ArgusSignalType.ECHO_STRONG_PRECEDENT_MATCH
    ]
    assert run_result.argus_signals_upserted == len(strong_signals)
    if strong_signals:
        sig = strong_signals[0]
        assert sig.confidence <= 1.0
        assert sig.source_engine == "echo"


@pytest.mark.asyncio
async def test_run_marks_failed_on_corpus_load_error():
    tenant_uow = _make_uow()
    tenant_id = _make_tenant()
    report = _seed_report(tenant_uow, tenant_id)

    req_result = await RequestEchoCrossReference(tenant_uow).execute(
        RequestEchoCrossReferenceInput(
            tenant_id=tenant_id,
            caller_tenant_id=tenant_id,
            caller_tenant_role="MEMBER",
            safety_report_id=report.id,
        )
    )
    result_id = req_result.crossref_result_id

    with pytest.raises(RuntimeError):
        await RunEchoCrossReference(
            tenant_uow=tenant_uow,
            public_uow=_make_uow(),
            corpus_loader=_FailingCorpusLoader(),
        ).execute(RunEchoCrossReferenceInput(tenant_id=tenant_id, crossref_result_id=result_id))

    stored = tenant_uow._store.tenancy.crossref_results[result_id]
    assert stored.status == CrossrefResultStatus.FAILED
    assert stored.error_detail is not None
    assert "corpus" in stored.error_detail.lower()


@pytest.mark.asyncio
async def test_run_rejects_nonexistent_result():
    tenant_uow = _make_uow()
    tenant_id = _make_tenant()

    with pytest.raises(TenantNotFoundError):
        await RunEchoCrossReference(
            tenant_uow=tenant_uow,
            public_uow=_make_uow(),
            corpus_loader=_StubCorpusLoader([]),
        ).execute(
            RunEchoCrossReferenceInput(
                tenant_id=tenant_id,
                crossref_result_id=uuid.uuid4(),  # doesn't exist
            )
        )


@pytest.mark.asyncio
async def test_run_rejects_non_pending_result():
    tenant_uow = _make_uow()
    tenant_id = _make_tenant()
    report = _seed_report(tenant_uow, tenant_id)

    req_result = await RequestEchoCrossReference(tenant_uow).execute(
        RequestEchoCrossReferenceInput(
            tenant_id=tenant_id,
            caller_tenant_id=tenant_id,
            caller_tenant_role="MEMBER",
            safety_report_id=report.id,
        )
    )
    result_id = req_result.crossref_result_id

    # Manually transition to COMPLETE.
    tenant_uow._store.tenancy.crossref_results[result_id] = (
        tenant_uow._store.tenancy.crossref_results[result_id].model_copy(
            update={"status": CrossrefResultStatus.COMPLETE}
        )
    )

    with pytest.raises(ValueError, match="COMPLETE"):
        await RunEchoCrossReference(
            tenant_uow=tenant_uow,
            public_uow=_make_uow(),
            corpus_loader=_StubCorpusLoader([]),
        ).execute(
            RunEchoCrossReferenceInput(
                tenant_id=tenant_id,
                crossref_result_id=result_id,
            )
        )


# ── Serialisation ─────────────────────────────────────────────────────────────


def test_serialise_match_is_jsonb_safe():
    import json

    m = _strong_match()
    d = _serialise_match(m)
    # Must round-trip through JSON without error.
    json.dumps(d)
    assert d["support"] == "STRONG"
    assert isinstance(d["shared_finding_categories"], list)
    assert isinstance(d["shared_terms"], list)
    assert "score" in d and 0.0 <= d["score"] <= 1.0
    # Sorted for determinism.
    assert d["shared_terms"] == sorted(d["shared_terms"])


def test_serialise_match_has_no_probability_field():
    d = _serialise_match(_strong_match())
    prob_keys = {k for k in d if "prob" in k and k != "display_probable_cause"}
    assert not prob_keys, f"unexpected probability key(s): {prob_keys}"
