"""Shared in-memory store dataclasses and utility helpers for all fake repositories."""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import date, datetime
from uuid import UUID

from atlas.domain.causality.entities import (
    EventHfacsAttribution,
    HfacsCategory,
    HfacsSubcategory,
    SheloFactor,
    SheloFactorInteraction,
)
from atlas.domain.cms.entities import (
    ChangelogEntry,
    ChangelogEntryRevision,
    GlossaryTerm,
    GlossaryTermRevision,
    MethodologyPage,
    MethodologyPageRevision,
)
from atlas.domain.constants import MAX_REGISTRATION_ALIASES
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
from atlas.domain.maps.entities import (
    MapIndexEntry,
)
from atlas.domain.metering.entities import (
    MetricKind,
    UsageDailyRollup,
    UsageEvent,
)
from atlas.domain.nl_search.entities import NlQueryLog, SavedNlQuery
from atlas.domain.publication.entities import (
    PublicEventPage,
    PublicEventPageRevision,
)
from atlas.domain.search.entities import (
    SearchIndexEntry,
)
from atlas.domain.tenancy.entities import (
    Tenant,
    TenantClaim,
    TenantCrossrefResult,
    TenantEventAssociation,
    TenantEventOverlay,
    TenantIngestionRun,
    TenantMembership,
    TenantSafetyReport,
    TenantSource,
)


def _normalise_registration_lookup(value: str) -> str:
    return re.sub(r"[-/\s]", "", str(value).lower().strip())


def _cap_registration_norms(values: list[str]) -> list[str]:
    seen: set[str] = set()
    capped_reversed: list[str] = []
    for item in reversed(values):
        if item in seen:
            continue
        seen.add(item)
        capped_reversed.append(item)
        if len(capped_reversed) >= MAX_REGISTRATION_ALIASES:
            break
    return list(reversed(capped_reversed))


def _slice_after_id(items: list, after_id: UUID | None, limit: int | None) -> list:
    if after_id is not None:
        try:
            index = next(i for i, item in enumerate(items) if item.id == after_id)
        except StopIteration:
            items = []
        else:
            items = items[index + 1 :]
    if limit is not None:
        items = items[:limit]
    return items


class _HermesStore:
    def __init__(self) -> None:
        self.sources: dict[UUID, HermesSource] = {}
        self.targets: dict[UUID, HermesCrawlTarget] = {}
        self.jobs: list[HermesFetchJob] = []
        self.documents: list[HermesFetchedDocument] = []
        self.changes: list[HermesSourceChange] = []


class _ChronosStore:
    def __init__(self) -> None:
        self.timeline_events: list[ChronosTimelineEvent] = []
        self.event_links: list[ChronosEventLink] = []
        self.sequence_reviews: list[ChronosSequenceReview] = []


class _OrionStore:
    def __init__(self) -> None:
        self.entities: dict[UUID, OrionEntity] = {}
        self.identifiers: list[OrionEntityIdentifier] = []
        self.relationships: list[OrionRelationship] = []
        self.claim_links: list[OrionEntityClaimLink] = []
        self.reviews: list[OrionEntityReview] = []


class _ArgusStore:
    def __init__(self) -> None:
        self.signals: dict[UUID, ArgusSignal] = {}
        self.evidence: list[ArgusSignalEvidence] = []
        self.reviews: list[ArgusSignalReview] = []


class _PublicationStore:
    def __init__(self) -> None:
        self.pages: dict[UUID, PublicEventPage] = {}
        self.revisions: list[PublicEventPageRevision] = []


class _SearchStore:
    def __init__(self) -> None:
        self.entries: dict[UUID, SearchIndexEntry] = {}


class _MapStore:
    def __init__(self) -> None:
        self.entries: dict[UUID, MapIndexEntry] = {}


class _CmsStore:
    """In-memory store for the three Phase 10 content kinds.

    Each kind is two dicts: the current row keyed by id, and the
    revision audit log as a list.  Uniqueness invariants (term,
    slug) are enforced by the fake repos so they match the SQL
    repo's behaviour.
    """

    def __init__(self) -> None:
        self.glossary_terms: dict[UUID, GlossaryTerm] = {}
        self.glossary_revisions: list[GlossaryTermRevision] = []
        self.methodology_pages: dict[UUID, MethodologyPage] = {}
        self.methodology_revisions: list[MethodologyPageRevision] = []
        self.changelog_entries: dict[UUID, ChangelogEntry] = {}
        self.changelog_revisions: list[ChangelogEntryRevision] = []


class _CausalityStore:
    """In-memory store for Phase 4 HFACS + SHELO.

    The HFACS taxonomy is seeded once per UoW (matching the
    migration's data-migration semantics).  Per-event tables are
    populated by use-case tests as they run.
    """

    def __init__(self) -> None:
        self.hfacs_categories: dict[UUID, HfacsCategory] = {}
        self.hfacs_subcategories: dict[UUID, HfacsSubcategory] = {}
        self.event_hfacs_attributions: dict[UUID, EventHfacsAttribution] = {}
        self.shelo_factors: dict[UUID, SheloFactor] = {}
        self.shelo_factor_interactions: dict[UUID, SheloFactorInteraction] = {}


class _TenancyStore:
    def __init__(self) -> None:
        self.tenants: dict[UUID, Tenant] = {}
        self.memberships: list[TenantMembership] = []
        self.sources: dict[UUID, TenantSource] = {}
        self.claims: dict[UUID, TenantClaim] = {}
        self.ingestion_runs: dict[UUID, TenantIngestionRun] = {}
        # Keyed by overlay.id; uniqueness on (tenant_id, event_id) is
        # enforced by the upsert path in the fake repo.
        self.overlays: dict[UUID, TenantEventOverlay] = {}
        # Phase 6 additions:
        self.safety_reports: dict[UUID, TenantSafetyReport] = {}
        self.event_associations: dict[UUID, TenantEventAssociation] = {}
        # Echo (Phase 7+):
        self.crossref_results: dict[UUID, TenantCrossrefResult] = {}


class _Store:
    """Shared, mutable backing store across repository instances within one UoW."""

    def __init__(self) -> None:
        self.sources: dict[UUID, Source] = {}
        self.snapshots: dict[UUID, RawSnapshot] = {}
        self.snapshots_by_run: dict[tuple[UUID, UUID], RawSnapshot] = {}
        self.ingestion_runs: dict[UUID, IngestionRun] = {}
        self.events: dict[UUID, AccidentEvent] = {}
        self.claims: dict[UUID, Claim] = {}
        self.claim_history: list[ClaimHistory] = []
        self.conflicts: dict[UUID, ClaimConflict] = {}
        self.conflict_claim_links: dict[UUID, list[UUID]] = defaultdict(list)
        self.conflict_activity: list[ConflictActivityLogEntry] = []
        self.projections: dict[UUID, ProjectedAccidentRecord] = {}
        self.projection_history: list[AccidentProjectionHistory] = []
        self.outbox: list[OutboxEvent] = []
        self.worker_heartbeats: dict[str, dict[str, datetime | None]] = {}
        self.archive_manifests: list[ArchiveManifest] = []
        self.duplicate_reviews: dict[UUID, PendingDuplicateReview] = {}
        self.identity_index: dict[UUID, EventIdentityIndex] = {}
        self.orion = _OrionStore()
        self.hermes = _HermesStore()
        self.chronos = _ChronosStore()
        self.argus = _ArgusStore()
        self.publication = _PublicationStore()
        self.search = _SearchStore()
        self.maps = _MapStore()
        self.cms = _CmsStore()
        self.causality = _CausalityStore()
        self.nl_search = _NlSearchStore()
        self.metering = _MeteringStore()
        self.tenancy = _TenancyStore()


class _NlSearchStore:
    def __init__(self) -> None:
        self.query_log: list[NlQueryLog] = []
        self.saved_queries: dict[UUID, SavedNlQuery] = {}


class _MeteringStore:
    def __init__(self) -> None:
        self.events: list[UsageEvent] = []
        # Keyed by (tenant_id, metric_kind, day) so the natural-key
        # uniqueness matches the SQL UPSERT semantics.
        self.rollups: dict[tuple[UUID, MetricKind, date], UsageDailyRollup] = {}
