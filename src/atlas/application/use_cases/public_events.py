"""Use cases for the public event encyclopedia (Phase 1).

Read-only paths that compose existing subsystems:

- ``ListPublicEvents``: keyset-paginated list of PUBLISHED pages.  The
  structured payload is enriched from the existing
  ``ProjectedAccidentRecord`` so the page row never has to duplicate
  projected truth.
- ``GetPublicEventPage``: slug -> page detail.  Translates DRAFT into
  a ``PublicEventPageNotPublishedError`` (404) and RETRACTED into a
  ``PublicEventPageRetractedError`` (410).
- ``GetPublicEventEvidence``: slug -> bounded public-facing evidence
  summary built from existing claims + sources.
- ``GetPublicEventTimeline``: slug -> Chronos timeline events for the
  canonical accident.
- ``GetPublicEventRelated``: slug -> related public events derived
  from Orion entity relationships (shared operator / aircraft type /
  airport).  Only PUBLISHED related pages are returned.

All canonicalization (following ``merged_into_event_id``) is handled
inside this module so the router layer never has to reason about it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from atlas.application.unit_of_work import UnitOfWork
from atlas.domain.entities import (
    AccidentEvent,
    ChronosTimelineEvent,
    Claim,
    OrionRelationship,
    ProjectedAccidentRecord,
    Source,
)
from atlas.domain.enums import ClaimType
from atlas.domain.publication.entities import PublicationStatus, PublicEventPage
from atlas.domain.publication.exceptions import (
    PublicEventPageNotFoundError,
    PublicEventPageNotPublishedError,
    PublicEventPageRetractedError,
)

logger = logging.getLogger(__name__)


# Limits chosen to keep public payloads predictable.  These are
# defensive ceilings, not hard contracts: callers can request smaller
# pages but never larger.
DEFAULT_PUBLIC_LIST_LIMIT = 25
MAX_PUBLIC_LIST_LIMIT = 100

# Public evidence/timeline/related responses are bounded explicitly
# rather than being open-ended; pagination of those is a Phase 9
# follow-up once the editorial workflow exists.
PUBLIC_EVIDENCE_CLAIM_LIMIT = 200
PUBLIC_EVIDENCE_SOURCE_LIMIT = 50
PUBLIC_TIMELINE_LIMIT = 200
PUBLIC_RELATED_LIMIT = 25
# Cap how many candidate events we fetch via Orion relationships
# before filtering down to PUBLISHED.  Avoids degenerate operator-
# wide fan-outs when an operator has thousands of events.
PUBLIC_RELATED_CANDIDATE_LIMIT = 200

# Claim types we consider "active evidence" in the public view.  We
# explicitly omit SUPERSEDED so retracted-by-curator values do not
# resurface; the source of truth on what active means is
# ``ClaimType.active_values()``.
_ACTIVE_CLAIM_TYPES = ClaimType.active_values()

# Orion relationship types that we consider as "related to this
# accident" for /related.  Limited to operator + aircraft type because
# those produce useful aviation-domain links; airport could be added
# later but is intentionally not in Phase 1 to keep the scope tight.
_RELATED_VIA_RELATIONSHIPS: frozenset[str] = frozenset({"OPERATED_BY", "AIRCRAFT_TYPE"})


# ── Internal helpers ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _PageWithCanonicalEvent:
    page: PublicEventPage
    canonical_event_id: UUID


async def _canonical_event_id_for(uow: UnitOfWork, event_id: UUID) -> UUID | None:
    """Walk ``merged_into_event_id`` to the surviving event id.

    Mirrors the cycle-safe walker in
    ``QueryAccidentPublicView._canonical_event_id`` rather than
    importing it: the public read path is allowed to swallow merge
    cycles (return None -> not found) but the provenance audit path
    raises.  Behavioural alignment is enforced by the public-event
    use-case tests, not by code reuse, because the failure modes must
    differ between these two callers.
    """
    seen: set[UUID] = set()
    current_id = event_id
    while True:
        event = await uow.events.get(current_id)
        if event is None:
            # The page references a missing event; treat as "no
            # canonical".  Construction guards already prevent this in
            # the happy path; this branch is here for resilience under
            # operator-initiated data cleanup.
            return None
        if not event.is_merged or event.merged_into_event_id is None:
            return event.id
        if event.id in seen:
            logger.error(
                "Merge cycle detected while canonicalising public page event_id=%s",
                event_id,
            )
            return None
        seen.add(event.id)
        current_id = event.merged_into_event_id


async def _load_page_and_check_visibility(uow: UnitOfWork, slug: str) -> _PageWithCanonicalEvent:
    """Common gate for all slug-keyed public routes.

    Raises:
        PublicEventPageNotFoundError: slug not in DB at all.
        PublicEventPageNotPublishedError: row exists but is DRAFT.
        PublicEventPageRetractedError: row exists but is RETRACTED.
    """
    page = await uow.public_event_pages.get_by_slug(slug)
    if page is None:
        raise PublicEventPageNotFoundError(f"Public event page {slug!r} not found")
    if page.status == PublicationStatus.DRAFT:
        # Surface a 404 rather than 403/410 so DRAFT existence is not
        # observable to anonymous callers.
        raise PublicEventPageNotPublishedError(f"Public event page {slug!r} is not published")
    if page.status == PublicationStatus.RETRACTED:
        raise PublicEventPageRetractedError(slug, page.retraction_note)
    # PUBLISHED — resolve the canonical event for downstream reads.
    canonical_id = await _canonical_event_id_for(uow, page.event_id)
    if canonical_id is None:
        # Page points at a missing or cyclic event; behave as not found
        # rather than expose a half-broken response.
        raise PublicEventPageNotFoundError(
            f"Public event page {slug!r} references an unresolvable event"
        )
    return _PageWithCanonicalEvent(page=page, canonical_event_id=canonical_id)


def _confidence_label(projection: ProjectedAccidentRecord | None) -> str:
    """Map ``completeness_score`` to a small ordinal label.

    A coarse public-facing band keeps the audit UI honest without
    leaking the raw score (which can be misread as "accuracy").
    """
    if projection is None:
        return "unknown"
    score = projection.completeness_score
    if score >= 0.85:
        return "high"
    if score >= 0.5:
        return "medium"
    if score > 0.0:
        return "low"
    return "unknown"


# ── List ─────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class PublicEventListItem:
    """Single row of the public event list response."""

    slug: str
    title: str
    short_summary: str | None
    event_date: str | None
    location: str | None
    operator: str | None
    aircraft_type: str | None
    fatalities_total: Any
    confidence: str
    has_unresolved_conflicts: bool
    last_published_at: Any  # datetime; kept Any so dataclass+pydantic stay happy


@dataclass(frozen=True)
class PublicEventListResult:
    items: list[PublicEventListItem]
    next_cursor: UUID | None
    limit: int


class ListPublicEvents:
    """Return a keyset-paginated page of PUBLISHED public events.

    Enrichment policy: for each page row, fetch the matching projection
    so the public list can carry the few high-value structured fields
    (operator, aircraft type, location, date, fatalities) without
    duplicating them into the page row itself.

    The N+1 read here is intentional and bounded: ``limit`` is capped
    at ``MAX_PUBLIC_LIST_LIMIT`` (100) and projection reads are PK
    lookups.  If profiling shows this on a hot path we can later add a
    repository batch method; for Phase 1 the simpler code is the right
    trade-off.
    """

    def __init__(self, uow: UnitOfWork):
        self._uow = uow

    async def execute(
        self,
        *,
        limit: int = DEFAULT_PUBLIC_LIST_LIMIT,
        after_id: UUID | None = None,
    ) -> PublicEventListResult:
        bounded_limit = max(1, min(limit, MAX_PUBLIC_LIST_LIMIT))
        page = await self._uow.public_event_pages.list_published(
            limit=bounded_limit, after_id=after_id
        )

        items: list[PublicEventListItem] = []
        for row in page.items:
            canonical_id = await _canonical_event_id_for(self._uow, row.event_id)
            projection = (
                await self._uow.projections.get(canonical_id) if canonical_id is not None else None
            )
            items.append(_summary_from(row, projection))

        return PublicEventListResult(items=items, next_cursor=page.next_cursor, limit=bounded_limit)


def _summary_from(
    row: PublicEventPage, projection: ProjectedAccidentRecord | None
) -> PublicEventListItem:
    fields = projection.fields if projection else {}
    return PublicEventListItem(
        slug=row.slug,
        title=row.title,
        short_summary=row.short_summary,
        event_date=_str_or_none(fields.get("event_date")),
        location=_str_or_none(fields.get("location")),
        operator=_str_or_none(fields.get("operator")),
        aircraft_type=_str_or_none(fields.get("aircraft_type")),
        fatalities_total=fields.get("fatalities_total"),
        confidence=_confidence_label(projection),
        has_unresolved_conflicts=bool(projection and projection.unresolved_conflict_fields),
        last_published_at=row.last_published_at,
    )


def _str_or_none(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    # DISPUTED sentinel renders to its marker string via __str__;
    # callers can treat that as opaque.
    return str(value)


# ── Detail ───────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class PublicEventDetail:
    """Detail response shape returned by :class:`GetPublicEventPage`.

    The structured ``fields`` dict mirrors the projection (with DISPUTED
    sentinels stringified).  ``editorial`` carries the page row's
    overlay fields and is explicitly separate so consumers cannot
    confuse evidence with editorial prose.
    """

    slug: str
    canonical_event_id: UUID
    title: str
    short_summary: str | None
    narrative_markdown: str | None
    fields: dict[str, Any]
    completeness_score: float
    confidence: str
    unresolved_conflict_fields: list[str]
    projection_version: int
    first_published_at: Any
    last_published_at: Any
    last_updated_at: Any


class GetPublicEventPage:
    def __init__(self, uow: UnitOfWork):
        self._uow = uow

    async def execute(self, slug: str) -> PublicEventDetail:
        loaded = await _load_page_and_check_visibility(self._uow, slug)
        projection = await self._uow.projections.get(loaded.canonical_event_id)
        if projection is None:
            # PUBLISHED page with no projection is a curator workflow
            # bug we want to surface, not hide.  The response would be
            # blank-shaped; raising 404 keeps the public surface clean
            # and the operator alerted via 4xx metrics.
            logger.error(
                "Published public event page %s has no projection for canonical_event_id=%s",
                slug,
                loaded.canonical_event_id,
            )
            raise PublicEventPageNotFoundError(
                f"Public event page {slug!r} has no projection available"
            )

        # ``projection.model_dump()`` runs the DISPUTED serializer
        # registered on the ProjectedAccidentRecord field, so the
        # exposed values are JSON-safe by construction.
        rendered = projection.model_dump()
        return PublicEventDetail(
            slug=loaded.page.slug,
            canonical_event_id=loaded.canonical_event_id,
            title=loaded.page.title,
            short_summary=loaded.page.short_summary,
            narrative_markdown=loaded.page.narrative_markdown,
            fields=rendered["fields"],
            completeness_score=projection.completeness_score,
            confidence=_confidence_label(projection),
            unresolved_conflict_fields=projection.unresolved_conflict_fields,
            projection_version=projection.projection_version,
            first_published_at=loaded.page.first_published_at,
            last_published_at=loaded.page.last_published_at,
            last_updated_at=projection.updated_at,
        )


# ── Evidence ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class PublicEvidenceClaim:
    """Public-facing claim summary.

    Internal-only fields (raw payload hashes, ingestion run ids, API
    key ids, source field mappings) are deliberately *absent* from
    this dataclass.  This is the whitelist-by-construction story
    described in the Phase 1 plan: the public Pydantic schema accepts
    only what this dataclass exposes, so adding a field later requires
    an explicit decision.
    """

    field_name: str
    field_value: Any
    claim_type: str
    source_name: str
    source_kind: str
    source_reliability_tier: int
    is_winning: bool
    is_superseded: bool
    created_at: Any


@dataclass(frozen=True)
class PublicEvidenceSource:
    name: str
    kind: str
    reliability_tier: int


@dataclass(frozen=True)
class PublicEvidenceResponse:
    slug: str
    canonical_event_id: UUID
    claims: list[PublicEvidenceClaim]
    sources: list[PublicEvidenceSource]
    claim_count: int
    truncated: bool


class GetPublicEventEvidence:
    """Surface a public-facing evidence summary for a slug.

    The "winning" flag uses the existing ``WinnerPolicy`` semantics:
    among active claims for a given field, the highest-ranked claim
    wins (MANUAL_OVERRIDE > CONFIRMED/RAW by reliability tier).  We
    determine winners by intersecting active claims with the existing
    projection rather than re-running the policy here — that keeps a
    single source of truth for winner selection and matches what the
    public read of the projection actually shows.
    """

    def __init__(self, uow: UnitOfWork):
        self._uow = uow

    async def execute(self, slug: str) -> PublicEvidenceResponse:
        loaded = await _load_page_and_check_visibility(self._uow, slug)

        active_claims = await self._uow.claims.find_active_by_event(loaded.canonical_event_id)
        # Defensive sort + bound: oldest active claims first, capped at
        # the public limit.  Truncation is reported in the response
        # rather than silently dropping rows.
        active_claims.sort(key=lambda c: (c.created_at, c.id))
        truncated = len(active_claims) > PUBLIC_EVIDENCE_CLAIM_LIMIT
        if truncated:
            active_claims = active_claims[:PUBLIC_EVIDENCE_CLAIM_LIMIT]

        # Resolve sources in a single batch fetch.  ``Source`` carries
        # ``field_mapping_json`` internally, so we explicitly strip
        # everything but name/kind/tier when projecting to the public
        # DTO.
        source_ids = {c.source_id for c in active_claims}
        sources = await self._uow.sources.get_by_ids(list(source_ids)) if source_ids else []
        source_by_id = {s.id: s for s in sources}

        projection = await self._uow.projections.get(loaded.canonical_event_id)
        winning_values = _winning_values_by_field(projection)

        public_claims: list[PublicEvidenceClaim] = []
        for claim in active_claims:
            source = source_by_id.get(claim.source_id)
            public_claims.append(_claim_to_public(claim, source, winning_values=winning_values))

        public_sources = sorted(
            (_source_to_public(s) for s in sources),
            key=lambda s: (s.reliability_tier, s.name),
        )[:PUBLIC_EVIDENCE_SOURCE_LIMIT]

        return PublicEvidenceResponse(
            slug=loaded.page.slug,
            canonical_event_id=loaded.canonical_event_id,
            claims=public_claims,
            sources=public_sources,
            claim_count=len(public_claims),
            truncated=truncated,
        )


def _winning_values_by_field(
    projection: ProjectedAccidentRecord | None,
) -> dict[str, Any]:
    """Return ``{field_name: projected_value}`` for non-disputed fields.

    The intersection of "active claim with this field value" and
    "projection has this concrete value" is our public proxy for
    "winning claim".  Disputed fields are excluded because no single
    claim wins; the public list still shows the unresolved-conflicts
    flag at the row level.
    """
    if projection is None:
        return {}
    disputed = set(projection.unresolved_conflict_fields)
    return {k: v for k, v in projection.fields.items() if k not in disputed}


def _claim_to_public(
    claim: Claim,
    source: Source | None,
    *,
    winning_values: dict[str, Any],
) -> PublicEvidenceClaim:
    is_winning = claim.field_name in winning_values and _values_equal(
        winning_values[claim.field_name], claim.field_value
    )
    return PublicEvidenceClaim(
        field_name=claim.field_name,
        field_value=claim.field_value,
        claim_type=claim.claim_type.value,
        source_name=source.name if source else "(unknown source)",
        source_kind=source.kind.value if source else "EXTERNAL",
        source_reliability_tier=source.reliability_tier if source else 99,
        is_winning=is_winning,
        is_superseded=claim.claim_type.value not in _ACTIVE_CLAIM_TYPES,
        created_at=claim.created_at,
    )


def _source_to_public(source: Source) -> PublicEvidenceSource:
    # field_mapping_json is intentionally NOT exposed.
    return PublicEvidenceSource(
        name=source.name,
        kind=source.kind.value,
        reliability_tier=source.reliability_tier,
    )


def _values_equal(a: Any, b: Any) -> bool:
    """Lightweight equality for projection ↔ claim winner detection.

    Projection values come from JSONB and may have been normalised
    differently from the claim's stored ``field_value``.  Equality
    falls back to ``str()`` on type mismatches so a winner detection
    is conservative rather than over-strict.
    """
    if a == b:
        return True
    return str(a) == str(b)


# ── Timeline ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class PublicTimelineEvent:
    event_type: str
    occurred_at: Any
    timestamp_precision: str
    sequence_index: int | None
    description: str | None


@dataclass(frozen=True)
class PublicTimelineResponse:
    slug: str
    canonical_event_id: UUID
    events: list[PublicTimelineEvent]


class GetPublicEventTimeline:
    """Return the Chronos timeline for the canonical accident."""

    def __init__(self, uow: UnitOfWork):
        self._uow = uow

    async def execute(self, slug: str) -> PublicTimelineResponse:
        loaded = await _load_page_and_check_visibility(self._uow, slug)
        timeline = await self._uow.chronos_timeline_events.list_for_accident_event(
            loaded.canonical_event_id
        )
        timeline.sort(key=lambda e: (_sort_key_for_timeline(e), e.id))
        bounded = timeline[:PUBLIC_TIMELINE_LIMIT]
        return PublicTimelineResponse(
            slug=loaded.page.slug,
            canonical_event_id=loaded.canonical_event_id,
            events=[_timeline_to_public(e) for e in bounded],
        )


def _sort_key_for_timeline(event: ChronosTimelineEvent) -> tuple[int, Any]:
    """Sort timeline events with NULL occurred_at last.

    Tuples compare element-by-element, so prefixing with 0/1 puts
    timestamped events ahead of un-timed ones without relying on
    None-comparison semantics that differ across Python versions.
    """
    if event.occurred_at is None:
        return (1, event.sequence_index if event.sequence_index is not None else 0)
    return (0, event.occurred_at)


def _timeline_to_public(event: ChronosTimelineEvent) -> PublicTimelineEvent:
    return PublicTimelineEvent(
        event_type=event.event_type.value,
        occurred_at=event.occurred_at,
        timestamp_precision=event.timestamp_precision.value,
        sequence_index=event.sequence_index,
        description=event.description,
    )


# ── Related ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class PublicRelatedEvent:
    slug: str
    title: str
    short_summary: str | None
    last_published_at: Any
    relation: str  # "OPERATED_BY" / "AIRCRAFT_TYPE" — public-safe enum value


@dataclass(frozen=True)
class PublicRelatedResponse:
    slug: str
    canonical_event_id: UUID
    items: list[PublicRelatedEvent]


class GetPublicEventRelated:
    """Find PUBLISHED public events related to this one via Orion.

    Algorithm:

    1. Walk the Orion relationships attached to the canonical event to
       collect (entity_id, relationship_type) pairs whose type is in
       :data:`_RELATED_VIA_RELATIONSHIPS`.
    2. For each related entity, fetch its other relationships
       (bounded), collect candidate accident_event_ids, drop the
       current canonical event itself.
    3. Resolve each candidate to a PUBLISHED public event page; drop
       anything that isn't PUBLISHED.
    4. Truncate to :data:`PUBLIC_RELATED_LIMIT`.

    Phase 1 returns a stable but unscored list.  Ranking by recency /
    aircraft model similarity is a deliberate follow-up in Phase 2/4.
    """

    def __init__(self, uow: UnitOfWork):
        self._uow = uow

    async def execute(self, slug: str) -> PublicRelatedResponse:
        loaded = await _load_page_and_check_visibility(self._uow, slug)
        primary_rels = await self._uow.orion_relationships.list_for_event(loaded.canonical_event_id)

        # First pass: collect (entity_id, relation_type) seeds for the
        # entities we want to follow outwards from.  We dedupe per
        # entity so a seed entity that appears under two relationship
        # types still produces only one outward walk.
        seeds: dict[UUID, str] = {}
        for rel in primary_rels:
            rel_type = rel.relationship_type.value
            if rel_type not in _RELATED_VIA_RELATIONSHIPS:
                continue
            seeds.setdefault(rel.object_entity_id, rel_type)

        # Second pass: walk each seed entity to find sibling events.
        # We cap total candidates aggressively to avoid an unbounded
        # fan-out when an operator has thousands of events.
        candidate_events: dict[UUID, str] = {}
        for entity_id, seed_relation in seeds.items():
            sibling_rels = await self._uow.orion_relationships.list_for_entity(entity_id)
            for sibling in sibling_rels:
                if sibling.accident_event_id == loaded.canonical_event_id:
                    continue
                # First seen relation wins — deterministic for tests.
                if sibling.accident_event_id not in candidate_events:
                    candidate_events[sibling.accident_event_id] = (
                        sibling.relationship_type.value
                        if sibling.relationship_type.value in _RELATED_VIA_RELATIONSHIPS
                        else seed_relation
                    )
                if len(candidate_events) >= PUBLIC_RELATED_CANDIDATE_LIMIT:
                    break
            if len(candidate_events) >= PUBLIC_RELATED_CANDIDATE_LIMIT:
                break

        # Third pass: resolve candidates to PUBLISHED public pages.
        related: list[PublicRelatedEvent] = []
        for candidate_event_id, relation in candidate_events.items():
            page = await self._uow.public_event_pages.get_by_event_id(candidate_event_id)
            if page is None or page.status != PublicationStatus.PUBLISHED:
                continue
            related.append(
                PublicRelatedEvent(
                    slug=page.slug,
                    title=page.title,
                    short_summary=page.short_summary,
                    last_published_at=page.last_published_at,
                    relation=relation,
                )
            )

        # Newest first — stable, useful default.
        related.sort(
            key=lambda r: (r.last_published_at or _SORT_MIN, r.slug),
            reverse=True,
        )
        return PublicRelatedResponse(
            slug=loaded.page.slug,
            canonical_event_id=loaded.canonical_event_id,
            items=related[:PUBLIC_RELATED_LIMIT],
        )


# Sentinel for "no last_published_at" so the sort key never gets a
# None.  Real ``PublicEventPage`` rows in PUBLISHED state always have
# a ``last_published_at`` (enforced by the entity validator + DB CHECK
# constraint), so this is only hit by malformed test fixtures.
_SORT_MIN: Any = ""


# ── Public surface ───────────────────────────────────────────────────────────

# Re-export everything the router needs.  We intentionally do not
# export the internal helpers so external callers can't bypass the
# visibility gates.

__all__ = [
    "DEFAULT_PUBLIC_LIST_LIMIT",
    "MAX_PUBLIC_LIST_LIMIT",
    "GetPublicEventEvidence",
    "GetPublicEventPage",
    "GetPublicEventRelated",
    "GetPublicEventTimeline",
    "ListPublicEvents",
    "PublicEventDetail",
    "PublicEventListItem",
    "PublicEventListResult",
    "PublicEvidenceClaim",
    "PublicEvidenceResponse",
    "PublicEvidenceSource",
    "PublicRelatedEvent",
    "PublicRelatedResponse",
    "PublicTimelineEvent",
    "PublicTimelineResponse",
]

# These imports are not used at module level but are referenced in
# docstrings/type comments; explicit re-bind so static checkers don't
# flag them as unused.
_ = (AccidentEvent, OrionRelationship)
