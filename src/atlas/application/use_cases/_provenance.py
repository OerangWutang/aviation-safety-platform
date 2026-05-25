"""Helpers for selecting field-level provenance consistently with projections."""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Callable
from uuid import UUID

from atlas.application.unit_of_work import UnitOfWork
from atlas.domain.entities import Claim, ClaimConflict, Source
from atlas.domain.services.conflict_utils import latest_resolved_conflicts_by_field
from atlas.domain.services.winner_policy import WinnerPolicy

logger = logging.getLogger(__name__)

SafeString = Callable[[object], str | None]


def _same_projected_value(value: object, projected: object, safe_str: SafeString) -> bool:
    claim_value = safe_str(value)
    projected_value = safe_str(projected)
    if claim_value is None or projected_value is None:
        return False
    return claim_value.casefold() == projected_value.casefold()


async def select_projected_claims_by_field(
    uow: UnitOfWork,
    *,
    event_id: UUID,
    fields: dict[str, object],
    safe_str: SafeString,
    winner_policy: WinnerPolicy | None = None,
) -> dict[str, Claim]:
    """Return the claim that best supports each projected field value.

    Orion and Chronos need field-level provenance. Earlier versions re-ranked
    matching claims locally with a simplified ``claim_type -> created_at`` sort,
    which could disagree with Atlas projection when sources have different
    reliability tiers or when a resolved conflict selected a specific winner.

    This helper centralizes the selection:
    * resolved conflicts with an active matching winning claim are honored;
    * otherwise the real ``WinnerPolicy`` is used with ``sources_by_id`` so
      source reliability tier participates in the tie-breaks;
    * claims whose value does not match the current projected value are never
      attached as provenance.
    """

    policy = winner_policy or WinnerPolicy()
    raw_claims = await uow.claims.find_active_by_event(event_id)
    source_ids = sorted({claim.source_id for claim in raw_claims}, key=str)
    sources = await uow.sources.get_by_ids(source_ids)
    sources_by_id: dict[UUID, Source] = {source.id: source for source in sources}

    claims_by_field: dict[str, list[Claim]] = defaultdict(list)
    claims_by_id: dict[UUID, Claim] = {}
    for claim in raw_claims:
        if claim.event_id != event_id or not claim.is_active:
            continue
        claims_by_field[claim.field_name].append(claim)
        claims_by_id[claim.id] = claim

    resolved_by_field: dict[str, ClaimConflict] = {}
    try:
        conflicts = await uow.conflicts.find_by_event(event_id)
    except (AttributeError, NotImplementedError):
        # Provenance should degrade gracefully if a fake/older repository does
        # not implement ``find_by_event`` (e.g. in unit tests).  Projection
        # itself remains authoritative; we just fall back to WinnerPolicy for
        # the matching claims.  Narrowed catch: real repository/database
        # errors (OperationalError, IntegrityError, MappingError, etc.) now
        # propagate instead of being silently swallowed — provenance is
        # audit-sensitive and silent degradation should be rare and visible.
        logger.debug(
            "provenance_select: conflicts repo does not support find_by_event "
            "for event_id=%s; falling back to WinnerPolicy",
            event_id,
            exc_info=True,
        )
    else:
        try:
            resolved_by_field = latest_resolved_conflicts_by_field(conflicts)
        except (TypeError, AttributeError, KeyError):
            # Same posture for the in-memory analyser: only compatibility-
            # shaped errors get the soft fallback, everything else surfaces.
            logger.warning(
                "provenance_select: failed to interpret conflict history for "
                "event_id=%s; falling back to WinnerPolicy",
                event_id,
                exc_info=True,
            )

    selected: dict[str, Claim] = {}
    for field_name, field_claims in claims_by_field.items():
        projected_value = fields.get(field_name)
        if safe_str(projected_value) is None:
            continue

        resolved = resolved_by_field.get(field_name)
        if resolved and resolved.winning_claim_id:
            resolved_winner = claims_by_id.get(resolved.winning_claim_id)
            if resolved_winner is not None and _same_projected_value(
                resolved_winner.field_value,
                projected_value,
                safe_str,
            ):
                selected[field_name] = resolved_winner
                continue

        matching_claims = [
            claim
            for claim in field_claims
            if _same_projected_value(claim.field_value, projected_value, safe_str)
        ]
        winner = policy.choose_winner(matching_claims, sources_by_id)
        if winner is not None:
            selected[field_name] = winner

    return selected
