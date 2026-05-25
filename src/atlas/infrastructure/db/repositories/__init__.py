"""Public surface of the ``repositories`` package.

Re-exports every ``Sql*Repository`` previously defined in the
monolithic ``repositories.py`` module.  Existing imports of the
form ``from atlas.infrastructure.db.repositories import Sql...``
continue to work because of the explicit re-exports below.

Helper symbols (``_to_domain``, advisory lock IDs, etc.) used to be
module-level here.  They live in ``_helpers.py`` now and are also
re-exported for the small number of external call sites that read
them.
"""

from __future__ import annotations

from atlas.infrastructure.db.repositories._helpers import (
    _to_domain,
    _to_domain_opt,
)
from atlas.infrastructure.db.repositories.archive import SqlArchiveManifestRepository
from atlas.infrastructure.db.repositories.argus import (
    SqlArgusSignalEvidenceRepository,
    SqlArgusSignalRepository,
    SqlArgusSignalReviewRepository,
)
from atlas.infrastructure.db.repositories.causality import (
    SqlEventHfacsAttributionRepository,
    SqlHfacsCategoryRepository,
    SqlHfacsSubcategoryRepository,
    SqlSheloFactorInteractionRepository,
    SqlSheloFactorRepository,
)
from atlas.infrastructure.db.repositories.chronos import (
    SqlChronosEventLinkRepository,
    SqlChronosSequenceReviewRepository,
    SqlChronosTimelineEventRepository,
)
from atlas.infrastructure.db.repositories.claims import (
    SqlClaimHistoryRepository,
    SqlClaimRepository,
)
from atlas.infrastructure.db.repositories.cms import (
    SqlChangelogEntryRepository,
    SqlChangelogEntryRevisionRepository,
    SqlGlossaryTermRepository,
    SqlGlossaryTermRevisionRepository,
    SqlMethodologyPageRepository,
    SqlMethodologyPageRevisionRepository,
)
from atlas.infrastructure.db.repositories.conflicts import (
    SqlConflictActivityLogRepository,
    SqlConflictRepository,
)
from atlas.infrastructure.db.repositories.events import SqlAccidentEventRepository
from atlas.infrastructure.db.repositories.hermes import (
    SqlHermesCrawlTargetRepository,
    SqlHermesFetchedDocumentRepository,
    SqlHermesFetchJobRepository,
    SqlHermesSourceChangeRepository,
    SqlHermesSourceRepository,
)
from atlas.infrastructure.db.repositories.identity import SqlEventIdentityIndexRepository
from atlas.infrastructure.db.repositories.ingestion import SqlIngestionRunRepository
from atlas.infrastructure.db.repositories.maps import SqlPostGisMapRepository
from atlas.infrastructure.db.repositories.metering import (
    SqlUsageDailyRollupRepository,
    SqlUsageEventRepository,
)
from atlas.infrastructure.db.repositories.nl_search import (
    SqlNlQueryLogRepository,
    SqlSavedNlQueryRepository,
)
from atlas.infrastructure.db.repositories.orion import (
    SqlOrionEntityClaimLinkRepository,
    SqlOrionEntityRepository,
    SqlOrionEntityReviewRepository,
    SqlOrionIdentifierRepository,
    SqlOrionRelationshipRepository,
)
from atlas.infrastructure.db.repositories.outbox import SqlOutboxRepository
from atlas.infrastructure.db.repositories.projections import (
    SqlProjectionHistoryRepository,
    SqlProjectionRepository,
)
from atlas.infrastructure.db.repositories.publication import SqlPublicEventPageRepository
from atlas.infrastructure.db.repositories.reviews import SqlPendingDuplicateReviewRepository
from atlas.infrastructure.db.repositories.search import SqlPostgresFtsSearchRepository
from atlas.infrastructure.db.repositories.snapshots import SqlRawSnapshotRepository
from atlas.infrastructure.db.repositories.sources import SqlSourceRepository
from atlas.infrastructure.db.repositories.tenancy import (
    SqlTenantClaimRepository,
    SqlTenantCrossrefResultRepository,
    SqlTenantEventAssociationRepository,
    SqlTenantEventOverlayRepository,
    SqlTenantIngestionRunRepository,
    SqlTenantMembershipRepository,
    SqlTenantRepository,
    SqlTenantSafetyReportRepository,
    SqlTenantSourceRepository,
)

__all__ = [
    "SqlAccidentEventRepository",
    "SqlArchiveManifestRepository",
    "SqlArgusSignalEvidenceRepository",
    "SqlArgusSignalRepository",
    "SqlArgusSignalReviewRepository",
    "SqlChangelogEntryRepository",
    "SqlChangelogEntryRevisionRepository",
    "SqlChronosEventLinkRepository",
    "SqlChronosSequenceReviewRepository",
    "SqlChronosTimelineEventRepository",
    "SqlClaimHistoryRepository",
    "SqlClaimRepository",
    "SqlConflictActivityLogRepository",
    "SqlConflictRepository",
    "SqlEventHfacsAttributionRepository",
    "SqlEventIdentityIndexRepository",
    "SqlGlossaryTermRepository",
    "SqlGlossaryTermRevisionRepository",
    "SqlHermesCrawlTargetRepository",
    "SqlHermesFetchJobRepository",
    "SqlHermesFetchedDocumentRepository",
    "SqlHermesSourceChangeRepository",
    "SqlHermesSourceRepository",
    "SqlHfacsCategoryRepository",
    "SqlHfacsSubcategoryRepository",
    "SqlIngestionRunRepository",
    "SqlMethodologyPageRepository",
    "SqlMethodologyPageRevisionRepository",
    "SqlNlQueryLogRepository",
    "SqlOrionEntityClaimLinkRepository",
    "SqlOrionEntityRepository",
    "SqlOrionEntityReviewRepository",
    "SqlOrionIdentifierRepository",
    "SqlOrionRelationshipRepository",
    "SqlOutboxRepository",
    "SqlPendingDuplicateReviewRepository",
    "SqlPostGisMapRepository",
    "SqlPostgresFtsSearchRepository",
    "SqlProjectionHistoryRepository",
    "SqlProjectionRepository",
    "SqlPublicEventPageRepository",
    "SqlRawSnapshotRepository",
    "SqlSavedNlQueryRepository",
    "SqlSheloFactorInteractionRepository",
    "SqlSheloFactorRepository",
    "SqlSourceRepository",
    "SqlTenantClaimRepository",
    "SqlTenantCrossrefResultRepository",
    "SqlTenantEventAssociationRepository",
    "SqlTenantEventOverlayRepository",
    "SqlTenantIngestionRunRepository",
    "SqlTenantMembershipRepository",
    "SqlTenantRepository",
    "SqlTenantSafetyReportRepository",
    "SqlTenantSourceRepository",
    "SqlUsageDailyRollupRepository",
    "SqlUsageEventRepository",
    "_to_domain",
    "_to_domain_opt",
]
