from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, AsyncSessionTransaction

from atlas.application.unit_of_work import UnitOfWork
from atlas.infrastructure.db.repositories import (
    SqlAccidentEventRepository,
    SqlArchiveManifestRepository,
    SqlArgusSignalEvidenceRepository,
    SqlArgusSignalRepository,
    SqlArgusSignalReviewRepository,
    SqlChangelogEntryRepository,
    SqlChangelogEntryRevisionRepository,
    SqlChronosEventLinkRepository,
    SqlChronosSequenceReviewRepository,
    SqlChronosTimelineEventRepository,
    SqlClaimHistoryRepository,
    SqlClaimRepository,
    SqlConflictActivityLogRepository,
    SqlConflictRepository,
    SqlEventHfacsAttributionRepository,
    SqlEventIdentityIndexRepository,
    SqlGlossaryTermRepository,
    SqlGlossaryTermRevisionRepository,
    SqlHermesCrawlTargetRepository,
    SqlHermesFetchedDocumentRepository,
    SqlHermesFetchJobRepository,
    SqlHermesSourceChangeRepository,
    SqlHermesSourceRepository,
    SqlHfacsCategoryRepository,
    SqlHfacsSubcategoryRepository,
    SqlIngestionRunRepository,
    SqlMethodologyPageRepository,
    SqlMethodologyPageRevisionRepository,
    SqlNlQueryLogRepository,
    SqlOrionEntityClaimLinkRepository,
    SqlOrionEntityRepository,
    SqlOrionEntityReviewRepository,
    SqlOrionIdentifierRepository,
    SqlOrionRelationshipRepository,
    SqlOutboxRepository,
    SqlPendingDuplicateReviewRepository,
    SqlPostGisMapRepository,
    SqlPostgresFtsSearchRepository,
    SqlProjectionHistoryRepository,
    SqlProjectionRepository,
    SqlPublicEventPageRepository,
    SqlRawSnapshotRepository,
    SqlSavedNlQueryRepository,
    SqlSheloFactorInteractionRepository,
    SqlSheloFactorRepository,
    SqlSourceRepository,
    SqlTenantClaimRepository,
    SqlTenantCrossrefResultRepository,
    SqlTenantEventAssociationRepository,
    SqlTenantEventOverlayRepository,
    SqlTenantIngestionRunRepository,
    SqlTenantMembershipRepository,
    SqlTenantRepository,
    SqlTenantSafetyReportRepository,
    SqlTenantSourceRepository,
    SqlUsageDailyRollupRepository,
    SqlUsageEventRepository,
)
from atlas.infrastructure.db.session import (
    async_public_session_factory,
    async_session_factory,
    async_tenant_session_factory,
)


class SqlAlchemyUnitOfWork(UnitOfWork):
    def __init__(self, session: AsyncSession):
        self.session = session
        self.sources = SqlSourceRepository(session)
        self.snapshots = SqlRawSnapshotRepository(session)
        self.ingestion_runs = SqlIngestionRunRepository(session)
        self.events = SqlAccidentEventRepository(session)
        self.claims = SqlClaimRepository(session)
        self.claim_history = SqlClaimHistoryRepository(session)
        self.conflicts = SqlConflictRepository(session)
        self.conflict_activity = SqlConflictActivityLogRepository(session)
        self.projections = SqlProjectionRepository(session)
        self.projection_history = SqlProjectionHistoryRepository(session)
        self.outbox = SqlOutboxRepository(session)
        self.archive_manifests = SqlArchiveManifestRepository(session)
        self.duplicate_reviews = SqlPendingDuplicateReviewRepository(session)
        self.identity_index = SqlEventIdentityIndexRepository(session)
        self.orion_entities = SqlOrionEntityRepository(session)
        self.orion_identifiers = SqlOrionIdentifierRepository(session)
        self.orion_relationships = SqlOrionRelationshipRepository(session)
        self.orion_claim_links = SqlOrionEntityClaimLinkRepository(session)
        self.orion_reviews = SqlOrionEntityReviewRepository(session)
        self.chronos_timeline_events = SqlChronosTimelineEventRepository(session)
        self.chronos_event_links = SqlChronosEventLinkRepository(session)
        self.chronos_sequence_reviews = SqlChronosSequenceReviewRepository(session)
        self.hermes_sources = SqlHermesSourceRepository(session)
        self.hermes_crawl_targets = SqlHermesCrawlTargetRepository(session)
        self.hermes_fetch_jobs = SqlHermesFetchJobRepository(session)
        self.hermes_fetched_documents = SqlHermesFetchedDocumentRepository(session)
        self.hermes_source_changes = SqlHermesSourceChangeRepository(session)
        self.argus_signals = SqlArgusSignalRepository(session)
        self.argus_signal_evidence = SqlArgusSignalEvidenceRepository(session)
        self.argus_signal_reviews = SqlArgusSignalReviewRepository(session)
        self.public_event_pages = SqlPublicEventPageRepository(session)
        self.search = SqlPostgresFtsSearchRepository(session)
        self.maps = SqlPostGisMapRepository(session)
        self.glossary_terms = SqlGlossaryTermRepository(session)
        self.glossary_term_revisions = SqlGlossaryTermRevisionRepository(session)
        self.methodology_pages = SqlMethodologyPageRepository(session)
        self.methodology_page_revisions = SqlMethodologyPageRevisionRepository(session)
        self.changelog_entries = SqlChangelogEntryRepository(session)
        self.changelog_entry_revisions = SqlChangelogEntryRevisionRepository(session)
        self.tenants = SqlTenantRepository(session)
        self.tenant_memberships = SqlTenantMembershipRepository(session)
        self.tenant_sources = SqlTenantSourceRepository(session)
        self.tenant_claims = SqlTenantClaimRepository(session)
        self.tenant_ingestion_runs = SqlTenantIngestionRunRepository(session)
        self.tenant_event_overlays = SqlTenantEventOverlayRepository(session)
        self.tenant_safety_reports = SqlTenantSafetyReportRepository(session)
        self.tenant_crossref_results = SqlTenantCrossrefResultRepository(session)
        self.tenant_event_associations = SqlTenantEventAssociationRepository(session)
        self.hfacs_categories = SqlHfacsCategoryRepository(session)
        self.hfacs_subcategories = SqlHfacsSubcategoryRepository(session)
        self.event_hfacs_attributions = SqlEventHfacsAttributionRepository(session)
        self.shelo_factors = SqlSheloFactorRepository(session)
        self.shelo_factor_interactions = SqlSheloFactorInteractionRepository(session)
        self.nl_query_log = SqlNlQueryLogRepository(session)
        self.saved_nl_queries = SqlSavedNlQueryRepository(session)
        self.usage_events = SqlUsageEventRepository(session)
        self.usage_daily_rollups = SqlUsageDailyRollupRepository(session)

    async def commit(self) -> None:
        await self.session.commit()

    async def rollback(self) -> None:
        await self.session.rollback()

    async def flush(self) -> None:
        await self.session.flush()

    def savepoint(self) -> AsyncSessionTransaction:
        """Wrap the current transaction in a SAVEPOINT.

        Returns ``self.session.begin_nested()`` which is an async context
        manager.  Callers should use ``async with uow.savepoint()``.
        """
        return self.session.begin_nested()


class SqlAlchemyTenantUnitOfWork(SqlAlchemyUnitOfWork):
    """Tenant-scoped UoW that keeps transaction-local RLS context current.

    ``set_tenant_context`` intentionally uses a transaction-local PostgreSQL
    GUC so PgBouncer transaction-pooling deployments cannot leak one tenant's
    context into the next request.  The trade-off is that ``COMMIT`` and
    ``ROLLBACK`` clear the GUC.  Some application flows legitimately commit and
    then perform a read before the request ends; without re-establishing the
    context, PostgreSQL RLS would fail closed and hide the tenant's own rows.

    Re-applying the context after each successful boundary keeps the remainder
    of the request in the same tenant scope while preserving the transaction-
    local safety property.
    """

    def __init__(self, session: AsyncSession, tenant_id: uuid.UUID):
        super().__init__(session)
        self._tenant_id = tenant_id

    async def commit(self) -> None:
        await super().commit()
        await set_tenant_context(self.session, self._tenant_id)

    async def rollback(self) -> None:
        await super().rollback()
        await set_tenant_context(self.session, self._tenant_id)


@asynccontextmanager
async def create_uow() -> AsyncIterator[SqlAlchemyUnitOfWork]:
    async with async_session_factory() as session:
        uow = SqlAlchemyUnitOfWork(session)
        try:
            yield uow
        except Exception:
            await uow.rollback()
            raise


#: GUC read by the ``tenant_isolation`` row-level-security policy (migration 045).
TENANT_GUC = "app.current_tenant_id"


async def set_tenant_context(session: AsyncSession, tenant_id: uuid.UUID) -> None:
    """Bind the RLS tenant context for the current transaction.

    Uses ``set_config(key, value, is_local := true)`` so the setting is scoped
    to **this transaction only**.  That is mandatory under PgBouncer
    transaction-pooling (the engine uses ``NullPool``): a session-level ``SET``
    would leak the tenant context onto the next client multiplexed onto the same
    server connection.  ``set_config`` also parameterises the value safely,
    avoiding any string interpolation into SQL.

    Because the value is transaction-local, a tenant unit of work must be a
    single logical transaction - which is the request-scoped norm here.
    """
    await session.execute(
        text("SELECT set_config(:k, :v, true)"),
        {"k": TENANT_GUC, "v": str(tenant_id)},
    )


@asynccontextmanager
async def create_tenant_uow(tenant_id: uuid.UUID) -> AsyncIterator[SqlAlchemyUnitOfWork]:
    """A unit of work with row-level-security tenant context established.

    Every statement runs under ``app.current_tenant_id = <tenant_id>``, so the
    database itself filters tenant payload tables and rejects cross-tenant
    writes (migration 045).  This is defense-in-depth *behind* the application's
    own tenant checks - not a replacement for them.

    Do not use this for cross-tenant system work (cross-reference indexing,
    admin, projection rebuilds); those must connect as a ``BYPASSRLS`` role.
    """
    async with async_tenant_session_factory() as session:
        uow = SqlAlchemyTenantUnitOfWork(session, tenant_id)
        try:
            await set_tenant_context(session, tenant_id)
            yield uow
        except Exception:
            # Exiting the tenant UoW; roll back without re-establishing a fresh
            # tenant GUC transaction.
            await session.rollback()
            raise


@asynccontextmanager
async def create_public_uow() -> AsyncIterator[SqlAlchemyUnitOfWork]:
    """A unit of work connected to the public Atlas database.

    In the split-topology deployment (``PUBLIC_DATABASE_URL`` set), this
    connects to the separate public DB that holds canonical event projections.
    In the single-database deployment (default), it uses the same DB as
    ``create_uow()`` — the behaviour is identical; only the connection target
    differs.

    Use this for corpus reads in Echo cross-reference and any other operation
    that reads only public projection data.  Never use it for tenant payload
    reads — there is no RLS context set and the session may point at a
    different physical database than the tenant tables.
    """
    async with async_public_session_factory() as session:
        uow = SqlAlchemyUnitOfWork(session)
        try:
            yield uow
        except Exception:
            await uow.rollback()
            raise
