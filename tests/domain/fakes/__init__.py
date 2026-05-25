"""In-memory fake repository layer for use-case-level tests.

This package splits the original monolithic ``_fake_uow.py`` into one file
per domain area.  The public surface is unchanged: import
``InMemoryUnitOfWork`` and ``make_settings`` from here (or from the
backward-compatible ``tests.domain._fake_uow`` shim).

Files
-----
_store.py       Shared in-memory store dataclasses.
core.py         Source, ingestion, events, claims, conflicts, projection.
orion.py        Orion entity extraction.
chronos.py      Chronos timeline reconstruction.
hermes.py       Hermes web crawler.
argus.py        Argus safety-signal detection.
publication.py  Public event pages.
search.py       Search index.
maps.py         Geospatial map index.
cms.py          Glossary, methodology pages, changelog.
tenancy.py      Tenant-scoped repositories.
causality.py    HFACS / SHELO causality.
nl_search.py    Natural-language search.
metering.py     Usage events and daily rollups.
"""

from __future__ import annotations

from uuid import uuid4

from atlas.application.unit_of_work import UnitOfWork
from tests.domain.fakes._store import _Store
from tests.domain.fakes.argus import (
    FakeArgusSignalEvidenceRepository,
    FakeArgusSignalRepository,
    FakeArgusSignalReviewRepository,
)
from tests.domain.fakes.causality import (
    FakeEventHfacsAttributionRepository,
    FakeHfacsCategoryRepository,
    FakeHfacsSubcategoryRepository,
    FakeSheloFactorInteractionRepository,
    FakeSheloFactorRepository,
)
from tests.domain.fakes.chronos import (
    FakeChronosEventLinkRepository,
    FakeChronosSequenceReviewRepository,
    FakeChronosTimelineEventRepository,
)
from tests.domain.fakes.cms import (
    FakeChangelogEntryRepository,
    FakeChangelogEntryRevisionRepository,
    FakeGlossaryTermRepository,
    FakeGlossaryTermRevisionRepository,
    FakeMethodologyPageRepository,
    FakeMethodologyPageRevisionRepository,
)
from tests.domain.fakes.core import (
    FakeAccidentEventRepository,
    FakeArchiveManifestRepository,
    FakeClaimHistoryRepository,
    FakeClaimRepository,
    FakeConflictActivityLogRepository,
    FakeConflictRepository,
    FakeEventIdentityIndexRepository,
    FakeIngestionRunRepository,
    FakeOutboxRepository,
    FakePendingDuplicateReviewRepository,
    FakeProjectionHistoryRepository,
    FakeProjectionRepository,
    FakeRawSnapshotRepository,
    FakeSourceRepository,
)
from tests.domain.fakes.hermes import (
    FakeHermesCrawlTargetRepository,
    FakeHermesFetchedDocumentRepository,
    FakeHermesFetchJobRepository,
    FakeHermesSourceChangeRepository,
    FakeHermesSourceRepository,
)
from tests.domain.fakes.maps import FakeMapRepository
from tests.domain.fakes.metering import (
    FakeUsageDailyRollupRepository,
    FakeUsageEventRepository,
)
from tests.domain.fakes.nl_search import (
    FakeNlQueryLogRepository,
    FakeSavedNlQueryRepository,
)
from tests.domain.fakes.orion import (
    FakeOrionEntityClaimLinkRepository,
    FakeOrionEntityRepository,
    FakeOrionEntityReviewRepository,
    FakeOrionIdentifierRepository,
    FakeOrionRelationshipRepository,
)
from tests.domain.fakes.publication import FakePublicEventPageRepository
from tests.domain.fakes.search import FakeSearchRepository
from tests.domain.fakes.tenancy import (
    FakeTenantClaimRepository,
    FakeTenantCrossrefResultRepository,
    FakeTenantEventAssociationRepository,
    FakeTenantEventOverlayRepository,
    FakeTenantIngestionRunRepository,
    FakeTenantMembershipRepository,
    FakeTenantRepository,
    FakeTenantSafetyReportRepository,
    FakeTenantSourceRepository,
)


class InMemoryUnitOfWork(UnitOfWork):
    """Thin UoW backed by an in-memory store. Useful for use-case tests."""

    def __init__(self) -> None:
        self._store = _Store()
        self.sources = FakeSourceRepository(self._store)
        self.snapshots = FakeRawSnapshotRepository(self._store)
        self.ingestion_runs = FakeIngestionRunRepository(self._store)
        self.events = FakeAccidentEventRepository(self._store)
        self.claims = FakeClaimRepository(self._store)
        self.claim_history = FakeClaimHistoryRepository(self._store)
        self.conflicts = FakeConflictRepository(self._store)
        self.conflict_activity = FakeConflictActivityLogRepository(self._store)
        self.projections = FakeProjectionRepository(self._store)
        self.projection_history = FakeProjectionHistoryRepository(self._store)
        self.outbox = FakeOutboxRepository(self._store)
        self.archive_manifests = FakeArchiveManifestRepository(self._store)
        self.duplicate_reviews = FakePendingDuplicateReviewRepository(self._store)
        self.identity_index = FakeEventIdentityIndexRepository(self._store)
        self.orion_entities = FakeOrionEntityRepository(self._store.orion)
        self.orion_identifiers = FakeOrionIdentifierRepository(self._store.orion)
        self.orion_relationships = FakeOrionRelationshipRepository(self._store.orion)
        self.orion_claim_links = FakeOrionEntityClaimLinkRepository(self._store.orion)
        self.orion_reviews = FakeOrionEntityReviewRepository(self._store.orion)
        self.chronos_timeline_events = FakeChronosTimelineEventRepository(self._store.chronos)
        self.chronos_event_links = FakeChronosEventLinkRepository(self._store.chronos)
        self.chronos_sequence_reviews = FakeChronosSequenceReviewRepository(self._store.chronos)
        self.hermes_sources = FakeHermesSourceRepository(self._store.hermes)
        self.hermes_crawl_targets = FakeHermesCrawlTargetRepository(self._store.hermes)
        self.hermes_fetch_jobs = FakeHermesFetchJobRepository(self._store.hermes)
        self.hermes_fetched_documents = FakeHermesFetchedDocumentRepository(self._store.hermes)
        self.hermes_source_changes = FakeHermesSourceChangeRepository(self._store.hermes)
        self.argus_signals = FakeArgusSignalRepository(self._store.argus)
        self.argus_signal_evidence = FakeArgusSignalEvidenceRepository(self._store.argus)
        self.argus_signal_reviews = FakeArgusSignalReviewRepository(self._store.argus)
        self.public_event_pages = FakePublicEventPageRepository(self._store.publication)
        self.search = FakeSearchRepository(self._store.search)
        self.maps = FakeMapRepository(self._store.maps)
        self.glossary_terms = FakeGlossaryTermRepository(self._store.cms)
        self.glossary_term_revisions = FakeGlossaryTermRevisionRepository(self._store.cms)
        self.methodology_pages = FakeMethodologyPageRepository(self._store.cms)
        self.methodology_page_revisions = FakeMethodologyPageRevisionRepository(self._store.cms)
        self.changelog_entries = FakeChangelogEntryRepository(self._store.cms)
        self.changelog_entry_revisions = FakeChangelogEntryRevisionRepository(self._store.cms)
        self.tenants = FakeTenantRepository(self._store.tenancy)
        self.tenant_memberships = FakeTenantMembershipRepository(self._store.tenancy)
        self.tenant_sources = FakeTenantSourceRepository(self._store.tenancy)
        self.tenant_claims = FakeTenantClaimRepository(self._store.tenancy)
        self.tenant_ingestion_runs = FakeTenantIngestionRunRepository(self._store.tenancy)
        self.tenant_event_overlays = FakeTenantEventOverlayRepository(self._store.tenancy)
        self.tenant_safety_reports = FakeTenantSafetyReportRepository(self._store.tenancy)
        self.tenant_crossref_results = FakeTenantCrossrefResultRepository(self._store.tenancy)
        self.tenant_event_associations = FakeTenantEventAssociationRepository(self._store.tenancy)
        self.hfacs_categories = FakeHfacsCategoryRepository(self._store.causality)
        self.hfacs_subcategories = FakeHfacsSubcategoryRepository(self._store.causality)
        self.event_hfacs_attributions = FakeEventHfacsAttributionRepository(
            self._store.causality, self._store.causality
        )
        self.shelo_factors = FakeSheloFactorRepository(self._store.causality)
        self.shelo_factor_interactions = FakeSheloFactorInteractionRepository(self._store.causality)
        self.nl_query_log = FakeNlQueryLogRepository(self._store.nl_search)
        self.saved_nl_queries = FakeSavedNlQueryRepository(self._store.nl_search)
        self.usage_events = FakeUsageEventRepository(self._store.metering)
        self.usage_daily_rollups = FakeUsageDailyRollupRepository(self._store.metering, self._store)
        self.commits = 0
        self.rollbacks = 0
        self.flushes = 0

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1

    async def flush(self) -> None:
        self.flushes += 1

    @property
    def store(self) -> _Store:
        """Test-only escape hatch for inspecting the store directly."""
        return self._store


def make_settings(curator_override_source_id=None):
    """Lightweight settings stub for use cases that consult only a few fields."""
    from types import SimpleNamespace

    return SimpleNamespace(
        max_claims_per_request=500,
        max_raw_payload_bytes=1_048_576,
        max_duplicate_reviews_per_ingestion=10,
        curator_override_source_id=curator_override_source_id or uuid4(),
        curator_override_source_name="CuratorOverride",
    )


__all__ = ["InMemoryUnitOfWork", "make_settings"]
