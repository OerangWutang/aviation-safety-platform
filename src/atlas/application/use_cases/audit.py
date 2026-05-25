"""Audit explanations for non-technical readers (Phase 11).

These use cases compose existing pieces — ``WinnerPolicy``, claims,
sources, conflicts, ``ProjectedAccidentRecord``, ``ClaimHistory``,
``RawSnapshot`` — and translate them into something a journalist,
investigator, or family member can read.

Two response modes are supported on every endpoint:

- **summary** (default): non-technical prose plus the minimum
  structured fields needed for a UI to render.
- **expert**: adds the full evidence row including claim ids,
  source reliability tiers, hash values, and the sort-key components
  the WinnerPolicy used.

The translation is intentionally centralised here.  Routers stay
thin; the prose lives next to the rules it describes.

Design principles
-----------------

- **No new sources of truth.**  Every value rendered to the user
  comes from a row that already exists.  If the projection changes,
  the explanation changes with it.
- **No re-implementation of the WinnerPolicy rule.**  The audit
  read uses the existing ``select_projected_claims_by_field``
  helper, which is what Provenance also uses; "this is what won"
  and "this is what the audit endpoint says won" stay aligned by
  construction.
- **Field-locked field names.**  Public callers can only ask about
  field names that exist in the projection.  Probing for arbitrary
  field names returns 404, not a half-shaped response, so the
  endpoint cannot be used to map the schema of internal fields that
  do not appear publicly.
- **Hash verification stays public-safe.**  We surface
  ``raw_payload_hash`` and the canonicalization recipe, never the
  raw payload itself.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from atlas.application.unit_of_work import UnitOfWork
from atlas.application.use_cases._provenance import (
    select_projected_claims_by_field,
)
from atlas.application.use_cases.public_events import (
    _load_page_and_check_visibility,
)
from atlas.domain.entities import (
    Claim,
    ClaimConflict,
    ClaimHistory,
    ProjectedAccidentRecord,
    RawSnapshot,
    Source,
)
from atlas.domain.enums import ClaimType, ConflictStatus
from atlas.domain.exceptions import NotFoundError
from atlas.domain.services.winner_policy import WinnerPolicy

logger = logging.getLogger(__name__)


# Cap on how many losing/superseded claims we render in a single
# field-explanation response.  The audit response is meant for human
# consumption; truncation past this cap is reported in the response
# rather than silently dropping rows.
_MAX_LOSING_CLAIMS = 25

# Cap on how many history rows we render for one claim.
_MAX_CLAIM_HISTORY_ROWS = 50

# Stable token used by the source-verification endpoint to identify
# the canonicalization recipe a verifier should apply.  Bumping this
# tag is a deliberate version change — any reader who saved the
# verification response would need to know which recipe was current
# when the hash was computed.
SOURCE_VERIFICATION_RECIPE_VERSION = "v1-utf8-sha256-sorted-keys"


# ── Plain-English glossary ───────────────────────────────────────────────────
#
# Phrases below are intentionally short and avoid jargon.  They live
# in one place so a copy editor can tune them without hunting through
# the codebase.


_CLAIM_TYPE_PROSE: dict[str, str] = {
    "MANUAL_OVERRIDE": (
        "An editor manually set this value, overriding the automated evidence chain."
    ),
    "CONFIRMED": "An editor confirmed this value after reviewing the sources.",
    "RAW": "This value comes directly from a source as it was ingested.",
    "SUPERSEDED": (
        "This claim has been replaced by a newer one and no longer "
        "participates in current evidence."
    ),
}


def _claim_type_prose(claim_type: ClaimType) -> str:
    return _CLAIM_TYPE_PROSE.get(claim_type.value, claim_type.value)


def _why_winner_prose(
    winner: Claim,
    winner_source: Source | None,
    candidates: list[tuple[Claim, Source | None]],
) -> str:
    """Render a one-sentence explanation of why ``winner`` won.

    The text mirrors the WinnerPolicy's actual decision steps:

    1. Manual override / confirmed beats raw.
    2. Lower reliability_tier wins.
    3. Older claim wins among equal peers.

    Ordered from most user-meaningful to least.
    """
    if winner.claim_type == ClaimType.MANUAL_OVERRIDE:
        return "An editor manually set this value, which overrides any automated evidence."
    if winner.claim_type == ClaimType.CONFIRMED:
        return (
            "An editor confirmed this value after reviewing the sources, "
            "and no manual override is in place."
        )
    # Plain RAW winner — pick the reason out of the WinnerPolicy
    # sort key: was it the only candidate, the most reliable source,
    # or the oldest?
    raws = [(c, s) for c, s in candidates if c.claim_type == ClaimType.RAW]
    if len(raws) <= 1:
        return (
            "This is the only source reporting this value, so it is the current best explanation."
        )
    # More than one RAW candidate.  Compare reliability tiers.
    tier_of_winner = winner_source.reliability_tier if winner_source is not None else 999
    has_lower_tier = any(
        s is not None and s.reliability_tier < tier_of_winner for c, s in raws if c.id != winner.id
    )
    has_equal_tier = any(
        s is not None and s.reliability_tier == tier_of_winner for c, s in raws if c.id != winner.id
    )
    if not has_lower_tier and has_equal_tier:
        return (
            "Multiple sources agree on this value; this claim was "
            "selected because it was reported earliest."
        )
    if not has_lower_tier:
        return "This is the most reliable source reporting this value."
    # Should be unreachable: a lower-tier (more trusted) source
    # exists, yet this claim won.  That implies the lower-tier
    # source's claim does not match the projection (was filtered out
    # by ``select_projected_claims_by_field``).  Fall back to a
    # truthful, generic phrasing.
    return (
        "This claim was selected from the candidate sources that match the current projected value."
    )


def _why_loser_prose(loser: Claim, loser_source: Source | None, winner: Claim) -> str:
    if loser.claim_type == ClaimType.SUPERSEDED:
        return (
            "This claim was replaced by a newer claim and no longer "
            "participates in the evidence chain."
        )
    if loser.field_value != winner.field_value:
        return (
            "This source reports a different value, which is currently treated as a disagreement."
        )
    # Same value, lost on rank — usually a higher (less trusted) tier.
    return "This source agrees with the current value but was outranked by a more reliable source."


# ── Helpers ──────────────────────────────────────────────────────────────────


def _confidence_label(score: float | None) -> str:
    """The same band logic used by the public list and search index."""
    if score is None:
        return "unknown"
    if score >= 0.85:
        return "high"
    if score >= 0.5:
        return "medium"
    if score > 0.0:
        return "low"
    return "unknown"


def _confidence_meaning(label: str) -> str:
    return {
        "high": ("Most of the expected fields are filled in and largely agree across sources."),
        "medium": ("Some fields are filled in and broadly agree; some are missing or disputed."),
        "low": (
            "Few fields are filled in.  Coverage is thin and details "
            "may change as more sources arrive."
        ),
        "unknown": ("We do not yet have enough evidence to assess this event's completeness."),
    }.get(label, label)


# ── Page-level audit ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class PageAuditFieldRow:
    field_name: str
    current_value: Any
    is_disputed: bool
    is_manually_overridden: bool
    confidence: str
    plain_english: str


@dataclass(frozen=True)
class PageAuditResponse:
    slug: str
    canonical_event_id: UUID
    summary: str
    confidence: str
    confidence_meaning: str
    projection_version: int
    last_updated_at: datetime
    fields: list[PageAuditFieldRow]


class GetPublicEventAudit:
    """High-level audit of a single public event page.

    The non-technical reader gets:

    - a single-sentence summary describing the overall state;
    - a confidence band with a plain-English meaning;
    - one row per projected field with status flags and a short
      explanation.

    The detailed per-field explanation (sources, conflicts, winner
    rationale) lives behind :class:`GetFieldExplanation` so this
    response stays short enough to fit on a single screen.
    """

    def __init__(self, uow: UnitOfWork):
        self._uow = uow

    async def execute(self, slug: str) -> PageAuditResponse:
        loaded = await _load_page_and_check_visibility(self._uow, slug)
        projection = await self._uow.projections.get(loaded.canonical_event_id)
        if projection is None:
            raise NotFoundError(f"Public event page {slug!r} has no projection available")
        active_claims = await self._uow.claims.find_active_by_event(loaded.canonical_event_id)
        manually_overridden_fields = {
            c.field_name for c in active_claims if c.claim_type == ClaimType.MANUAL_OVERRIDE
        }
        disputed = set(projection.unresolved_conflict_fields)

        rows: list[PageAuditFieldRow] = [
            PageAuditFieldRow(
                field_name=name,
                current_value=value,
                is_disputed=name in disputed,
                is_manually_overridden=name in manually_overridden_fields,
                confidence=_confidence_label(projection.completeness_score),
                plain_english=_field_summary_prose(
                    name=name,
                    is_disputed=name in disputed,
                    is_manually_overridden=name in manually_overridden_fields,
                ),
            )
            for name, value in projection.fields.items()
        ]
        # Stable order so UI rendering and tests are deterministic.
        rows.sort(key=lambda r: r.field_name)

        confidence = _confidence_label(projection.completeness_score)
        return PageAuditResponse(
            slug=loaded.page.slug,
            canonical_event_id=loaded.canonical_event_id,
            summary=_overall_summary_prose(
                disputed_count=len(disputed),
                manual_override_count=len(manually_overridden_fields),
                total_fields=len(rows),
            ),
            confidence=confidence,
            confidence_meaning=_confidence_meaning(confidence),
            projection_version=projection.projection_version,
            last_updated_at=projection.updated_at,
            fields=rows,
        )


def _overall_summary_prose(
    *,
    disputed_count: int,
    manual_override_count: int,
    total_fields: int,
) -> str:
    parts: list[str] = []
    parts.append(
        f"This page summarises {total_fields} field"
        f"{'' if total_fields == 1 else 's'} drawn from the evidence "
        f"chain."
    )
    if disputed_count > 0:
        parts.append(
            f"{disputed_count} field"
            f"{'' if disputed_count == 1 else 's'} are currently "
            f"disputed between sources."
        )
    if manual_override_count > 0:
        parts.append(
            f"{manual_override_count} field"
            f"{'' if manual_override_count == 1 else 's'} were set by "
            f"an editor."
        )
    if disputed_count == 0 and manual_override_count == 0:
        parts.append("All fields are currently supported by source evidence.")
    return " ".join(parts)


def _field_summary_prose(*, name: str, is_disputed: bool, is_manually_overridden: bool) -> str:
    if is_manually_overridden:
        return "An editor set this value, overriding the automated evidence chain."
    if is_disputed:
        return (
            "Sources disagree on this value.  See the field "
            "explanation for the candidate values and the conflict "
            "status."
        )
    return "This value is supported by current source evidence."


# ── Field-level explanation ─────────────────────────────────────────────────


@dataclass(frozen=True)
class ExpertWinnerDetail:
    """Expert-mode addendum on the winning claim."""

    claim_id: UUID
    claim_type: str
    source_reliability_tier: int | None
    created_at: datetime


@dataclass(frozen=True)
class FieldExplanationWinner:
    field_name: str
    current_value: Any
    plain_english: str
    source_name: str
    source_kind: str
    expert: ExpertWinnerDetail | None = None


@dataclass(frozen=True)
class FieldExplanationLoser:
    source_name: str
    source_kind: str
    reported_value: Any
    plain_english: str
    expert: ExpertWinnerDetail | None = None


@dataclass(frozen=True)
class FieldExplanationConflict:
    status: str
    plain_english: str
    resolved_at: datetime | None


@dataclass(frozen=True)
class FieldExplanationResponse:
    event_id: UUID
    field_name: str
    has_winner: bool
    winner: FieldExplanationWinner | None
    losers: list[FieldExplanationLoser]
    losers_truncated: bool
    conflict: FieldExplanationConflict | None


class GetFieldExplanation:
    """Per-field deep audit.

    Walks the existing active-claim/conflict tables and produces:

    - the winning claim, with its source, plus a plain-English why;
    - the losing or superseded claims (capped), with a per-row why;
    - the current conflict status if any.

    Public callers can only ask about fields that appear in the
    current projection.  Probing for arbitrary field names returns
    404, so the endpoint cannot be used to enumerate fields that the
    projection deliberately omits from the public surface.
    """

    def __init__(self, uow: UnitOfWork, *, expert: bool = False):
        self._uow = uow
        self._expert = expert
        self._policy = WinnerPolicy()

    async def execute(self, event_id: UUID, field_name: str) -> FieldExplanationResponse:
        projection = await self._uow.projections.get(event_id)
        if projection is None:
            raise NotFoundError(f"No projection found for event {event_id}")
        if field_name not in projection.fields:
            # 404 rather than empty response.  Phase-1 plan: do not
            # expose internal-only field names through public probing.
            raise NotFoundError(
                f"Field {field_name!r} is not part of the public projection for event {event_id}"
            )

        winner_claim = await self._compute_winner(projection, field_name)
        active_claims = await self._uow.claims.find_active_by_event_field(event_id, field_name)
        # Pull sources used in the explanation in one batch.
        source_ids = sorted({c.source_id for c in active_claims}, key=str)
        if winner_claim and winner_claim.source_id not in source_ids:
            source_ids.append(winner_claim.source_id)
        sources_list = await self._uow.sources.get_by_ids(source_ids)
        sources_by_id: dict[UUID, Source] = {s.id: s for s in sources_list}

        winner_response = (
            self._render_winner(winner_claim, sources_by_id, active_claims)
            if winner_claim is not None
            else None
        )

        losers_response, truncated = self._render_losers(
            winner_claim=winner_claim,
            active_claims=active_claims,
            sources_by_id=sources_by_id,
        )

        conflict_response = await self._render_conflict(event_id, field_name)

        return FieldExplanationResponse(
            event_id=event_id,
            field_name=field_name,
            has_winner=winner_response is not None,
            winner=winner_response,
            losers=losers_response,
            losers_truncated=truncated,
            conflict=conflict_response,
        )

    async def _compute_winner(
        self, projection: ProjectedAccidentRecord, field_name: str
    ) -> Claim | None:
        """Return the claim that best supports the projected value.

        Delegated to the existing provenance helper so this stays
        consistent with the rest of the system.
        """
        selected = await select_projected_claims_by_field(
            self._uow,
            event_id=projection.event_id,
            fields={field_name: projection.fields[field_name]},
            safe_str=lambda v: str(v) if v is not None else None,
            winner_policy=self._policy,
        )
        return selected.get(field_name)

    def _render_winner(
        self,
        winner: Claim,
        sources_by_id: dict[UUID, Source],
        active_claims: list[Claim],
    ) -> FieldExplanationWinner:
        winner_source = sources_by_id.get(winner.source_id)
        candidates_with_sources = [(c, sources_by_id.get(c.source_id)) for c in active_claims]
        return FieldExplanationWinner(
            field_name=winner.field_name,
            current_value=winner.field_value,
            plain_english=_why_winner_prose(winner, winner_source, candidates_with_sources),
            source_name=winner_source.name if winner_source else "(unknown source)",
            source_kind=(winner_source.kind.value if winner_source else "EXTERNAL"),
            expert=self._expert_detail(winner, winner_source),
        )

    def _render_losers(
        self,
        *,
        winner_claim: Claim | None,
        active_claims: list[Claim],
        sources_by_id: dict[UUID, Source],
    ) -> tuple[list[FieldExplanationLoser], bool]:
        if winner_claim is None:
            losers_candidates: list[Claim] = active_claims
        else:
            losers_candidates = [c for c in active_claims if c.id != winner_claim.id]
        # Sort losers oldest-first for deterministic output.
        losers_candidates.sort(key=lambda c: (c.created_at, c.id))
        truncated = len(losers_candidates) > _MAX_LOSING_CLAIMS
        if truncated:
            losers_candidates = losers_candidates[:_MAX_LOSING_CLAIMS]

        rendered: list[FieldExplanationLoser] = []
        for c in losers_candidates:
            source = sources_by_id.get(c.source_id)
            rendered.append(
                FieldExplanationLoser(
                    source_name=source.name if source else "(unknown source)",
                    source_kind=source.kind.value if source else "EXTERNAL",
                    reported_value=c.field_value,
                    plain_english=(
                        _why_loser_prose(c, source, winner_claim)
                        if winner_claim is not None
                        else (
                            "No claim is currently winning for this field; "
                            "the projection may be undecided."
                        )
                    ),
                    expert=self._expert_detail(c, source),
                )
            )
        return rendered, truncated

    async def _render_conflict(
        self, event_id: UUID, field_name: str
    ) -> FieldExplanationConflict | None:
        conflict: ClaimConflict | None
        try:
            conflict = await self._uow.conflicts.find_by_event_field(event_id, field_name)
        except (AttributeError, NotImplementedError):
            # Some fake repositories used in older tests don't expose
            # this; soft-degrade rather than fail the read.
            logger.debug(
                "audit: conflict repo missing find_by_event_field for event_id=%s field=%s",
                event_id,
                field_name,
                exc_info=True,
            )
            return None
        if conflict is None:
            return None
        return FieldExplanationConflict(
            status=conflict.status.value,
            plain_english=_conflict_prose(conflict),
            resolved_at=conflict.resolved_at,
        )

    def _expert_detail(self, claim: Claim, source: Source | None) -> ExpertWinnerDetail | None:
        if not self._expert:
            return None
        return ExpertWinnerDetail(
            claim_id=claim.id,
            claim_type=claim.claim_type.value,
            source_reliability_tier=(source.reliability_tier if source is not None else None),
            created_at=claim.created_at,
        )


def _conflict_prose(conflict: ClaimConflict) -> str:
    if conflict.status == ConflictStatus.RESOLVED:
        return (
            "Sources disagreed on this value, but an editor reviewed the "
            "evidence and resolved the disagreement."
        )
    return (
        "Sources currently disagree on this value.  The displayed value "
        "is the current best explanation while the disagreement is "
        "reviewed."
    )


# ── Claim-level explanation ─────────────────────────────────────────────────


@dataclass(frozen=True)
class ClaimHistoryRow:
    action: str
    reason: str
    to_claim_type: str
    from_claim_type: str | None
    created_at: datetime


@dataclass(frozen=True)
class ClaimExplanationResponse:
    claim_id: UUID
    event_id: UUID
    field_name: str
    field_value: Any
    claim_type: str
    plain_english: str
    source_name: str
    source_kind: str
    is_winning: bool
    is_active: bool
    is_superseded: bool
    created_at: datetime
    history: list[ClaimHistoryRow]
    history_truncated: bool


class GetClaimExplanation:
    """Single-claim audit.

    Answers: what role does this claim play right now?  Is it winning?
    Was it superseded?  What's its provenance trail?

    Internal-only attributes (raw_snapshot_id, created_by user id)
    are deliberately not exposed on this response.  The expert mode
    on field explanations carries claim_id and tier; this endpoint is
    keyed on claim_id already, so there's nothing more to expose.
    """

    def __init__(self, uow: UnitOfWork):
        self._uow = uow

    async def execute(self, claim_id: UUID) -> ClaimExplanationResponse:
        claim = await self._uow.claims.get(claim_id)
        if claim is None:
            raise NotFoundError(f"Claim {claim_id} not found")
        source = (
            await self._uow.sources.get(claim.source_id) if claim.source_id is not None else None
        )

        # "Is this claim winning?": fetch the current winner for the
        # same field and compare ids.  If the projection has a
        # different value than the claim, this claim cannot be
        # winning.
        is_winning = False
        projection = await self._uow.projections.get(claim.event_id)
        if projection is not None and claim.field_name in projection.fields:
            selected = await select_projected_claims_by_field(
                self._uow,
                event_id=claim.event_id,
                fields={claim.field_name: projection.fields[claim.field_name]},
                safe_str=lambda v: str(v) if v is not None else None,
            )
            winner = selected.get(claim.field_name)
            is_winning = winner is not None and winner.id == claim.id

        history_rows = await self._fetch_claim_history(claim)
        truncated = len(history_rows) > _MAX_CLAIM_HISTORY_ROWS
        if truncated:
            history_rows = history_rows[:_MAX_CLAIM_HISTORY_ROWS]

        return ClaimExplanationResponse(
            claim_id=claim.id,
            event_id=claim.event_id,
            field_name=claim.field_name,
            field_value=claim.field_value,
            claim_type=claim.claim_type.value,
            plain_english=_claim_type_prose(claim.claim_type),
            source_name=source.name if source else "(unknown source)",
            source_kind=source.kind.value if source else "EXTERNAL",
            is_winning=is_winning,
            is_active=claim.is_active,
            is_superseded=claim.claim_type == ClaimType.SUPERSEDED,
            created_at=claim.created_at,
            history=[
                ClaimHistoryRow(
                    action=h.action,
                    reason=h.reason,
                    to_claim_type=h.to_claim_type.value,
                    from_claim_type=(h.from_claim_type.value if h.from_claim_type else None),
                    created_at=h.created_at,
                )
                for h in history_rows
            ],
            history_truncated=truncated,
        )

    async def _fetch_claim_history(self, claim: Claim) -> list[ClaimHistory]:
        # No per-claim history method exists on the repository; we
        # fetch the event-scoped history and filter.  Bounded by the
        # event's claim count, which is small in practice.
        try:
            all_history = await self._uow.claim_history.find_by_event(claim.event_id)
        except (AttributeError, NotImplementedError):
            logger.debug(
                "audit: claim history repo missing find_by_event for "
                "claim_id=%s; returning empty history",
                claim.id,
                exc_info=True,
            )
            return []
        rows = [h for h in all_history if h.claim_id == claim.id]
        rows.sort(key=lambda h: (h.created_at, h.id))
        return rows


# ── Source verification ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class SourceVerificationResponse:
    snapshot_id: UUID
    source_name: str
    source_kind: str
    source_record_id: str | None
    raw_payload_hash: str | None
    captured_at: datetime
    recipe_version: str
    recipe_steps: list[str]
    verification_note: str


class GetSourceVerification:
    """Source-record hash verification (public-safe).

    Exposes the snapshot's ``raw_payload_hash`` plus a canonicalization
    recipe a reader can apply to the original source payload to verify
    the hash independently.

    We deliberately do NOT return the raw payload.  Sources retain
    their content rights, and a verifier is expected to fetch the
    source themselves (the URL is part of the source record's
    metadata, surfaced elsewhere on the page).  This keeps the
    verification audit honest without making Atlas a content
    redistribution channel.
    """

    def __init__(self, uow: UnitOfWork):
        self._uow = uow

    async def execute(self, snapshot_id: UUID) -> SourceVerificationResponse:
        snapshot = await self._fetch_snapshot(snapshot_id)
        source = await self._uow.sources.get(snapshot.source_id)
        return SourceVerificationResponse(
            snapshot_id=snapshot.id,
            source_name=source.name if source else "(unknown source)",
            source_kind=source.kind.value if source else "EXTERNAL",
            source_record_id=snapshot.source_record_id,
            raw_payload_hash=snapshot.raw_payload_hash,
            captured_at=snapshot.captured_at,
            recipe_version=SOURCE_VERIFICATION_RECIPE_VERSION,
            recipe_steps=[
                ("Fetch the original source record at the URL on the source's metadata page."),
                (
                    "Parse the payload as JSON.  If the source provides "
                    "another format, normalise it the same way the "
                    "ingestion pipeline did (consult the source's "
                    "documentation page)."
                ),
                (
                    "Re-serialise the JSON with sorted keys, no extra "
                    "whitespace, and UTF-8 encoding."
                ),
                ("Compute the SHA-256 hash of the resulting bytes."),
                (
                    "Compare the result with ``raw_payload_hash``.  A "
                    "match confirms the source we recorded matches what "
                    "you fetched today; a mismatch means the source has "
                    "been edited or the recipe has changed (see "
                    f"``recipe_version`` = {SOURCE_VERIFICATION_RECIPE_VERSION})."
                ),
            ],
            verification_note=(
                "Atlas does not redistribute source content.  You verify "
                "the hash against the source you fetch directly from the "
                "publisher."
            ),
        )

    async def _fetch_snapshot(self, snapshot_id: UUID) -> RawSnapshot:
        snapshot = await self._uow.snapshots.get(snapshot_id)
        if snapshot is None:
            raise NotFoundError(f"Source snapshot {snapshot_id} not found")
        return snapshot


__all__ = [
    "SOURCE_VERIFICATION_RECIPE_VERSION",
    "ClaimExplanationResponse",
    "ClaimHistoryRow",
    "ExpertWinnerDetail",
    "FieldExplanationConflict",
    "FieldExplanationLoser",
    "FieldExplanationResponse",
    "FieldExplanationWinner",
    "GetClaimExplanation",
    "GetFieldExplanation",
    "GetPublicEventAudit",
    "GetSourceVerification",
    "PageAuditFieldRow",
    "PageAuditResponse",
    "SourceVerificationResponse",
]
