from __future__ import annotations

import logging
from collections import defaultdict
from uuid import UUID

from atlas.domain.constants import DISPUTED
from atlas.domain.entities import Claim, ClaimConflict, ProjectedAccidentRecord, Source
from atlas.domain.enums import ClaimType, ConflictStatus
from atlas.domain.services.completeness import CompletenessCalculator
from atlas.domain.services.conflict_detector import unique_normalised_values
from atlas.domain.services.conflict_utils import latest_resolved_conflicts_by_field
from atlas.domain.services.winner_policy import WinnerPolicy

logger = logging.getLogger(__name__)


class ProjectionBuilder:
    """Build a ProjectedAccidentRecord from claims and conflicts.

    Dispute safety
    --------------
    A field is marked DISPUTED whenever active claims carry more than one
    distinct normalised value AND there is no resolved conflict that
    legitimately selected a winner.  Crucially, the projection does NOT
    require an open ClaimConflict row to be present before marking a field
    disputed.  Conflict detection may lag, fail, or race; relying on it to
    prevent silent winner selection would create a dangerous failure mode
    where two active claims disagree and the projection quietly picks one.

    The open-conflict index is still respected: a field already marked open
    in the conflict table goes straight to DISPUTED without re-evaluating
    the value set.  But the builder also performs its own value-set check so
    that a late or absent conflict row cannot cause a false "uncontested"
    projection.
    """

    def __init__(
        self,
        winner_policy: WinnerPolicy | None = None,
        completeness: CompletenessCalculator | None = None,
    ) -> None:
        self.winner_policy = winner_policy or WinnerPolicy()
        self.completeness = completeness or CompletenessCalculator()

    def build(
        self,
        *,
        event_id: UUID,
        claims: list[Claim],
        conflicts: list[ClaimConflict],
        sources_by_id: dict[UUID, Source],
        projection_version: int,
    ) -> ProjectedAccidentRecord:
        claims_by_field: dict[str, list[Claim]] = defaultdict(list)
        for claim in claims:
            if claim.event_id == event_id and claim.is_active:
                claims_by_field[claim.field_name].append(claim)

        open_conflicts = {
            c.field_name: c
            for c in conflicts
            if c.event_id == event_id and c.status == ConflictStatus.OPEN
        }
        # Pick the most-recent resolved conflict deterministically when
        # historical resolved conflicts exist for the same event/field. The DB
        # only guarantees at most one OPEN conflict per event/field; RESOLVED
        # rows are retained for audit history.
        resolved_conflicts = latest_resolved_conflicts_by_field(
            [conflict for conflict in conflicts if conflict.event_id == event_id]
        )

        fields: dict[str, object] = {}
        # Tracks fields that are disputed (both open conflict rows AND
        # defensively-detected value-set disagreements).
        disputed_fields: set[str] = set()
        all_fields = sorted(set(claims_by_field) | set(open_conflicts))
        for field_name in all_fields:
            # Fast path: an open conflict row is authoritative — mark disputed.
            if field_name in open_conflicts:
                fields[field_name] = DISPUTED
                disputed_fields.add(field_name)
                continue

            field_claims = claims_by_field.get(field_name, [])
            resolved = resolved_conflicts.get(field_name)

            if resolved and resolved.winning_claim_id:
                winner_candidate = next(
                    (claim for claim in field_claims if claim.id == resolved.winning_claim_id),
                    None,
                )
                if winner_candidate is not None:
                    # Resolved conflict with a valid active winner — use it directly.
                    fields[field_name] = winner_candidate.field_value
                    continue
                # Winner is no longer active (e.g. superseded by a source-record
                # correction). Fall back to the value-set check below rather than
                # silently delegating to winner policy alone.
                logger.warning(
                    "Resolved conflict %s for event %s field %r has an inactive "
                    "winning_claim_id=%s - falling back to value-set check. "
                    "Consider reopening the conflict for curator review.",
                    resolved.id,
                    event_id,
                    field_name,
                    resolved.winning_claim_id,
                )

            # Authority-tiered dispute check.
            #
            # The defensive check must respect claim authority rather than
            # comparing all winnable claims in a flat pool.  A MANUAL_OVERRIDE
            # intentionally supersedes RAW evidence; treating the disagreement
            # between an override and its source as a dispute would incorrectly
            # block curator-approved values.
            #
            # Tiers (highest authority wins):
            #   1. MANUAL_OVERRIDE: a curator explicitly set this value.
            #      If overrides disagree with each other → DISPUTED.
            #      If exactly one distinct override value exists → use it.
            #   2. CONFIRMED: source-confirmed values with no override present.
            #      If confirmed claims disagree with each other → DISPUTED.
            #      If exactly one distinct confirmed value → use it.
            #   3. RAW: apply the defensive check at this tier only.
            #      If RAW claims disagree → DISPUTED (conflict detection may lag).
            #      If they agree → use winner policy for tier/age tiebreaking.
            #
            # All normalisation uses unique_normalised_values() so the same
            # coercion rules as ConflictDetector apply (numeric strings, booleans,
            # whitespace collapse, etc.).
            override_claims = [c for c in field_claims if c.claim_type == ClaimType.MANUAL_OVERRIDE]
            if override_claims:
                if len(unique_normalised_values(override_claims)) > 1:
                    logger.debug(
                        "Defensive dispute (override tier): event %s field %r has multiple "
                        "distinct MANUAL_OVERRIDE values. Marking DISPUTED.",
                        event_id,
                        field_name,
                    )
                    fields[field_name] = DISPUTED
                    disputed_fields.add(field_name)
                else:
                    winner = self.winner_policy.choose_winner(override_claims, sources_by_id)
                    if winner is not None:
                        fields[field_name] = winner.field_value
                continue

            confirmed_claims = [c for c in field_claims if c.claim_type == ClaimType.CONFIRMED]
            if confirmed_claims:
                if len(unique_normalised_values(confirmed_claims)) > 1:
                    logger.debug(
                        "Defensive dispute (confirmed tier): event %s field %r has multiple "
                        "distinct CONFIRMED values but no open conflict row. Marking DISPUTED.",
                        event_id,
                        field_name,
                    )
                    fields[field_name] = DISPUTED
                    disputed_fields.add(field_name)
                else:
                    winner = self.winner_policy.choose_winner(confirmed_claims, sources_by_id)
                    if winner is not None:
                        fields[field_name] = winner.field_value
                continue

            # RAW tier — defensive check guards against conflict-detection lag.
            raw_claims = [c for c in field_claims if c.claim_type == ClaimType.RAW]
            if len(unique_normalised_values(raw_claims)) > 1:
                logger.debug(
                    "Defensive dispute (raw tier): event %s field %r has %d distinct "
                    "active values but no open conflict row. Marking DISPUTED until "
                    "conflict detection runs.",
                    event_id,
                    field_name,
                    len(unique_normalised_values(raw_claims)),
                )
                fields[field_name] = DISPUTED
                disputed_fields.add(field_name)
                continue

            # Pass ``raw_claims`` rather than ``field_claims`` to keep the
            # per-tier invariant explicit: at the RAW tier only RAW claims are
            # eligible candidates.  Today this is equivalent because
            # ``is_active`` upstream already filtered to {RAW, CONFIRMED,
            # MANUAL_OVERRIDE} and both higher tiers ``continue``d earlier, so
            # ``field_claims == raw_claims`` here.  But naming the candidate
            # set we actually intend prevents a future active ClaimType (e.g.
            # a TENTATIVE state) from silently bypassing the per-tier dispute
            # check below.  Pinned by
            # ``test_raw_tier_winner_only_considers_raw_claims``.
            winner = self.winner_policy.choose_winner(raw_claims, sources_by_id)
            if winner is not None:
                fields[field_name] = winner.field_value

        return ProjectedAccidentRecord(
            event_id=event_id,
            projection_version=projection_version,
            fields=fields,
            completeness_score=self.completeness.score(fields),
            unresolved_conflict_fields=sorted(disputed_fields),
        )
