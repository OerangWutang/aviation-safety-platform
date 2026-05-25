from __future__ import annotations

from abc import ABC, abstractmethod
from contextlib import AbstractAsyncContextManager
from typing import Any

from atlas.domain.interfaces.repositories import (
    AccidentEventRepository,
    ArchiveManifestRepository,
    ArgusSignalEvidenceRepository,
    ArgusSignalRepository,
    ArgusSignalReviewRepository,
    ChangelogEntryRepository,
    ChangelogEntryRevisionRepository,
    ChronosEventLinkRepository,
    ChronosSequenceReviewRepository,
    ChronosTimelineEventRepository,
    ClaimHistoryRepository,
    ClaimRepository,
    ConflictActivityLogRepository,
    ConflictRepository,
    EventHfacsAttributionRepository,
    EventIdentityIndexRepository,
    GlossaryTermRepository,
    GlossaryTermRevisionRepository,
    HermesCrawlTargetRepository,
    HermesFetchedDocumentRepository,
    HermesFetchJobRepository,
    HermesSourceChangeRepository,
    HermesSourceRepository,
    HfacsCategoryRepository,
    HfacsSubcategoryRepository,
    IngestionRunRepository,
    MapRepository,
    MethodologyPageRepository,
    MethodologyPageRevisionRepository,
    NlQueryLogRepository,
    OrionEntityClaimLinkRepository,
    OrionEntityRepository,
    OrionEntityReviewRepository,
    OrionIdentifierRepository,
    OrionRelationshipRepository,
    OutboxRepository,
    PendingDuplicateReviewRepository,
    ProjectionHistoryRepository,
    ProjectionRepository,
    PublicEventPageRepository,
    RawSnapshotRepository,
    SavedNlQueryRepository,
    SearchRepository,
    SheloFactorInteractionRepository,
    SheloFactorRepository,
    SourceRepository,
    TenantClaimRepository,
    TenantCrossrefResultRepository,
    TenantEventAssociationRepository,
    TenantEventOverlayRepository,
    TenantIngestionRunRepository,
    TenantMembershipRepository,
    TenantRepository,
    TenantSafetyReportRepository,
    TenantSourceRepository,
    UsageDailyRollupRepository,
    UsageEventRepository,
)


class UnitOfWork(ABC):
    sources: SourceRepository
    snapshots: RawSnapshotRepository
    ingestion_runs: IngestionRunRepository
    events: AccidentEventRepository
    claims: ClaimRepository
    claim_history: ClaimHistoryRepository
    conflicts: ConflictRepository
    conflict_activity: ConflictActivityLogRepository
    projections: ProjectionRepository
    projection_history: ProjectionHistoryRepository
    outbox: OutboxRepository
    archive_manifests: ArchiveManifestRepository
    duplicate_reviews: PendingDuplicateReviewRepository
    identity_index: EventIdentityIndexRepository
    orion_entities: OrionEntityRepository
    orion_identifiers: OrionIdentifierRepository
    orion_relationships: OrionRelationshipRepository
    orion_claim_links: OrionEntityClaimLinkRepository
    orion_reviews: OrionEntityReviewRepository
    chronos_timeline_events: ChronosTimelineEventRepository
    chronos_event_links: ChronosEventLinkRepository
    chronos_sequence_reviews: ChronosSequenceReviewRepository
    hermes_sources: HermesSourceRepository
    hermes_crawl_targets: HermesCrawlTargetRepository
    hermes_fetch_jobs: HermesFetchJobRepository
    hermes_fetched_documents: HermesFetchedDocumentRepository
    hermes_source_changes: HermesSourceChangeRepository
    argus_signals: ArgusSignalRepository
    argus_signal_evidence: ArgusSignalEvidenceRepository
    argus_signal_reviews: ArgusSignalReviewRepository
    public_event_pages: PublicEventPageRepository
    search: SearchRepository
    maps: MapRepository
    glossary_terms: GlossaryTermRepository
    glossary_term_revisions: GlossaryTermRevisionRepository
    methodology_pages: MethodologyPageRepository
    methodology_page_revisions: MethodologyPageRevisionRepository
    changelog_entries: ChangelogEntryRepository
    changelog_entry_revisions: ChangelogEntryRevisionRepository
    tenants: TenantRepository
    tenant_memberships: TenantMembershipRepository
    tenant_sources: TenantSourceRepository
    tenant_claims: TenantClaimRepository
    tenant_ingestion_runs: TenantIngestionRunRepository
    tenant_event_overlays: TenantEventOverlayRepository
    tenant_safety_reports: TenantSafetyReportRepository
    tenant_event_associations: TenantEventAssociationRepository
    tenant_crossref_results: TenantCrossrefResultRepository
    hfacs_categories: HfacsCategoryRepository
    hfacs_subcategories: HfacsSubcategoryRepository
    event_hfacs_attributions: EventHfacsAttributionRepository
    shelo_factors: SheloFactorRepository
    shelo_factor_interactions: SheloFactorInteractionRepository
    nl_query_log: NlQueryLogRepository
    saved_nl_queries: SavedNlQueryRepository
    usage_events: UsageEventRepository
    usage_daily_rollups: UsageDailyRollupRepository

    @abstractmethod
    async def commit(self) -> None: ...

    @abstractmethod
    async def rollback(self) -> None: ...

    @abstractmethod
    async def flush(self) -> None:
        """Materialize pending writes to the database without ending
        the transaction.

        Needed where insert ordering matters and the ORM cannot
        infer it: a parent row must physically exist before a child
        row's foreign key can reference it.  Distinct from
        ``commit`` — the transaction stays open and a later
        ``rollback`` still undoes everything flushed.
        """
        ...

    def savepoint(self) -> AbstractAsyncContextManager[Any]:
        """Return an async context manager that wraps the current
        transaction in a database savepoint.

        On commit the savepoint is released; on exception it is rolled
        back, leaving the outer transaction intact.  Used by
        ``EchoCrossReference`` to ensure that a failed Argus signal
        upsert does not abort the entire cross-reference run.

        The default implementation raises ``NotImplementedError`` so
        in-memory fakes that do not need savepoint semantics (unit tests
        run single-threaded against an in-memory store) fail loudly
        rather than silently ignoring the boundary.
        """
        raise NotImplementedError("savepoint() not implemented for this UnitOfWork")
