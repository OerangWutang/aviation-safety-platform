from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any
from uuid import UUID

from atlas.domain.causality.entities import (
    EventHfacsAttribution,
    HfacsCategory,
    HfacsSubcategory,
    SheloFactor,
    SheloFactorInteraction,
    SheloInteractionKind,
)
from atlas.domain.cms.entities import (
    ChangelogEntry,
    ChangelogEntryRevision,
    GlossaryTerm,
    GlossaryTermRevision,
    MethodologyPage,
    MethodologyPageRevision,
)
from atlas.domain.entities import (
    AccidentEvent,
    AccidentProjectionHistory,
    ArchiveManifest,
    ArgusSignal,
    ArgusSignalEvidence,
    ArgusSignalReview,
    ChronosEventLink,
    ChronosSequenceReview,
    ChronosTimelineEvent,
    Claim,
    ClaimConflict,
    ClaimHistory,
    ConflictActivityLogEntry,
    EventIdentityIndex,
    HermesCrawlTarget,
    HermesFetchedDocument,
    HermesFetchJob,
    HermesSource,
    HermesSourceChange,
    IngestionRun,
    OrionEntity,
    OrionEntityClaimLink,
    OrionEntityIdentifier,
    OrionEntityReview,
    OrionRelationship,
    OutboxEvent,
    PendingDuplicateReview,
    ProjectedAccidentRecord,
    RawSnapshot,
    Source,
)
from atlas.domain.enums import (
    ArgusSeverity,
    ArgusSignalStatus,
    ArgusSignalType,
    ChronosTimelineEventType,
    ConflictStatus,
    DuplicateReviewStatus,
    HermesFetchJobStatus,
    HermesTargetStatus,
    OrionEntityType,
    OutboxStatus,
)
from atlas.domain.maps.entities import (
    MapClusterResult,
    MapIndexEntry,
    MapQuery,
    MapSearchResult,
)
from atlas.domain.metering.entities import (
    MetricKind,
    UsageDailyRollup,
    UsageEvent,
    UsageSummaryRow,
)
from atlas.domain.nl_search.entities import NlQueryLog, SavedNlQuery
from atlas.domain.publication.entities import (
    PublicationStatus,
    PublicEventPage,
    PublicEventPageRevision,
)
from atlas.domain.search.entities import (
    SearchIndexEntry,
    SearchQuery,
    SearchResult,
)
from atlas.domain.tenancy.entities import (
    Tenant,
    TenantClaim,
    TenantClaimKind,
    TenantCrossrefResult,
    TenantEventAssociation,
    TenantEventOverlay,
    TenantIngestionRun,
    TenantIngestionRunStatus,
    TenantMembership,
    TenantSafetyReport,
    TenantSource,
)


@dataclass(frozen=True)
class HermesRecoveryOutcome:
    """Per-job outcome from ``HermesFetchJobRepository.recover_stale_running``.

    The repository returns one of these for each RUNNING job whose lease
    expired and was reclaimed.  Callers (typically the Hermes worker) use
    ``final_status`` to decide whether to emit a ``FETCH_FAILED`` source-
    change audit row: terminally failed recoveries (FAILED, attempts
    exhausted) should be visible in the target's change stream so that
    crash/expiry-driven failures are not hidden inside job records alone.
    Requeued recoveries (QUEUED, attempts remain) need no source-change
    event because the next run will produce one if it also fails.
    """

    job_id: UUID
    target_id: UUID
    final_status: HermesFetchJobStatus
    attempt_count: int


class SourceRepository(ABC):
    @abstractmethod
    async def get(self, id: UUID) -> Source | None: ...

    @abstractmethod
    async def get_by_name(self, name: str) -> Source | None: ...

    @abstractmethod
    async def get_all(self) -> list[Source]: ...

    @abstractmethod
    async def get_by_ids(self, ids: list[UUID]) -> list[Source]: ...

    @abstractmethod
    async def add(self, source: Source) -> None: ...

    @abstractmethod
    async def update_field_mapping(
        self, source_id: UUID, field_mapping: dict[str, str]
    ) -> Source | None:
        """Replace ``Source.field_mapping_json`` with the given mapping.

        Returns the updated source, or ``None`` if no row exists for
        ``source_id``.  Callers (use cases) are expected to validate the
        mapping targets against ``RequiredField`` before invoking this method
        - the repository is a pure persistence concern and does not enforce
        domain rules.
        """
        ...


class RawSnapshotRepository(ABC):
    @abstractmethod
    async def add(self, snapshot: RawSnapshot) -> None: ...

    @abstractmethod
    async def get(self, snapshot_id: UUID) -> RawSnapshot | None:
        """Return the snapshot row for ``snapshot_id`` if it exists.

        Pure read.  Added in Phase 11 for the source-verification audit
        endpoint, which needs the snapshot's ``raw_payload_hash`` so a
        reader can independently re-hash the original source record and
        confirm the chain is intact.
        """
        ...

    @abstractmethod
    async def try_add_unique(self, snapshot: RawSnapshot) -> bool: ...

    @abstractmethod
    async def find_by_source_run(
        self,
        source_id: UUID,
        ingestion_run_id: UUID,
    ) -> RawSnapshot | None:
        """Return any snapshot already recorded for this source/run pair.

        Used to enforce idempotency-key semantics before inserting a new
        snapshot.  A reused key with a different payload must be rejected rather
        than recorded as another ingestion under the same run identity.
        """
        ...

    @abstractmethod
    async def update_ingestion_result(
        self,
        snapshot_id: UUID,
        result_json: dict[str, Any],
    ) -> None:
        """Persist the completed ingestion result for exact idempotent replay."""
        ...

    @abstractmethod
    async def find_latest_by_source_record_id(
        self,
        source_id: UUID,
        source_record_id: str,
    ) -> RawSnapshot | None:
        """Return the most recently created snapshot for this (source, record id) pair.

        Used by diagnostics and legacy callers. Continuity should prefer
        ``find_latest_event_id_by_source_record_id`` so an orphan snapshot
        without claims cannot hide an older valid event owner.
        """
        ...

    @abstractmethod
    async def find_latest_event_id_by_source_record_id(
        self,
        source_id: UUID,
        source_record_id: str,
    ) -> UUID | None:
        """Return the latest event id attached to this source record.

        The lookup skips orphan snapshots with no claims. This protects
        source-record continuity from a crash window where a raw snapshot row
        exists but no claims were committed yet: a later correction should still
        attach to the latest older snapshot that has actual event ownership.
        """
        ...

    @abstractmethod
    async def lock_for_source_record_correction(
        self,
        source_id: UUID,
        source_record_id: str,
    ) -> None:
        """Acquire a transaction-scoped advisory lock for this (source, record) pair.

        Two concurrent corrections for the same source_record_id must not both
        read the same ``prior`` snapshot, each supersede the old claims, and each
        insert new ones - that race leaves two active claim sets for the same
        field.  This lock serialises them: the second transaction blocks here
        until the first commits, then finds the updated snapshot and claim state.

        The lock key is a hash of ``'{source_id}:{source_record_id}'``.

        The in-memory fake is a no-op because unit tests run single-threaded.
        """
        ...


class IngestionRunRepository(ABC):
    @abstractmethod
    async def add(self, run: IngestionRun) -> None: ...

    @abstractmethod
    async def get(self, id: UUID) -> IngestionRun | None: ...

    @abstractmethod
    async def update_status(
        self, id: UUID, status: str, finished_at: object | None = None
    ) -> None: ...

    @abstractmethod
    async def ensure_started(self, id: UUID, source_id: UUID) -> None: ...


class AccidentEventRepository(ABC):
    @abstractmethod
    async def get(self, id: UUID) -> AccidentEvent | None: ...

    @abstractmethod
    async def lock_for_update(self, id: UUID) -> AccidentEvent | None:
        """Lock and return an event row for the duration of the transaction.

        Writers that are about to attach claims to an event and writers that
        are about to merge that event must both acquire this row lock.  This
        closes the merge x ingestion TOCTOU window: after the lock is acquired,
        callers must re-check ``merged_into_event_id`` before writing evidence.

        The SQL implementation uses ``SELECT ... FOR UPDATE``.  The in-memory
        fake is a no-op/read because unit tests are single-threaded; real
        contention is covered by integration tests against PostgreSQL.
        """
        ...

    @abstractmethod
    async def add(self, event: AccidentEvent) -> None: ...

    @abstractmethod
    async def save(self, event: AccidentEvent) -> None:
        """Persist a mutated AccidentEvent (e.g. after setting merged_into_event_id)."""
        ...

    @abstractmethod
    async def update(self, event: AccidentEvent) -> None:
        """Persist changes to an existing AccidentEvent (e.g. merged_into_event_id)."""
        ...

    @abstractmethod
    async def list_all_ids(self) -> list[UUID]: ...

    @abstractmethod
    async def list_ids_page(self, limit: int, offset: int = 0) -> list[UUID]: ...

    @abstractmethod
    async def list_ids_after_keyset(self, after_id: UUID | None, limit: int) -> list[UUID]:
        """Return event ids ordered by id after the supplied cursor.

        Used by maintenance rebuilds to avoid offset drift while scanning a
        mutating table.  Concurrent inserts whose UUID sorts before the cursor
        may be handled by a later rebuild; callers should treat this as a
        bounded maintenance pass, not a serializable snapshot.
        """
        ...

    @abstractmethod
    async def lock_for_reprojection(self, event_id: UUID) -> None:
        """Take a transaction-scoped lock that serializes reprojection of one event.

        Concurrent calls with the same ``event_id`` block until the holding
        transaction commits or rolls back. The lock is released automatically
        on transaction end. This prevents two reprojection workers from racing
        on ``projection_version + 1`` and producing the same version number,
        which would otherwise turn a normal concurrency window into a unique-
        constraint exception path.

        SQL implementation uses ``pg_advisory_xact_lock`` keyed on the event
        UUID (lossy 64-bit hash); the in-memory fake is a no-op because tests
        run single-threaded inside one event loop.
        """
        ...

    @abstractmethod
    async def try_atomic_merge(self, source_event_id: UUID, target_event_id: UUID) -> bool:
        """Atomically claim a merge by setting merged_into_event_id.

        Executes a single conditional UPDATE:

            UPDATE accident_events
            SET merged_into_event_id = :target
            WHERE id = :source AND merged_into_event_id IS NULL
            RETURNING id

        Returns ``True`` if the row was successfully claimed (exactly one row
        updated), ``False`` if the row was already merged (zero rows updated
        because the WHERE predicate did not match).

        This prevents two concurrent merge requests from both reading
        ``is_merged == False`` and then both copying active claims.  The first
        request to commit wins; the second sees ``merged_into_event_id IS NOT
        NULL`` and gets ``False`` back, which ``MergeDuplicateEvents`` converts
        into ``EventAlreadyMergedError``.

        The in-memory fake uses a simple conditional assignment - safe because
        unit tests run single-threaded inside one event loop.
        """
        ...

    async def find_existing_ids(self, ids: list[UUID]) -> set[UUID]:
        """Return the subset of ``ids`` that exist in the event table.

        Used by :class:`SubmitTenantClaimsBatch` to validate event references
        before the bulk INSERT so a FK violation surfaces as a clean 422 rather
        than a 500 IntegrityError.

        A single ``WHERE id = ANY(:ids)`` query so the cost is O(1) round trips
        regardless of batch size.  The default implementation falls back to N
        individual ``get()`` calls for compatibility with fakes that do not
        override it; production implementations should override with a bulk query.
        """
        result: set[UUID] = set()
        for event_id in ids:
            event = await self.get(event_id)
            if event is not None:
                result.add(event_id)
        return result


class ClaimRepository(ABC):
    @abstractmethod
    async def get(self, id: UUID) -> Claim | None: ...

    @abstractmethod
    async def lock_for_update(self, id: UUID) -> Claim | None:
        """Lock and return a claim row for the duration of the transaction.

        Conflict resolution uses this immediately before committing a selected
        source winner so a concurrent ingestion/merge cannot supersede that
        claim between eligibility validation and writing ``winning_claim_id``.
        The SQL implementation uses ``SELECT ... FOR UPDATE``; the in-memory
        fake is a normal read because unit tests are single-threaded.
        """
        ...

    @abstractmethod
    async def get_many(self, claim_ids: list[UUID]) -> list[Claim]: ...

    @abstractmethod
    async def add(self, claim: Claim) -> None: ...

    @abstractmethod
    async def update(self, claim: Claim) -> None: ...

    @abstractmethod
    async def find_active_by_event(self, event_id: UUID) -> list[Claim]: ...

    @abstractmethod
    async def find_all_by_event(
        self,
        event_id: UUID,
        *,
        limit: int | None = None,
        after_id: UUID | None = None,
    ) -> list[Claim]: ...

    @abstractmethod
    async def find_event_id_by_raw_snapshot_id(self, raw_snapshot_id: UUID) -> UUID | None: ...

    @abstractmethod
    async def find_active_by_event_field(self, event_id: UUID, field_name: str) -> list[Claim]: ...

    @abstractmethod
    async def find_active_by_source_record(
        self,
        source_id: UUID,
        source_record_id: str,
    ) -> list[Claim]:
        """Return active claims produced by a source's stable record id.

        Re-ingesting the same source record is a replacement/versioning event,
        not independent evidence.  The ingestion use case uses this lookup to
        supersede the previous active version of each updated field.
        """
        ...

    @abstractmethod
    async def bulk_supersede(self, claim_ids: list[UUID], by_claim_id: UUID) -> list[Claim]: ...

    @abstractmethod
    async def find_superseded_by(self, by_claim_id: UUID) -> list[Claim]: ...

    @abstractmethod
    async def bulk_unsupersede(self, claim_ids: list[UUID]) -> list[Claim]: ...

    @abstractmethod
    async def count_total(self) -> int:
        """Return the total number of claim rows across all events."""
        ...


class ClaimHistoryRepository(ABC):
    @abstractmethod
    async def add(self, history: ClaimHistory) -> None: ...

    @abstractmethod
    async def find_by_event(
        self,
        event_id: UUID,
        *,
        limit: int | None = None,
        after_id: UUID | None = None,
    ) -> list[ClaimHistory]: ...


class ConflictRepository(ABC):
    @abstractmethod
    async def get(self, id: UUID) -> ClaimConflict | None: ...

    @abstractmethod
    async def add(self, conflict: ClaimConflict) -> None: ...

    @abstractmethod
    async def try_add_open(self, conflict: ClaimConflict) -> bool:
        """Insert a new OPEN conflict.

        Returns True if inserted, False if an OPEN conflict for the same
        (event_id, field_name) pair already exists (concurrent ingestion race).
        Callers that get False should reload via ``find_open_by_event_field``
        and merge into the existing conflict.
        """
        ...

    @abstractmethod
    async def save(self, conflict: ClaimConflict) -> None: ...

    @abstractmethod
    async def find_by_event(
        self,
        event_id: UUID,
        limit: int | None = None,
        offset: int = 0,
        after_id: UUID | None = None,
    ) -> list[ClaimConflict]: ...

    @abstractmethod
    async def close_event_conflicts_as_merged(
        self,
        event_id: UUID,
        *,
        note: str,
    ) -> list[ClaimConflict]:
        """Tombstone OPEN conflicts attached to an absorbed/merged event.

        MergeDuplicateEvents moves/supersedes source-event claims, so leaving
        source-event ClaimConflict rows OPEN creates orphaned conflicts in admin
        queues and stale dispute markers during rebuilds. Implementations should
        mark only OPEN rows RESOLVED with a SYSTEM_AUTO_CLOSED reason and return
        the updated rows so callers can append activity-log audit rows with the
        actual persisted version. Already-resolved curator decisions should be
        left untouched; their historical meaning is not changed by a later merge.
        """
        ...

    @abstractmethod
    async def find_by_event_field(
        self, event_id: UUID, field_name: str
    ) -> ClaimConflict | None: ...

    @abstractmethod
    async def find_open_by_event_field(
        self, event_id: UUID, field_name: str
    ) -> ClaimConflict | None: ...

    @abstractmethod
    async def get_claim_ids_for_conflict(self, conflict_id: UUID) -> list[UUID]: ...

    @abstractmethod
    async def add_claim_to_conflict(self, conflict_id: UUID, claim_id: UUID) -> None: ...

    @abstractmethod
    async def update_with_version_check(
        self, conflict_id: UUID, expected_version: int, updates: dict[str, Any]
    ) -> ClaimConflict | None: ...

    @abstractmethod
    async def find_resolved_by_winning_claim(self, claim_id: UUID) -> ClaimConflict | None:
        """Return the RESOLVED conflict whose winning_claim_id equals claim_id, or None.

        Used when a source-record re-ingestion supersedes a claim that was
        previously chosen as the winner of a resolved conflict.  The caller
        must then either update winning_claim_id to the replacement claim (same
        value) or reopen the conflict (values now disagree).
        """
        ...

    @abstractmethod
    async def find_resolved_by_winning_claims(
        self, claim_ids: list[UUID]
    ) -> dict[UUID, ClaimConflict]:
        """Return a ``winning_claim_id -> conflict`` map for a batch of claim ids.

        Preferred over repeated ``find_resolved_by_winning_claim`` calls when
        multiple claims are superseded at once.  Returns only the subset of
        claim_ids that are the winner of a RESOLVED conflict.
        """
        ...

    @abstractmethod
    async def count_by_status(self, status: ConflictStatus) -> int:
        """Return the number of conflicts in the given status."""
        ...

    @abstractmethod
    async def count_open_conflicts_per_event(
        self,
        min_count: int = 3,
        limit: int = 50,
    ) -> list[tuple[UUID, int]]:
        """Return ``(event_id, open_conflict_count)`` pairs for events whose
        number of OPEN ``claim_conflicts`` is at least ``min_count``.

        Ordered by count DESC then event_id ASC so pagination is deterministic
        across calls.  ``limit`` caps the result; callers that need more
        should re-run with a higher limit (the natural fan-out per event in
        practice is small enough that pagination is not yet needed).

        Used by ``RunArgusSignalDetection`` to fan out
        ``HIGH_CONFLICT_ACCIDENT_RECORD`` signals.  ``min_count`` must be
        ``>= 2`` — a single open conflict is not, by definition, a "high
        conflict" record.  Implementations should raise ``ValueError`` on
        smaller values.
        """
        ...


class ConflictActivityLogRepository(ABC):
    @abstractmethod
    async def add(self, entry: ConflictActivityLogEntry) -> None: ...

    @abstractmethod
    async def next_sequence(self, conflict_id: UUID) -> int: ...

    @abstractmethod
    async def find_by_conflict(
        self,
        conflict_id: UUID,
        *,
        limit: int | None = None,
        after_id: UUID | None = None,
    ) -> list[ConflictActivityLogEntry]: ...

    @abstractmethod
    async def find_by_event(
        self,
        event_id: UUID,
        *,
        limit: int | None = None,
        after_id: UUID | None = None,
    ) -> list[ConflictActivityLogEntry]: ...

    @abstractmethod
    async def latest_for_conflict(self, conflict_id: UUID) -> ConflictActivityLogEntry | None: ...


class ProjectionRepository(ABC):
    @abstractmethod
    async def get(self, event_id: UUID) -> ProjectedAccidentRecord | None: ...

    @abstractmethod
    async def upsert(self, projection: ProjectedAccidentRecord) -> None: ...

    @abstractmethod
    async def delete(self, event_id: UUID) -> None:
        """Remove a projected read-model row if one exists."""
        ...

    @abstractmethod
    async def find_candidates_for_event_matching(
        self,
        event_date: str,
        limit: int = 50,
    ) -> list[ProjectedAccidentRecord]:
        """Return projected records whose event_date field is within ±1 day.

        Used by the event-matching service before in-memory scoring.
        ``event_date`` must be a normalised YYYY-MM-DD string.
        """
        ...

    @abstractmethod
    async def count_total(self) -> int:
        """Return the total number of projected accident records."""
        ...

    @abstractmethod
    def iter_all_claims(self) -> AsyncIterator[tuple[UUID, dict[str, Any]]]:
        """Yield (event_id, fields) pairs for every projected record.

        Used by Echo's ``InMemoryCorpusLoader`` to build the public
        precedent corpus.  Streams rows in event_id order so memory
        usage is bounded regardless of corpus size — callers consume
        the iterator rather than materialising the full table.

        The yielded ``fields`` dict is the raw JSONB from the projection;
        it is the same shape as the canonical NTSB claim vocabulary (the
        NTSB importer writes canonical field names into projection fields).
        """
        ...


class ProjectionHistoryRepository(ABC):
    @abstractmethod
    async def add(self, history: AccidentProjectionHistory) -> None: ...

    @abstractmethod
    async def find_by_event(
        self,
        event_id: UUID,
        *,
        limit: int | None = None,
        after_id: UUID | None = None,
    ) -> list[AccidentProjectionHistory]: ...

    @abstractmethod
    async def find_by_outbox_event(
        self, outbox_event_id: UUID
    ) -> AccidentProjectionHistory | None: ...


class OutboxRepository(ABC):
    @abstractmethod
    async def add(self, event: OutboxEvent) -> None: ...

    @abstractmethod
    async def fetch_and_lock_pending(
        self, limit: int, worker_id: str, max_attempts: int = 5
    ) -> list[OutboxEvent]: ...

    @abstractmethod
    async def update_status(
        self,
        event_id: UUID,
        status: OutboxStatus,
        attempt_count: int,
        last_error: str | None = None,
        next_attempt_at: datetime | None = None,
        expected_worker_id: str | None = None,
        expected_attempt_count: int | None = None,
    ) -> bool:
        """Update the terminal/intermediate state of an outbox event.

        When ``expected_worker_id`` and ``expected_attempt_count`` are provided,
        the update is fenced: it succeeds only if the row's lock matches that
        worker AND the attempt counter is unchanged since the lock was acquired.
        Returns True iff the row was updated.

        Without fencing parameters, the call behaves as an unconditional update
        (used for unit tests and for paths that have already verified ownership).
        Production callers SHOULD pass the fencing parameters.
        """
        ...

    @abstractmethod
    async def list_recent(self, limit: int = 50) -> list[OutboxEvent]: ...

    async def requeue_stale_locked_with_dead_letters(
        self, stale_after_minutes: int = 10, max_attempts: int = 5
    ) -> tuple[int, list[OutboxEvent]]:
        """Requeue stale locks and return any events newly moved to DEAD_LETTER.

        The count-only API below is kept for existing callers. Workers that own
        user-visible job state should use this richer method so stale-lock
        dead-lettering can also mark the corresponding result as FAILED.
        """
        count = await self.requeue_stale_locked(
            stale_after_minutes=stale_after_minutes,
            max_attempts=max_attempts,
        )
        return count, []

    @abstractmethod
    async def requeue_stale_locked(
        self, stale_after_minutes: int = 10, max_attempts: int = 5
    ) -> int:
        """Requeue stale PROCESSING events to PENDING for retry.

        Events still under their ``max_attempts`` budget become PENDING again.
        Events that have exhausted their attempts move to DEAD_LETTER instead,
        so they cannot remain stuck in PROCESSING forever.

        Returns the total number of rows transitioned (PENDING + DEAD_LETTER).
        """
        ...

    @abstractmethod
    async def count_by_status(self, status: OutboxStatus) -> int:
        """Return the number of outbox events in the given status."""
        ...

    @abstractmethod
    async def oldest_unprocessed_age_seconds(self) -> float | None:
        """Return the age of the oldest non-terminal outbox row in seconds.

        Counts PENDING, PROCESSING, and FAILED rows. ``None`` means there is no
        current backlog. This is the most useful worker-health signal for
        small self-hosted deployments: a stuck worker shows up as growing age
        even if the process has not crashed.
        """
        ...

    @abstractmethod
    async def record_worker_heartbeat(
        self, worker_id: str, *, successful_batch: bool = False
    ) -> None:
        """Record that an outbox worker loop is alive and optionally made progress."""
        ...

    @abstractmethod
    async def worker_heartbeat_age_seconds(self) -> float | None:
        """Return age in seconds since the newest worker loop heartbeat, if any."""
        ...

    @abstractmethod
    async def worker_successful_batch_age_seconds(self) -> float | None:
        """Return age in seconds since the newest successful worker batch, if any."""
        ...


class ArchiveManifestRepository(ABC):
    @abstractmethod
    async def add(self, manifest: ArchiveManifest) -> None: ...


class PendingDuplicateReviewRepository(ABC):
    @abstractmethod
    async def add(self, review: PendingDuplicateReview) -> PendingDuplicateReview | None:
        """Insert review if absent and return the stored row.

        Implementations should enforce unordered PENDING pair uniqueness at the
        database/storage boundary so concurrent ingestion cannot create duplicate
        review tasks for the same event pair.
        """
        ...

    @abstractmethod
    async def get(self, id: UUID) -> PendingDuplicateReview | None: ...

    @abstractmethod
    async def find_pending_for_event(self, event_id: UUID) -> list[PendingDuplicateReview]:
        """Return all PENDING reviews involving either side of this event."""
        ...

    @abstractmethod
    async def find_pending_pair(
        self, event_id_a: UUID, event_id_b: UUID
    ) -> PendingDuplicateReview | None:
        """Return the current PENDING review for this unordered pair, if any."""
        ...

    @abstractmethod
    async def find_existing_pair(
        self, event_id_a: UUID, event_id_b: UUID
    ) -> PendingDuplicateReview | None:
        """Return a deterministic historical review for this ordered or reversed pair.

        Implementations should prefer an active PENDING review if one exists,
        then fall back to the newest historical row for operator/debug views.
        """
        ...

    @abstractmethod
    async def list_pending(
        self, *, limit: int, after_id: UUID | None = None
    ) -> list[PendingDuplicateReview]:
        """Return up to *limit* PENDING reviews ordered newest-first.

        ``after_id`` is an opaque keyset cursor from the previous page.
        Implementations should order by ``created_at DESC, id DESC`` so rows
        remain stable when many reviews are created in the same timestamp.
        """
        ...

    @abstractmethod
    async def update_status(
        self,
        id: UUID,
        status: DuplicateReviewStatus,
        resolved_by: UUID | None = None,
        resolution_note: str | None = None,
    ) -> PendingDuplicateReview | None: ...


class EventIdentityIndexRepository(ABC):
    """Synchronous identity substrate for event matching.

    Written in the same database transaction as ingestion so that the second
    ingestion of the same accident can find the first event's identity fields
    immediately - before any projection has been built.

    The advisory lock serializes concurrent ingestions whose key fields
    (event_date + registration) are identical, eliminating the race window
    where two transactions both find an empty index and both create new events.
    """

    @abstractmethod
    async def upsert(self, entry: EventIdentityIndex) -> None:
        """Write or update the identity record for an event.

        On conflict (same event_id): update non-null fields, accumulate
        source_record_ids (union, not replace), and bump updated_at.
        """
        ...

    @abstractmethod
    async def enrich_identity_index_from_alias(
        self,
        entry: EventIdentityIndex,
    ) -> None:
        """Enrich a canonical identity row from a merged-event alias match.

        This is the target-preserving companion to ``upsert``.  Scalar identity
        fields keep the canonical row's existing non-null values and only fill
        gaps from ``entry``.  Array fields are always unioned so source record
        IDs and historical registration aliases are retained.

        Use this when an ingestion matched a merged/absorbed event and has been
        canonicalized to the surviving event.  The incoming alias data should
        be searchable on the canonical row, but it must not overwrite canonical
        primary identity fields such as the current registration.
        """
        ...

    @abstractmethod
    async def merge_identity_index(
        self,
        source_event_id: UUID,
        target_event_id: UUID,
    ) -> None:
        """Union source-event identity aliases into the canonical target row.

        Scalar fields preserve target non-null values and fill only target gaps
        from the source row.  Array fields are unioned so no historical
        source_record_id or registration alias is lost.

        The source identity row remains in place as a historical alias; callers
        canonicalize merged candidates before writing claims.
        """
        ...

    @abstractmethod
    async def find_candidates(
        self,
        event_date_norm: str,
        limit: int = 50,
    ) -> list[EventIdentityIndex]:
        """Return identity entries whose event_date_norm is within ±1 day.

        Merged events are intentionally included as historical identity aliases.
        The ingestion use case resolves any candidate through
        ``_canonical_event_for`` before attaching claims or creating duplicate
        reviews, so aliases remain searchable without allowing writes to an
        absorbed event. Results are ordered newest-first so recently active
        identities score first.
        """
        ...

    @abstractmethod
    async def lock_for_identity_resolution(
        self,
        event_date_norm: str,
        registration_norm: str | None,
    ) -> None:
        """Acquire a transaction-scoped advisory lock for this identity key.

        Serializes ``_resolve_or_create_event`` calls whose normalised
        (event_date, registration) matches the same key.  Two concurrent
        ingestions of the same accident will block at this call; the second
        will find the index entry created by the first rather than both
        creating independent new events.

        The lock is keyed on ``hashtextextended('{date}:{reg}', 0)`` so it
        is scoped tightly to the identity key and does not contend across
        different accidents.  The in-memory fake is a no-op.
        """
        ...

    @abstractmethod
    async def find_by_registration(
        self,
        registration_norm: str,
        event_date_norm: str | None = None,
    ) -> list[EventIdentityIndex]:
        """Find identity entries by primary or historical registration alias.

        ``registration_norm`` is accepted as raw registration text or as the
        stored normalized form; implementations normalize defensively before
        querying. Searches both ``registration_norm`` (primary scalar) and
        ``registration_norms`` (historical alias list) so that the correct
        event is found even when more than ``find_candidates``'s limit of 50
        entries share the same date.

        When ``event_date_norm`` is supplied, results are filtered to the
        ±1-day window (same window used by ``find_candidates``).

        Used by ``_resolve_or_create_event`` alongside ``find_candidates`` to
        guarantee that the registration-based lookup is never blocked by the
        50-row date-window cap. Implementations should order live/canonical
        events before merged aliases, then newest identity rows first.
        """
        ...


# ── Orion Repository Interfaces ───────────────────────────────────────────────


class OrionEntityRepository(ABC):
    @abstractmethod
    async def get(self, id: UUID) -> OrionEntity | None: ...

    @abstractmethod
    async def add(self, entity: OrionEntity) -> None: ...

    @abstractmethod
    async def lock_for_identifier_identity(
        self,
        entity_type: OrionEntityType,
        identifier_type: str,
        normalized_value: str,
    ) -> None:
        """Acquire a transaction-scoped lock for a strong Orion identifier.

        Entity extraction resolves by identifier, then creates a new entity when
        none exists.  Without serializing that read/create path, two workers can
        create duplicate active entities for the same strong identifier before
        either attaches the identifier row.  SQL implementations should use a
        dedicated advisory-lock namespace keyed by
        ``entity_type:identifier_type:normalized_value``.  In-memory fakes may
        no-op because unit tests are single-threaded.
        """
        ...

    @abstractmethod
    async def save(self, entity: OrionEntity) -> None: ...

    @abstractmethod
    async def find_by_identifier(
        self,
        entity_type: OrionEntityType,
        identifier_type: str,
        normalized_value: str,
    ) -> OrionEntity | None: ...

    @abstractmethod
    async def find_by_canonical_name(
        self,
        entity_type: OrionEntityType,
        normalized_name: str,
    ) -> OrionEntity | None: ...

    @abstractmethod
    async def search(
        self,
        query: str,
        entity_type: OrionEntityType | None = None,
        limit: int = 25,
    ) -> list[OrionEntity]: ...

    @abstractmethod
    async def list_for_event(self, event_id: UUID) -> list[OrionEntity]: ...


class OrionIdentifierRepository(ABC):
    @abstractmethod
    async def add(self, identifier: OrionEntityIdentifier) -> None: ...

    @abstractmethod
    async def try_add(self, identifier: OrionEntityIdentifier) -> bool:
        """Add identifier if it does not already exist."""
        ...

    @abstractmethod
    async def list_for_entity(self, entity_id: UUID) -> list[OrionEntityIdentifier]: ...


class OrionRelationshipRepository(ABC):
    @abstractmethod
    async def add(self, relationship: OrionRelationship) -> None: ...

    @abstractmethod
    async def upsert_relationship(
        self, relationship: OrionRelationship
    ) -> tuple[OrionRelationship, bool]:
        """Insert or return an existing identical relationship.

        accident_event_id is required on all v0.1 relationships.
        Returns (relationship, created) where created=False if already existed.
        """
        ...

    @abstractmethod
    async def list_for_entity(self, entity_id: UUID) -> list[OrionRelationship]: ...

    @abstractmethod
    async def list_for_event(self, event_id: UUID) -> list[OrionRelationship]: ...


class OrionEntityClaimLinkRepository(ABC):
    @abstractmethod
    async def add(self, link: OrionEntityClaimLink) -> None: ...

    @abstractmethod
    async def list_for_entity(self, entity_id: UUID) -> list[OrionEntityClaimLink]: ...


class OrionEntityReviewRepository(ABC):
    @abstractmethod
    async def add(self, review: OrionEntityReview) -> None: ...

    @abstractmethod
    async def list_pending(self, limit: int = 50, offset: int = 0) -> list[OrionEntityReview]: ...

    @abstractmethod
    async def mark_merged(
        self, review_id: UUID, resolved_by: UUID, note: str | None = None
    ) -> None: ...

    @abstractmethod
    async def mark_rejected(
        self, review_id: UUID, resolved_by: UUID, note: str | None = None
    ) -> None: ...


# ── Chronos Repository Interfaces ─────────────────────────────────────────────


class ChronosTimelineEventRepository(ABC):
    @abstractmethod
    async def get(self, id: UUID) -> ChronosTimelineEvent | None: ...

    @abstractmethod
    async def add(self, event: ChronosTimelineEvent) -> None: ...

    @abstractmethod
    async def upsert_event(self, event: ChronosTimelineEvent) -> tuple[ChronosTimelineEvent, bool]:
        """Insert or return existing event matching (accident_event_id, event_type, raw_value)."""
        ...

    @abstractmethod
    async def list_for_accident_event(
        self, accident_event_id: UUID
    ) -> list[ChronosTimelineEvent]: ...

    @abstractmethod
    async def find_existing(
        self, accident_event_id: UUID, event_type: ChronosTimelineEventType, raw_value: str | None
    ) -> ChronosTimelineEvent | None: ...


class ChronosEventLinkRepository(ABC):
    @abstractmethod
    async def add(self, link: ChronosEventLink) -> None: ...

    @abstractmethod
    async def upsert_link(self, link: ChronosEventLink) -> tuple[ChronosEventLink, bool]:
        """Insert or return existing link."""
        ...

    @abstractmethod
    async def list_for_accident_event(self, accident_event_id: UUID) -> list[ChronosEventLink]: ...


class ChronosSequenceReviewRepository(ABC):
    @abstractmethod
    async def add(self, review: ChronosSequenceReview) -> None: ...

    @abstractmethod
    async def list_pending(
        self, limit: int = 50, offset: int = 0
    ) -> list[ChronosSequenceReview]: ...

    @abstractmethod
    async def mark_confirmed(
        self, review_id: UUID, resolved_by: UUID, note: str | None = None
    ) -> None: ...

    @abstractmethod
    async def mark_rejected(
        self, review_id: UUID, resolved_by: UUID, note: str | None = None
    ) -> None: ...


# ── Hermes Repository Interfaces ─────────────────────────────────────────────


class HermesSourceRepository(ABC):
    @abstractmethod
    async def get(self, id: UUID) -> HermesSource | None: ...

    @abstractmethod
    async def add(self, source: HermesSource) -> None: ...

    @abstractmethod
    async def find_by_name(self, name: str) -> HermesSource | None: ...

    @abstractmethod
    async def add_or_get_by_name(self, source: HermesSource) -> tuple[HermesSource, bool]:
        """Atomically insert or return an existing source matched by ``lower(name)``."""
        ...

    @abstractmethod
    async def list_active(self, limit: int = 100, offset: int = 0) -> list[HermesSource]: ...


class HermesCrawlTargetRepository(ABC):
    @abstractmethod
    async def get(self, id: UUID) -> HermesCrawlTarget | None: ...

    @abstractmethod
    async def add(self, target: HermesCrawlTarget) -> None: ...

    @abstractmethod
    async def save(self, target: HermesCrawlTarget) -> None: ...

    @abstractmethod
    async def find_by_normalized_url(self, normalized_url: str) -> HermesCrawlTarget | None: ...

    @abstractmethod
    async def add_or_get_by_normalized_url(
        self, target: HermesCrawlTarget
    ) -> tuple[HermesCrawlTarget, bool]:
        """Atomically insert or return an existing target matched by ``normalized_url``."""
        ...

    @abstractmethod
    async def list(
        self,
        status: HermesTargetStatus | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[HermesCrawlTarget]: ...


class HermesFetchJobRepository(ABC):
    @abstractmethod
    async def get(self, id: UUID) -> HermesFetchJob | None: ...

    @abstractmethod
    async def add(self, job: HermesFetchJob) -> None: ...

    @abstractmethod
    async def save(self, job: HermesFetchJob) -> None: ...

    @abstractmethod
    async def find_active_for_target(self, target_id: UUID) -> HermesFetchJob | None:
        """Return the QUEUED or RUNNING job for this target, or None."""
        ...

    @abstractmethod
    async def add_or_get_active_for_target(
        self, job: HermesFetchJob
    ) -> tuple[HermesFetchJob, bool]:
        """Atomically insert a QUEUED job or return the existing QUEUED/RUNNING job.

        Uses ``INSERT … ON CONFLICT DO NOTHING`` against the partial unique index
        ``uq_hermes_fetch_jobs_one_active_per_target`` (which enforces at most
        one QUEUED or RUNNING job per target) so two concurrent enqueue calls
        produce at most one row.  Returns ``(job, True)`` when a new row was
        inserted, ``(job, False)`` when an existing active job was found.
        """
        ...

    @abstractmethod
    async def claim_running(
        self,
        job_id: UUID,
        *,
        worker_id: str,
        lease_expires_at: datetime,
    ) -> HermesFetchJob | None:
        """Atomically transition a due QUEUED job to RUNNING with a lease."""
        ...

    @abstractmethod
    async def claim_next_running(
        self,
        *,
        worker_id: str,
        lease_expires_at: datetime,
    ) -> HermesFetchJob | None:
        """Atomically claim the next due QUEUED job and transition it to RUNNING."""
        ...

    @abstractmethod
    async def lock_claim_for_finalization(
        self,
        job_id: UUID,
        *,
        worker_id: str,
        attempt_count: int,
        now: datetime,
    ) -> HermesFetchJob | None:
        """Lock and return the RUNNING job only if the caller still owns its live lease."""
        ...

    @abstractmethod
    async def recover_stale_running(
        self,
        *,
        now: datetime,
        limit: int = 100,
    ) -> list[HermesRecoveryOutcome]:
        """Recover expired RUNNING jobs back to QUEUED or FAILED.

        Returns one :class:`HermesRecoveryOutcome` per recovered job so
        callers can decide what audit events to emit.  Terminally FAILED
        recoveries (attempts exhausted) should appear in the target's
        change stream as ``FETCH_FAILED`` source-change rows; requeued
        recoveries do not need a source-change because the next attempt
        will produce one if it also fails.
        """
        ...

    @abstractmethod
    async def get_next_queued(self) -> HermesFetchJob | None: ...

    @abstractmethod
    async def list(
        self,
        status: HermesFetchJobStatus | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[HermesFetchJob]: ...


class HermesFetchedDocumentRepository(ABC):
    @abstractmethod
    async def get(self, id: UUID) -> HermesFetchedDocument | None: ...

    @abstractmethod
    async def add(self, document: HermesFetchedDocument) -> None: ...

    @abstractmethod
    async def find_by_target_and_hash(
        self, target_id: UUID, content_sha256: str
    ) -> HermesFetchedDocument | None: ...

    @abstractmethod
    async def get_latest_for_target(self, target_id: UUID) -> HermesFetchedDocument | None: ...

    @abstractmethod
    async def list_for_target(
        self, target_id: UUID, limit: int = 50, offset: int = 0
    ) -> list[HermesFetchedDocument]: ...


class HermesSourceChangeRepository(ABC):
    @abstractmethod
    async def get(self, id: UUID) -> HermesSourceChange | None: ...

    @abstractmethod
    async def add(self, change: HermesSourceChange) -> None: ...

    @abstractmethod
    async def list_for_target(
        self, target_id: UUID, limit: int = 50, offset: int = 0
    ) -> list[HermesSourceChange]: ...

    @abstractmethod
    async def list_recent(self, limit: int = 100, offset: int = 0) -> list[HermesSourceChange]: ...


# ── Argus Repository Interfaces ───────────────────────────────────────────────


class ArgusSignalRepository(ABC):
    @abstractmethod
    async def get(self, id: UUID) -> ArgusSignal | None: ...

    @abstractmethod
    async def add(self, signal: ArgusSignal) -> None: ...

    @abstractmethod
    async def save(self, signal: ArgusSignal) -> None: ...

    @abstractmethod
    async def find_by_dedupe_key(self, dedupe_key: str) -> ArgusSignal | None: ...

    @abstractmethod
    async def upsert_signal(self, signal: ArgusSignal) -> tuple[ArgusSignal, bool]:
        """Return (signal, created). If dedupe_key already exists, update last_detected_at."""
        ...

    @abstractmethod
    async def update_with_version_check(
        self, signal_id: UUID, expected_version: int, updates: dict[str, Any]
    ) -> ArgusSignal | None:
        """Atomically update a signal iff ``version == expected_version``.

        Returns the updated ``ArgusSignal`` (with ``version`` bumped by 1) on
        success, or ``None`` if the row no longer matches the expected
        version — i.e. another reviewer raced and won.  Callers should map
        ``None`` to an ``ArgusSignalModifiedError`` 409 response so the
        client can re-read state and decide whether to retry.
        """
        ...

    @abstractmethod
    async def list_page(
        self,
        *,
        status: ArgusSignalStatus | None = None,
        signal_type: ArgusSignalType | None = None,
        severity: ArgusSeverity | None = None,
        limit: int = 50,
        after_id: UUID | None = None,
    ) -> list[ArgusSignal]:
        """Keyset-paginated variant of :meth:`list`.

        Ordering is ``(last_detected_at DESC, id DESC)`` — the same key as
        the composite index ``ix_argus_signals_last_detected_id_desc``.  When
        ``after_id`` is provided, results strictly precede that signal in
        the ordering (i.e. older or same-timestamp + lower-id).

        Callers should fetch ``limit + 1`` rows, slice to ``limit``, and use
        the last item's ``id`` as the next cursor — same pattern as
        :meth:`PendingDuplicateReviewRepository.list_pending`.  Stale or
        invalid cursors are treated as absent (no error) so a deleted
        cursor row doesn't break a paginating client.

        NOTE: defined before :meth:`list` so its ``list[ArgusSignal]`` return
        annotation resolves to the builtin rather than the shadowing
        ``list`` method.  The interface module deliberately does not use
        ``from __future__ import annotations`` to stay consistent with the
        rest of the package, so annotation-evaluation order matters.
        """
        ...

    @abstractmethod
    async def list(
        self,
        status: ArgusSignalStatus | None = None,
        signal_type: ArgusSignalType | None = None,
        severity: ArgusSeverity | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[ArgusSignal]: ...


class ArgusSignalEvidenceRepository(ABC):
    @abstractmethod
    async def add(self, evidence: ArgusSignalEvidence) -> None: ...

    @abstractmethod
    async def upsert_evidence(
        self, evidence: ArgusSignalEvidence
    ) -> tuple[ArgusSignalEvidence, bool]:
        """Return (evidence, created). Unique on (signal_id, evidence_type, evidence_id)."""
        ...

    @abstractmethod
    async def list_for_signal(self, signal_id: UUID) -> list[ArgusSignalEvidence]: ...


class ArgusSignalReviewRepository(ABC):
    @abstractmethod
    async def add(self, review: ArgusSignalReview) -> None: ...

    @abstractmethod
    async def list_for_signal(self, signal_id: UUID) -> list[ArgusSignalReview]: ...


# ── Publication: public event pages (Phase 1) ────────────────────────────────


@dataclass(frozen=True)
class PublicEventPagePage:
    """One page of public event pages, with a stable keyset cursor.

    ``next_cursor`` is the last row's id when more rows may follow;
    callers pass it back as ``after_id`` on the next request.  When
    ``None``, the result set is exhausted.
    """

    items: list[PublicEventPage]
    next_cursor: UUID | None


class PublicEventPageRepository(ABC):
    """Persistence interface for editorial public-event-page metadata.

    Phase 1 introduced read paths over PUBLISHED rows.  Phase 9 adds
    editorial write paths:

    - :meth:`update` with optimistic concurrency on ``version``;
    - :meth:`list_editorial` for curator-side filtering across
      DRAFT/IN_REVIEW/APPROVED/PUBLISHED/ARCHIVED;
    - :meth:`add_revision` / :meth:`list_revisions` for the
      immutable audit trail.

    The repository never commits — the use case owns the transaction
    boundary via the unit-of-work pattern.
    """

    @abstractmethod
    async def add(self, page: PublicEventPage) -> None:
        """Insert a new page.

        Raises ``SlugAlreadyTakenError`` if the slug collides and
        ``PublicEventPageAlreadyExistsError`` if the event already has
        a page.  Both invariants are also enforced by unique indexes
        at the DB level — the repository surfaces them as typed
        domain errors rather than letting raw ``IntegrityError`` leak.
        """
        ...

    @abstractmethod
    async def update(self, page: PublicEventPage, *, expected_version: int) -> None:
        """Persist edits to an existing page.

        Performs an optimistic-concurrency check against the stored
        ``version``: if the row in the DB does not match
        ``expected_version``, raises
        :class:`PublicEventPageModifiedError` and leaves the row
        untouched.  The page argument's own ``version`` should already
        have been incremented by the caller; the implementation
        writes that value back.

        Raises :class:`PublicEventPageNotFoundError` when the id no
        longer exists.
        """
        ...

    @abstractmethod
    async def get_by_id(self, page_id: UUID) -> PublicEventPage | None: ...

    @abstractmethod
    async def get_by_slug(self, slug: str) -> PublicEventPage | None:
        """Return the page for ``slug`` regardless of status.

        The use-case layer is responsible for translating DRAFT into a
        404-style response and RETRACTED into a 410-style response.
        """
        ...

    @abstractmethod
    async def get_by_event_id(self, event_id: UUID) -> PublicEventPage | None:
        """Return the page for ``event_id`` regardless of status."""
        ...

    @abstractmethod
    async def list_published(
        self,
        *,
        limit: int,
        after_id: UUID | None = None,
    ) -> PublicEventPagePage:
        """Return a keyset-paginated page of PUBLISHED rows.

        Ordering is ``(last_published_at DESC, id DESC)`` — matching
        the partial index installed by migration 035.  Equal
        ``last_published_at`` values are broken by ``id`` so the cursor
        is unique under all conditions.
        """
        ...

    @abstractmethod
    async def list_editorial(
        self,
        *,
        statuses: frozenset[PublicationStatus] | None = None,
        limit: int,
        after_id: UUID | None = None,
    ) -> PublicEventPagePage:
        """Editorial-side listing across any status set.

        Ordered ``(updated_at DESC, id DESC)`` — backed by the index
        ``ix_public_event_pages_status_updated`` from migration 036.
        Passing ``statuses=None`` lists every non-terminal row (i.e.
        excludes RETRACTED, which has its own 410 path).
        """
        ...

    @abstractmethod
    async def add_revision(self, revision: PublicEventPageRevision) -> None:
        """Append an immutable audit row."""
        ...

    @abstractmethod
    async def list_revisions(self, page_id: UUID) -> list[PublicEventPageRevision]:
        """Return every revision for ``page_id`` in chronological order."""
        ...


# ── Search: index over PUBLISHED public event pages (Phase 2) ────────────────


class SearchRepository(ABC):
    """Persistence interface for the public-event search index.

    Three concerns separated:

    - :meth:`upsert` / :meth:`delete` — write paths driven by the
      publication lifecycle.  Called from the Phase 9 publish /
      archive / retract use cases.  Idempotent on ``page_id`` so a
      re-publish of the same page replaces the prior index row.
    - :meth:`search` — the public read path.  Takes a validated
      :class:`SearchQuery` and returns a keyset-paginated result set
      ordered by ``(rank DESC, page_id DESC)`` when a text query is
      provided, or by ``(last_published_at DESC, page_id DESC)`` when
      it isn't.
    - :meth:`rebuild_all_from` — admin reindex.  Takes an iterable of
      index entries and replaces the index atomically (within the
      caller's transaction).

    The interface is deliberately small.  Adding a new filter facet
    requires touching :class:`SearchQuery`, the Pydantic request
    schema, and the SQL ``WHERE`` clause — no changes to this
    protocol.
    """

    @abstractmethod
    async def upsert(self, entry: SearchIndexEntry) -> None:
        """Insert or replace an index row by ``page_id``.

        Builds the ``tsvector`` from the entry's text fields with
        weighted ``setweight`` calls.  Implementations must keep
        weighting consistent across upserts so ranking is stable
        across re-publishes.
        """
        ...

    @abstractmethod
    async def delete(self, page_id: UUID) -> None:
        """Remove the index row for ``page_id`` if it exists.

        No-op if the row does not exist — archive/retract may be
        called on a page that has never been published.
        """
        ...

    @abstractmethod
    async def search(self, query: SearchQuery) -> SearchResult:
        """Run a public search.

        Returns up to ``query.limit`` hits ordered by relevance.  The
        cursor in :class:`SearchResult` is opaque to the caller —
        pass ``next_cursor_rank`` and ``next_cursor_id`` back as
        ``query.after_rank`` and ``query.after_id`` on the next call.
        """
        ...

    @abstractmethod
    async def rebuild_all_from(self, entries: list[SearchIndexEntry]) -> int:
        """Replace the entire index with ``entries``.

        Returns the number of rows written.  Used by the admin
        reindex endpoint; not part of the hot path.
        """
        ...


# ── Tenancy: repositories (Phase 5) ─────────────────────────────────────────
#
# Every method on every tenant repository takes ``tenant_id`` as a
# REQUIRED parameter and includes it in the WHERE clause.  This is
# the second of the three isolation layers (auth gate, repo gate,
# use-case gate).  A router that forgets to pass ``tenant_id`` gets
# a TypeError, not silent leakage.
#
# A useful invariant for code review: searching this file for
# ``tenant_id`` should find it in every tenant-related method
# signature.  If it's missing, that's a bug.


class TenantRepository(ABC):
    """Tenant directory.  Lookups by id and slug only; tenants are
    not enumerated through this interface — admin tooling owns that."""

    @abstractmethod
    async def get(self, tenant_id: UUID) -> Tenant | None: ...

    @abstractmethod
    async def get_by_slug(self, slug: str) -> Tenant | None: ...

    @abstractmethod
    async def add(self, tenant: Tenant) -> None: ...


class TenantMembershipRepository(ABC):
    @abstractmethod
    async def add(self, membership: TenantMembership) -> None: ...

    @abstractmethod
    async def get_for_user_in_tenant(
        self, *, tenant_id: UUID, user_id: UUID
    ) -> TenantMembership | None:
        """The canonical "does this user belong to this tenant" lookup.

        Used by ``require_tenant_membership`` as the authoritative
        access check.  A tenant API key's columns are a fast cache;
        this is the source of truth.
        """
        ...


class TenantSourceRepository(ABC):
    @abstractmethod
    async def add(self, *, tenant_id: UUID, source: TenantSource) -> None:
        """Insert a tenant-private source.

        Raises :class:`TenantSourceAlreadyExistsError` on a
        (tenant_id, name) collision.  ``tenant_id`` is passed
        explicitly even though it is already on ``source`` so the
        repository surface mirrors all other tenant methods
        (defensive symmetry).
        """
        ...

    @abstractmethod
    async def list_for_tenant(self, *, tenant_id: UUID) -> list[TenantSource]: ...

    @abstractmethod
    async def get(self, *, tenant_id: UUID, source_id: UUID) -> TenantSource | None:
        """Return ``source_id`` if and only if it belongs to ``tenant_id``.

        The tenant_id filter is required, not advisory — looking up
        a source id without naming the tenant would defeat isolation.
        """
        ...


class TenantClaimRepository(ABC):
    @abstractmethod
    async def add(self, *, tenant_id: UUID, claim: TenantClaim) -> None: ...

    @abstractmethod
    async def add_many(self, *, tenant_id: UUID, claims: list[TenantClaim]) -> None:
        """Bulk-append a batch of claims (Phase 6).

        Provided as a distinct method because batch ingestion is the
        common case for FOQA: a single run can produce hundreds of
        exceedance claims and per-claim ``INSERT`` round-trips would
        be wasteful.  Implementations should be all-or-nothing within
        the caller's UoW; partial failure is the caller's concern.
        """
        ...

    @abstractmethod
    async def list_for_event(self, *, tenant_id: UUID, event_id: UUID) -> list[TenantClaim]: ...

    @abstractmethod
    async def list_for_event_by_kind(
        self,
        *,
        tenant_id: UUID,
        event_id: UUID,
        claim_kind: TenantClaimKind,
    ) -> list[TenantClaim]:
        """Filter ``list_for_event`` by ``claim_kind``.

        Phase 6 read path uses this to surface "all FOQA claims on
        this event" or "all OTHER tenant claims on this event" as
        separate sections in the tenant evidence view.
        """
        ...


class TenantIngestionRunRepository(ABC):
    @abstractmethod
    async def add(self, *, tenant_id: UUID, run: TenantIngestionRun) -> None: ...

    @abstractmethod
    async def get(self, *, tenant_id: UUID, run_id: UUID) -> TenantIngestionRun | None:
        """Cross-tenant-safe lookup: returns None if the run id
        doesn't belong to this tenant, even when the row exists.
        Same defence-in-depth pattern as
        :meth:`TenantSourceRepository.get`.
        """
        ...

    @abstractmethod
    async def update_status(
        self,
        *,
        tenant_id: UUID,
        run_id: UUID,
        status: TenantIngestionRunStatus,
        finished_at: datetime | None,
    ) -> None:
        """Transition the run to a terminal status.

        Implementations enforce a one-way door: a non-RUNNING status
        cannot be re-opened.  Phase 6's
        :class:`CompleteTenantIngestionRun` use case is the only
        caller.
        """
        ...


# ── Phase 6: safety reports + event associations ────────────────────────────


class TenantSafetyReportRepository(ABC):
    """Tenant-private ASAP-style narrative reports.

    Hard invariant enforced by router routing (not by this protocol
    on its own): no public surface ever calls into this repository.
    """

    @abstractmethod
    async def add(self, *, tenant_id: UUID, report: TenantSafetyReport) -> None: ...

    @abstractmethod
    async def get(self, *, tenant_id: UUID, report_id: UUID) -> TenantSafetyReport | None: ...

    @abstractmethod
    async def list_for_tenant(
        self, *, tenant_id: UUID, limit: int = 50
    ) -> list[TenantSafetyReport]:
        """Recent reports, ordered ``created_at DESC``.  Phase 6
        keeps this unpaginated (limit-only) — operational use is a
        small dashboard, not browsing."""
        ...


class TenantEventAssociationRepository(ABC):
    @abstractmethod
    async def add(self, *, tenant_id: UUID, association: TenantEventAssociation) -> None: ...

    @abstractmethod
    async def list_for_event(
        self, *, tenant_id: UUID, event_id: UUID
    ) -> list[TenantEventAssociation]: ...


@dataclass(frozen=True)
class TenantEventOverlayPage:
    """A page of (event_id, overlay) results, with the public projection
    pre-joined in the use case for response composition.

    The keyset cursor is just ``event_id`` because the listing is
    ordered by the overlay's ``updated_at DESC`` and broken by
    ``event_id DESC``.  The overlay row already gives us the order
    key; callers pass the last returned ``event_id`` back.
    """

    items: list[TenantEventOverlay]
    next_cursor: UUID | None


class TenantEventOverlayRepository(ABC):
    @abstractmethod
    async def get(self, *, tenant_id: UUID, event_id: UUID) -> TenantEventOverlay | None: ...

    @abstractmethod
    async def upsert(self, *, tenant_id: UUID, overlay: TenantEventOverlay) -> TenantEventOverlay:
        """Insert or replace by (tenant_id, event_id).

        Phase 5 keeps this as a simple last-write-wins.  A
        versioned editorial-style workflow (analogous to Phase 9
        public pages) is reserved for a later phase if tenants
        request it.
        """
        ...

    @abstractmethod
    async def list_for_tenant(
        self,
        *,
        tenant_id: UUID,
        limit: int,
        after_id: UUID | None = None,
    ) -> TenantEventOverlayPage:
        """Keyset-paginated list of this tenant's overlays.

        Ordered ``(updated_at DESC, event_id DESC)``.  Both halves of
        the cursor predicate match the index in migration 038.
        """
        ...


# ── Maps: spatial index over PUBLISHED public event pages (Phase 3) ─────────


class MapRepository(ABC):
    """Persistence interface for the public map-event index.

    Three concerns, mirroring :class:`SearchRepository`:

    - :meth:`upsert` / :meth:`delete` — lifecycle writes driven by
      Phase 9's publish / archive / retract use cases.  Idempotent
      on ``page_id``.
    - :meth:`search_bbox` / :meth:`cluster_bbox` — read paths used
      by the public map router.
    - :meth:`rebuild_all_from` — admin reindex.

    Implementations are responsible for keeping the spatial index
    coherent (in Postgres this is the ``ST_MakePoint(lng, lat)``
    geography column plus the GiST index).  The interface itself is
    backend-agnostic.
    """

    @abstractmethod
    async def upsert(self, entry: MapIndexEntry) -> None: ...

    @abstractmethod
    async def delete(self, page_id: UUID) -> None: ...

    @abstractmethod
    async def search_bbox(self, query: MapQuery) -> MapSearchResult: ...

    @abstractmethod
    async def cluster_bbox(self, query: MapQuery) -> MapClusterResult: ...

    @abstractmethod
    async def rebuild_all_from(self, entries: list[MapIndexEntry]) -> int: ...


# ── CMS: glossary, methodology, changelog (Phase 10) ────────────────────────
#
# Three repos with parallel shapes.  Each repo follows the same
# pattern as the Phase 9 public-event-page repo: get/add/update with
# expected_version for optimistic concurrency, plus a revision audit
# subresource and the kind-specific listing/lookup methods.
#
# We don't factor these into a single generic protocol because the
# kind-specific lookups (by term, by slug, by section+order, by
# effective_date) differ enough that a `Protocol[T]` would lose more
# clarity than it would save.


@dataclass(frozen=True)
class GlossaryTermPage:
    items: list[GlossaryTerm]
    next_cursor: UUID | None


class GlossaryTermRepository(ABC):
    @abstractmethod
    async def get(self, term_id: UUID) -> GlossaryTerm | None: ...

    @abstractmethod
    async def get_by_term(self, term: str) -> GlossaryTerm | None:
        """Canonical-key lookup used by the public surface and the
        cross-reference helper.  Returns the row regardless of
        status; visibility filtering is the use case's job."""
        ...

    @abstractmethod
    async def add(self, term: GlossaryTerm) -> None: ...

    @abstractmethod
    async def update(self, term: GlossaryTerm, *, expected_version: int) -> None:
        """Update with optimistic-concurrency check.

        Raises :class:`CmsContentModifiedError` if the stored
        version does not match ``expected_version``.
        """
        ...

    @abstractmethod
    async def list_published_terms(self) -> list[GlossaryTerm]:
        """All PUBLISHED terms, ordered by ``term`` ascending.

        The glossary is bounded — on the order of dozens to hundreds
        of entries — so a single full-table read is fine here.  No
        pagination.
        """
        ...

    @abstractmethod
    async def list_editorial(
        self,
        *,
        statuses: frozenset[PublicationStatus] | None = None,
        limit: int = 50,
        after_id: UUID | None = None,
    ) -> GlossaryTermPage: ...


class GlossaryTermRevisionRepository(ABC):
    @abstractmethod
    async def add(self, revision: GlossaryTermRevision) -> None: ...

    @abstractmethod
    async def list_for_term(self, term_id: UUID) -> list[GlossaryTermRevision]: ...


@dataclass(frozen=True)
class MethodologyPagePage:
    items: list[MethodologyPage]
    next_cursor: UUID | None


class MethodologyPageRepository(ABC):
    @abstractmethod
    async def get(self, page_id: UUID) -> MethodologyPage | None: ...

    @abstractmethod
    async def get_by_slug(self, slug: str) -> MethodologyPage | None: ...

    @abstractmethod
    async def add(self, page: MethodologyPage) -> None: ...

    @abstractmethod
    async def update(self, page: MethodologyPage, *, expected_version: int) -> None: ...

    @abstractmethod
    async def list_published_grouped_by_section(
        self,
    ) -> list[MethodologyPage]:
        """All PUBLISHED methodology pages, ordered by
        ``(section, section_order, title)`` so the public render
        groups correctly without a second sort.

        Same bounded-collection assumption as the glossary.
        """
        ...

    @abstractmethod
    async def list_editorial(
        self,
        *,
        statuses: frozenset[PublicationStatus] | None = None,
        limit: int = 50,
        after_id: UUID | None = None,
    ) -> MethodologyPagePage: ...


class MethodologyPageRevisionRepository(ABC):
    @abstractmethod
    async def add(self, revision: MethodologyPageRevision) -> None: ...

    @abstractmethod
    async def list_for_page(self, page_id: UUID) -> list[MethodologyPageRevision]: ...


@dataclass(frozen=True)
class ChangelogEntryPage:
    items: list[ChangelogEntry]
    next_cursor: UUID | None


class ChangelogEntryRepository(ABC):
    @abstractmethod
    async def get(self, entry_id: UUID) -> ChangelogEntry | None: ...

    @abstractmethod
    async def get_by_slug(self, slug: str) -> ChangelogEntry | None: ...

    @abstractmethod
    async def add(self, entry: ChangelogEntry) -> None: ...

    @abstractmethod
    async def update(self, entry: ChangelogEntry, *, expected_version: int) -> None: ...

    @abstractmethod
    async def list_published(
        self,
        *,
        limit: int = 50,
        after_id: UUID | None = None,
    ) -> ChangelogEntryPage:
        """Public-facing list, ordered by ``effective_date DESC,
        id DESC``.

        Unlike glossary/methodology this is unbounded, so it's
        keyset-paginated.  The cursor is just ``id``; the repo
        resolves it back to ``(effective_date, id)`` for the
        keyset predicate.
        """
        ...

    @abstractmethod
    async def list_editorial(
        self,
        *,
        statuses: frozenset[PublicationStatus] | None = None,
        limit: int = 50,
        after_id: UUID | None = None,
    ) -> ChangelogEntryPage: ...


class ChangelogEntryRevisionRepository(ABC):
    @abstractmethod
    async def add(self, revision: ChangelogEntryRevision) -> None: ...

    @abstractmethod
    async def list_for_entry(self, entry_id: UUID) -> list[ChangelogEntryRevision]: ...


# ── Phase 4: causality ──────────────────────────────────────────────────────


class HfacsCategoryRepository(ABC):
    @abstractmethod
    async def list_all(self) -> list[HfacsCategory]:
        """Full reference set, sorted by tier then code.

        The taxonomy is small (under 30 rows) and read-mostly.  No
        pagination."""
        ...

    @abstractmethod
    async def get(self, category_id: UUID) -> HfacsCategory | None: ...

    @abstractmethod
    async def get_by_code(self, code: str) -> HfacsCategory | None: ...


class HfacsSubcategoryRepository(ABC):
    @abstractmethod
    async def list_for_category(self, category_id: UUID) -> list[HfacsSubcategory]: ...

    @abstractmethod
    async def get(self, subcategory_id: UUID) -> HfacsSubcategory | None: ...


class EventHfacsAttributionRepository(ABC):
    @abstractmethod
    async def list_for_event(self, event_id: UUID) -> list[EventHfacsAttribution]:
        """All attributions for an event, sorted by tier then code
        via the joined category."""
        ...

    @abstractmethod
    async def get(self, attribution_id: UUID) -> EventHfacsAttribution | None: ...

    @abstractmethod
    async def find_natural(
        self,
        *,
        event_id: UUID,
        category_id: UUID,
        subcategory_id: UUID | None,
    ) -> EventHfacsAttribution | None:
        """Look up an attribution by its natural key
        ``(event, category, subcategory)``.  Mirrors the partial
        unique index in the migration."""
        ...

    @abstractmethod
    async def add(self, attribution: EventHfacsAttribution) -> None: ...

    @abstractmethod
    async def update(
        self,
        attribution: EventHfacsAttribution,
        *,
        expected_version: int,
    ) -> None:
        """Optimistic-concurrency update.  Raises
        :class:`HfacsAttributionConflictError` on stale version."""
        ...

    @abstractmethod
    async def delete(self, attribution_id: UUID) -> None: ...


class SheloFactorRepository(ABC):
    @abstractmethod
    async def list_for_event(self, event_id: UUID) -> list[SheloFactor]: ...

    @abstractmethod
    async def get(self, factor_id: UUID) -> SheloFactor | None: ...

    @abstractmethod
    async def add(self, factor: SheloFactor) -> None: ...

    @abstractmethod
    async def update(self, factor: SheloFactor, *, expected_version: int) -> None: ...

    @abstractmethod
    async def delete(self, factor_id: UUID) -> None: ...


class SheloFactorInteractionRepository(ABC):
    @abstractmethod
    async def list_for_event(self, event_id: UUID) -> list[SheloFactorInteraction]: ...

    @abstractmethod
    async def find_natural(
        self,
        *,
        event_id: UUID,
        source_factor_id: UUID,
        target_factor_id: UUID,
        interaction_kind: SheloInteractionKind,
    ) -> SheloFactorInteraction | None: ...

    @abstractmethod
    async def add(self, interaction: SheloFactorInteraction) -> None: ...

    @abstractmethod
    async def delete(self, interaction_id: UUID) -> None: ...


# ── Phase 7: NL search log + saved queries ──────────────────────────────────


class NlQueryLogRepository(ABC):
    """Append-only log of NL queries.

    Writes only — no read API in Phase 7.  Analytics queries are
    expected to run as direct SQL against ``nl_query_log`` by
    operators, not via the repository surface.
    """

    @abstractmethod
    async def add(self, entry: NlQueryLog) -> None: ...


class SavedNlQueryRepository(ABC):
    @abstractmethod
    async def add(self, saved: SavedNlQuery) -> None: ...

    @abstractmethod
    async def list_for_user(self, user_id: UUID, *, limit: int = 50) -> list[SavedNlQuery]: ...

    @abstractmethod
    async def get(self, saved_id: UUID) -> SavedNlQuery | None: ...

    @abstractmethod
    async def delete_for_user(self, *, saved_id: UUID, user_id: UUID) -> bool:
        """Delete only if the saved query belongs to ``user_id``.

        Returns ``True`` on a successful delete, ``False`` if the
        row didn't exist or belonged to another user.  Cross-user
        delete returns False rather than raising so the read-modify-
        delete UI flow stays simple — the auth gate at the router
        is the authoritative protection.
        """
        ...


# ── Phase 8: metering ──────────────────────────────────────────────────────


class UsageEventRepository(ABC):
    """Append-only log of metered actions."""

    @abstractmethod
    async def add(self, event: UsageEvent) -> None: ...

    @abstractmethod
    async def add_many(self, events: list[UsageEvent]) -> None:
        """Bulk-insert a batch of usage events in a single round trip.

        The metering service uses this for quantity>1 recordings (a
        1000-claim batch records 1000 events) so metering doesn't
        turn one bulk action into N database flushes.  An empty list
        is a no-op.
        """
        ...

    @abstractmethod
    async def count_in_range(
        self,
        *,
        tenant_id: UUID | None,
        metric_kind: MetricKind,
        start: datetime,
        end: datetime,
    ) -> int:
        """Inclusive ``start``, exclusive ``end``.  Used by the
        rollup computer to populate ``usage_daily_rollups``.

        ``tenant_id=None`` means system-wide events (NL search,
        etc.) — the SQL repo filters with ``IS NULL``, not the
        sentinel UUID.  The sentinel exists only in the rollup
        table where its column is non-nullable.
        """
        ...

    @abstractmethod
    async def distinct_tenants_in_range(self, *, start: datetime, end: datetime) -> list[UUID]:
        """Distinct non-null tenant ids with at least one event in
        the range.  The rollup computer uses this to enumerate which
        tenants need rollup rows without depending on a tenant
        directory enumeration (which the TenantRepository
        deliberately doesn't expose)."""
        ...


class UsageDailyRollupRepository(ABC):
    @abstractmethod
    async def upsert(self, rollup: UsageDailyRollup) -> None:
        """Idempotent UPSERT on ``(tenant_id, metric_kind, day)``.

        Re-running rollup computation for the same day replaces
        the existing count; the natural-key constraint guarantees
        single-row semantics.
        """
        ...

    @abstractmethod
    async def list_for_tenant(
        self,
        *,
        tenant_id: UUID,
        day_from: date,
        day_to: date,
    ) -> list[UsageDailyRollup]:
        """Inclusive date range.  Ordered by (day, metric_kind)
        so the wire response is stable across calls."""
        ...

    @abstractmethod
    async def summary_across_tenants(
        self,
        *,
        day_from: date,
        day_to: date,
    ) -> list[UsageSummaryRow]:
        """Admin endpoint: sum counts per (tenant, metric) over the
        date range.  Maps the sentinel UUID back to ``tenant_id=None``
        in the result rows so consumers don't have to know about it."""
        ...


class TenantCrossrefResultRepository(ABC):
    """Tenant-private Echo cross-reference results.

    Hard invariant: no public surface calls this repository.  All
    reads are routed through the tenant-prefix router.  RLS (migration
    046) provides the DB-level enforcement.
    """

    @abstractmethod
    async def add(self, *, tenant_id: UUID, result: TenantCrossrefResult) -> None: ...

    @abstractmethod
    async def get(self, *, tenant_id: UUID, result_id: UUID) -> TenantCrossrefResult | None: ...

    @abstractmethod
    async def mark_complete(
        self,
        *,
        tenant_id: UUID,
        result_id: UUID,
        matches_json: list[dict[str, Any]],
        matcher_config_json: dict[str, Any],
        match_count: int,
        completed_at: datetime,
    ) -> None: ...

    @abstractmethod
    async def mark_failed(
        self,
        *,
        tenant_id: UUID,
        result_id: UUID,
        error_detail: str,
        completed_at: datetime,
    ) -> None: ...

    @abstractmethod
    async def list_for_report(
        self, *, tenant_id: UUID, safety_report_id: UUID, limit: int = 10
    ) -> list[TenantCrossrefResult]: ...
