"""Echo cross-reference use cases.

Two classes compose the write path:

:class:`RequestEchoCrossReference`
    Validates the caller, resolves the hazard report, creates a
    ``TenantCrossrefResult`` row in ``PENDING`` state, and returns its id.
    Lightweight: one DB round-trip, commits immediately.  The actual
    matching happens in a separate step so the HTTP request isn't blocked
    by the corpus scan.

:class:`RunEchoCrossReference`
    Loads the corpus, runs the pure matching core, persists the ranked
    ``PrecedentMatch`` list into the PENDING result row (marking it
    ``COMPLETE``), then calls the Argus upsert for any ``STRONG``
    matches so they surface in the existing reviewer queue.  On any
    unhandled exception it marks the result ``FAILED`` and re-raises.

Together they preserve the same three isolation layers every other
tenant use case enforces:

1. Auth gate (``require_tenant_membership``) at the router.
2. Caller-vs-path ``tenant_id`` check in the use case.
3. Repository methods take ``tenant_id``; RLS (migration 046) enforces
   it at the DB level.

Corpus loading
--------------
``RunEchoCrossReference`` delegates corpus loading to a
``PrecedentCorpusLoader`` protocol so tests can inject a pre-built
corpus without a database.  The production implementation
(``InMemoryCorpusLoader``) reads public projection claims in a single
pass; it does NOT need the tenant GUC — it reads only public data, and
callers must pass a UoW whose session is not tenant-scoped (typically
the system-level ``create_uow()`` backed by a BYPASSRLS role).

Echo is a read against the public corpus crossed with a derived private
signal.  Nothing private is written to the public side.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import UUID

from atlas.application.crossref import (
    build_hazard_profile,
    cross_reference,
    precedent_record_from_ntsb_claims,
)
from atlas.application.services.metering import MeteringService
from atlas.application.unit_of_work import UnitOfWork
from atlas.application.use_cases.tenant_ingestion import _require_write_role
from atlas.domain.crossref.entities import EvidenceSupport, PrecedentMatch, PrecedentRecord
from atlas.domain.entities import ArgusSignal, ArgusSignalEvidence, OutboxEvent
from atlas.domain.enums import (
    ArgusEvidenceType,
    ArgusSeverity,
    ArgusSignalType,
)
from atlas.domain.metering.entities import MetricKind
from atlas.domain.services.argus_dedupe import make_argus_dedupe_key
from atlas.domain.tenancy.entities import CrossrefResultStatus, TenantCrossrefResult
from atlas.domain.tenancy.exceptions import CrossTenantAccessError, TenantNotFoundError
from atlas.infrastructure.observability.echo_metrics import (
    observe_matching_duration,
    record_run_complete,
    record_run_failed,
)

logger = logging.getLogger(__name__)


# ── Corpus loader protocol (dependency seam) ─────────────────────────────────


class PrecedentCorpusLoader(Protocol):
    """Load the public precedent corpus for matching.

    The production implementation reads public projection data.  Tests
    inject a corpus directly.  The loader is always called on the
    *public* UoW (no tenant GUC) — loading the corpus must never touch
    tenant-private data.
    """

    async def load(self, *, uow: UnitOfWork) -> list[PrecedentRecord]: ...


class InMemoryCorpusLoader:
    """Raw corpus loader: streams public projection claims in one pass.

    Reads every projected accident record, builds a ``PrecedentRecord`` from
    the canonical claim vocabulary, and returns the full list.  For 30k events
    this takes ~8s.  Use ``CachedCorpusLoader`` (the production default) to
    avoid paying this cost on every cross-reference run.
    """

    async def load(self, *, uow: UnitOfWork) -> list[PrecedentRecord]:
        records: list[PrecedentRecord] = []
        async for event_id, fields in uow.projections.iter_all_claims():
            try:
                rec = precedent_record_from_ntsb_claims(str(event_id), fields)
                records.append(rec)
            except Exception:
                logger.warning(
                    "Failed to build PrecedentRecord for event %s; skipping.",
                    event_id,
                    exc_info=True,
                )
        return records


@dataclass
class _CorpusCacheEntry:
    records: list[PrecedentRecord]
    built_at: datetime
    size: int


class CachedCorpusLoader:
    """Production corpus loader with in-process TTL cache.

    On the first call (or after the TTL expires) the full corpus is loaded from
    the public projection table (~8s for 30k events) and stored in memory.
    Subsequent calls within the TTL window return the cached list instantly,
    so concurrent cross-reference runs in the same process share one load.

    Thread / task safety: the cache is a module-level singleton updated under
    an ``asyncio.Lock``.  A single waiter loads; all others wait on the lock
    and reuse the result.  There is no thundering-herd: the lock is acquired
    before the staleness check, so only one coroutine ever loads at a time.

    TTL=0 disables caching (reload on every call) — useful in tests or when
    the corpus is updated frequently via the NTSB importer.
    """

    _cache: _CorpusCacheEntry | None = None
    _lock: asyncio.Lock | None = None

    def __init__(self, ttl_seconds: int | None = None) -> None:
        if ttl_seconds is None:
            from atlas.config import get_settings

            ttl_seconds = get_settings().echo_corpus_cache_ttl_seconds
        self._ttl = ttl_seconds

    def _get_lock(self) -> asyncio.Lock:
        # Lazily create the lock inside the running event loop so construction
        # is safe at import time (before any event loop exists).
        if CachedCorpusLoader._lock is None:
            CachedCorpusLoader._lock = asyncio.Lock()
        return CachedCorpusLoader._lock

    def _is_fresh(self, entry: _CorpusCacheEntry) -> bool:
        if self._ttl <= 0:
            return False
        age = (datetime.now(UTC) - entry.built_at).total_seconds()
        return age < self._ttl

    async def load(self, *, uow: UnitOfWork) -> list[PrecedentRecord]:
        async with self._get_lock():
            if CachedCorpusLoader._cache is not None and self._is_fresh(CachedCorpusLoader._cache):
                logger.debug(
                    "Echo corpus cache hit: %d records (age %.0fs)",
                    CachedCorpusLoader._cache.size,
                    (datetime.now(UTC) - CachedCorpusLoader._cache.built_at).total_seconds(),
                )
                return CachedCorpusLoader._cache.records

            logger.info("Echo corpus cache miss — loading from projection table.")
            t0 = datetime.now(UTC)
            records = await InMemoryCorpusLoader().load(uow=uow)
            elapsed = (datetime.now(UTC) - t0).total_seconds()
            CachedCorpusLoader._cache = _CorpusCacheEntry(
                records=records,
                built_at=datetime.now(UTC),
                size=len(records),
            )
            logger.info(
                "Echo corpus loaded: %d records in %.1fs — cached for %ds.",
                len(records),
                elapsed,
                self._ttl,
            )
            from atlas.infrastructure.observability.echo_metrics import record_corpus_loaded

            record_corpus_loaded(len(records), elapsed)
            return records

    @classmethod
    def invalidate(cls) -> None:
        """Invalidate the cache, forcing a reload on the next call.

        Call this after a bulk NTSB import to ensure the next cross-reference
        run picks up the new events rather than serving stale precedents.
        """
        cls._cache = None
        logger.info("Echo corpus cache invalidated.")


# ── Request use case (lightweight, HTTP-facing) ───────────────────────────────


@dataclass(frozen=True)
class RequestEchoCrossReferenceInput:
    tenant_id: UUID
    caller_tenant_id: UUID
    caller_tenant_role: str
    safety_report_id: UUID


@dataclass(frozen=True)
class RequestEchoCrossReferenceResult:
    crossref_result_id: UUID


class RequestEchoCrossReference:
    """Validate and enqueue an Echo cross-reference run.

    Creates a ``TenantCrossrefResult`` in PENDING state, enqueues an
    ``ECHO_CROSSREF_REQUESTED`` outbox event, and returns the result id.  The
    outbox worker later runs ``RunEchoCrossReference`` durably with retries.
    """

    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    async def execute(
        self, input: RequestEchoCrossReferenceInput
    ) -> RequestEchoCrossReferenceResult:
        if input.caller_tenant_id != input.tenant_id:
            raise CrossTenantAccessError(
                caller_tenant_id=input.caller_tenant_id,
                target_tenant_id=input.tenant_id,
            )

        _require_write_role(input.caller_tenant_role)

        # Verify the report exists and belongs to this tenant.
        report = await self._uow.tenant_safety_reports.get(
            tenant_id=input.tenant_id, report_id=input.safety_report_id
        )
        if report is None:
            raise TenantNotFoundError(
                f"Safety report {input.safety_report_id} not found in tenant {input.tenant_id}"
            )

        pending = TenantCrossrefResult(
            tenant_id=input.tenant_id,
            safety_report_id=input.safety_report_id,
            status=CrossrefResultStatus.PENDING,
        )
        await self._uow.tenant_crossref_results.add(tenant_id=input.tenant_id, result=pending)
        await self._uow.outbox.add(
            OutboxEvent(
                event_type="ECHO_CROSSREF_REQUESTED",
                aggregate_id=pending.id,
                payload={
                    "tenant_id": str(input.tenant_id),
                    "crossref_result_id": str(pending.id),
                    "safety_report_id": str(input.safety_report_id),
                },
            )
        )
        await self._uow.commit()
        return RequestEchoCrossReferenceResult(crossref_result_id=pending.id)


# ── Run use case (heavy, worker-facing) ──────────────────────────────────────


@dataclass(frozen=True)
class RunEchoCrossReferenceInput:
    tenant_id: UUID
    crossref_result_id: UUID
    limit: int = 20


@dataclass(frozen=True)
class RunEchoCrossReferenceResult:
    match_count: int
    strong_match_count: int
    argus_signals_upserted: int
    completed_at: datetime


class RunEchoCrossReference:
    """Run Echo matching for a PENDING crossref result and persist output.

    Boundary discipline:
    - ``tenant_uow``  — RLS-enforced, reads/writes tenant-private data.
    - ``public_uow``  — public corpus read, no tenant GUC, BYPASSRLS role.
      Must be passed by the caller so the session roles are explicit rather
      than implicit.
    - ``corpus_loader`` — defaults to ``InMemoryCorpusLoader``; tests inject
      a pre-built list.
    """

    def __init__(
        self,
        *,
        tenant_uow: UnitOfWork,
        public_uow: UnitOfWork,
        corpus_loader: PrecedentCorpusLoader | None = None,
        mark_failed_on_error: bool = True,
    ) -> None:
        self._tenant_uow = tenant_uow
        self._public_uow = public_uow
        self._loader = corpus_loader or CachedCorpusLoader()
        self._mark_failed_on_error = mark_failed_on_error

    async def execute(self, input: RunEchoCrossReferenceInput) -> RunEchoCrossReferenceResult:
        now = datetime.now(UTC)

        # 1. Resolve the pending result (tenant-scoped, RLS active).
        result = await self._tenant_uow.tenant_crossref_results.get(
            tenant_id=input.tenant_id, result_id=input.crossref_result_id
        )
        if result is None:
            raise TenantNotFoundError(
                f"CrossrefResult {input.crossref_result_id} not found in tenant {input.tenant_id}"
            )
        if result.status != CrossrefResultStatus.PENDING:
            raise ValueError(
                f"CrossrefResult {input.crossref_result_id} is "
                f"{result.status!r}; only PENDING results can be run."
            )

        # 2. Resolve the hazard report and build its profile.
        assert result.safety_report_id is not None
        report = await self._tenant_uow.tenant_safety_reports.get(
            tenant_id=input.tenant_id, report_id=result.safety_report_id
        )
        if report is None:
            await self._record_failure(input, now, "Safety report not found at run time")
            record_run_failed()
            raise TenantNotFoundError(f"Safety report {result.safety_report_id} vanished mid-run")

        # The report narrative has already been scrubbed by SubmitTenantSafetyReport.
        # We build the profile from it directly — only derived tokens are retained.
        profile = build_hazard_profile(scrubbed_narrative=report.narrative_markdown)

        if profile.is_empty():
            await self._record_failure(
                input,
                now,
                "Hazard profile is empty after normalisation; "
                "no usable signal to match against the public corpus.",
            )
            record_run_failed()
            raise ValueError("Empty hazard profile — cannot cross-reference")

        # 3. Load public corpus (no tenant context — public data only).
        try:
            corpus = await self._loader.load(uow=self._public_uow)
        except Exception as exc:
            await self._record_failure(input, now, f"Corpus load failed: {exc}")
            record_run_failed()
            raise

        # 4. Run matching (pure, deterministic).
        try:
            t_match = datetime.now(UTC)
            matches = cross_reference(profile, corpus, limit=input.limit)
            observe_matching_duration((datetime.now(UTC) - t_match).total_seconds())
        except Exception as exc:
            await self._record_failure(input, now, f"Matching failed: {exc}")
            record_run_failed()
            raise

        # 5. Serialise matches to JSONB-safe dicts.
        matches_json = [_serialise_match(m) for m in matches]
        matcher_config = {
            "weights": {"finding_categories": 0.5, "attributes": 0.2, "lexical": 0.3},
            "thresholds": {"strong": 0.60, "moderate": 0.35, "weak": 0.15},
            "limit": input.limit,
            "corpus_size": len(corpus),
        }

        # 6. Persist results (tenant-scoped, RLS active).
        await self._tenant_uow.tenant_crossref_results.mark_complete(
            tenant_id=input.tenant_id,
            result_id=input.crossref_result_id,
            matches_json=matches_json,
            matcher_config_json=matcher_config,
            match_count=len(matches),
            completed_at=now,
        )

        # 7. Emit Argus signals for STRONG matches (tenant-private, same UoW).
        strong = [m for m in matches if m.support == EvidenceSupport.STRONG]
        argus_count = 0
        for match in strong:
            argus_count += await self._upsert_argus_signal(
                input=input, match=match, crossref_result_id=input.crossref_result_id, now=now
            )

        # 8. Meter (atomic with the commit below).
        await MeteringService(self._tenant_uow).record(
            metric_kind=MetricKind.ECHO_CROSSREF_RUN,
            tenant_id=input.tenant_id,
            user_id=None,
            resource_id=input.crossref_result_id,
        )

        await self._tenant_uow.commit()
        record_run_complete()
        return RunEchoCrossReferenceResult(
            match_count=len(matches),
            strong_match_count=len(strong),
            argus_signals_upserted=argus_count,
            completed_at=now,
        )

    async def _upsert_argus_signal(
        self,
        *,
        input: RunEchoCrossReferenceInput,
        match: PrecedentMatch,
        crossref_result_id: UUID,
        now: datetime,
    ) -> int:
        """Upsert one ECHO_STRONG_PRECEDENT_MATCH signal; return 1 on upsert, 0 on skip."""
        # Dedupe key: one signal per (tenant, report, public event) triple.
        # A re-run that finds the same strong match updates the existing signal
        # rather than creating a duplicate.
        dedupe_key = make_argus_dedupe_key(
            ArgusSignalType.ECHO_STRONG_PRECEDENT_MATCH,
            "echo",
            [str(input.tenant_id), str(input.crossref_result_id), match.event_id],
        )
        signal = ArgusSignal(
            signal_type=ArgusSignalType.ECHO_STRONG_PRECEDENT_MATCH,
            # Echo matches are evidence support, not causal assessment.  We use
            # MEDIUM severity so operators see it without it drowning CRITICAL
            # safety signals (high-conflict records, fetch failure spikes).
            severity=ArgusSeverity.MEDIUM,
            # Score is similarity [0,1]; frame it as confidence in the match,
            # not as probability of recurrence.
            confidence=match.score,
            title=(f"Echo: STRONG precedent match — public event {match.event_id}"),
            description=(
                f"Echo found a STRONG public precedent (similarity={match.score:.2f}). "
                f"Occurred: {match.display_occurred_on or 'unknown'}, "
                f"{match.display_location or 'location unknown'}. "
                f"Aircraft: {match.display_aircraft or 'unknown'}. "
                f"Probable cause: {(match.display_probable_cause or '')[:200]}"
            ),
            source_engine="echo",
            dedupe_key=dedupe_key,
            first_detected_at=now,
            last_detected_at=now,
        )
        try:
            async with self._tenant_uow.savepoint():
                persisted, _created = await self._tenant_uow.argus_signals.upsert_signal(signal)
                await self._tenant_uow.argus_signal_evidence.upsert_evidence(
                    ArgusSignalEvidence(
                        signal_id=persisted.id,
                        evidence_type=ArgusEvidenceType.ECHO_CROSSREF_RESULT,
                        evidence_id=crossref_result_id,
                        engine="echo",
                        summary=(
                            f"CrossrefResult {crossref_result_id}: "
                            f"score={match.score:.3f}, support={match.support}"
                        ),
                    )
                )
            return 1
        except Exception:
            # G2 mirror: a signal upsert failure must not abort the whole run.
            # The savepoint (begin_nested) rolls back only this signal upsert,
            # leaving the outer transaction intact so the COMPLETE result and
            # metering write can still commit.  Losing a signal is recoverable
            # by re-running detection.
            logger.exception(
                "Echo: failed to upsert Argus signal for match %s; continuing.",
                match.event_id,
            )
            return 0

    async def _record_failure(
        self,
        input: RunEchoCrossReferenceInput,
        now: datetime,
        detail: str,
    ) -> None:
        """Record an immediate FAILED result when the caller owns failure policy.

        Durable worker executions leave the result PENDING while the outbox
        event remains retryable.  The worker marks the result FAILED only after
        the outbox event is dead-lettered.
        """
        if not self._mark_failed_on_error:
            return
        await self._mark_failed(input, now, detail)

    async def _mark_failed(
        self,
        input: RunEchoCrossReferenceInput,
        now: datetime,
        detail: str,
    ) -> None:
        try:
            await self._tenant_uow.tenant_crossref_results.mark_failed(
                tenant_id=input.tenant_id,
                result_id=input.crossref_result_id,
                error_detail=detail,
                completed_at=now,
            )
            await self._tenant_uow.commit()
        except Exception:
            logger.exception("Echo: failed to mark crossref result FAILED; swallowing.")


# ── Serialisation ─────────────────────────────────────────────────────────────


def _serialise_match(m: PrecedentMatch) -> dict[str, Any]:
    """Convert a PrecedentMatch to a JSONB-safe dict.

    frozensets become sorted lists (stable order for determinism).
    All values are JSON-primitive so Postgres JSONB round-trips cleanly.
    """
    return {
        "event_id": m.event_id,
        "score": m.score,
        "support": m.support.value,
        "components": [
            {
                "name": c.name,
                "weight": c.weight,
                "score": c.score,
                "detail": c.detail,
            }
            for c in m.components
        ],
        "shared_finding_categories": sorted(m.shared_finding_categories),
        "shared_terms": sorted(m.shared_terms),
        "display_occurred_on": m.display_occurred_on,
        "display_location": m.display_location,
        "display_aircraft": m.display_aircraft,
        "display_probable_cause": m.display_probable_cause,
    }


__all__ = [
    "CachedCorpusLoader",
    "InMemoryCorpusLoader",
    "PrecedentCorpusLoader",
    "RequestEchoCrossReference",
    "RequestEchoCrossReferenceInput",
    "RequestEchoCrossReferenceResult",
    "RunEchoCrossReference",
    "RunEchoCrossReferenceInput",
    "RunEchoCrossReferenceResult",
]
