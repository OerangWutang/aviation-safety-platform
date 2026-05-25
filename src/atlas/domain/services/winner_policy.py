from uuid import UUID

from atlas.domain.entities import Claim, Source
from atlas.domain.enums import ClaimType


class WinnerPolicy:
    """Select the single best claim for a field from active candidates.

    Priority, highest wins:
    1. claim_type rank: MANUAL_OVERRIDE > CONFIRMED > RAW > SUPERSEDED
    2. reliability_tier: lower tier number is more trusted
    3. created_at: older claim wins among equal-tier peers
    4. id string: deterministic final tiebreaker
    """

    def choose_winner(self, claims: list[Claim], sources_by_id: dict[UUID, Source]) -> Claim | None:
        active_claims = [claim for claim in claims if claim.can_win()]
        if not active_claims:
            return None
        return sorted(
            active_claims, key=lambda claim: self._sort_key(claim, sources_by_id), reverse=True
        )[0]

    def _sort_key(
        self, claim: Claim, sources_by_id: dict[UUID, Source]
    ) -> tuple[int, int, float, str]:
        source = sources_by_id.get(claim.source_id)
        tier_score = -(source.reliability_tier if source else 999)
        age_score = -claim.created_at.timestamp()
        return (
            self._claim_type_rank(claim.claim_type),
            tier_score,
            age_score,
            str(claim.id),
        )

    @staticmethod
    def _claim_type_rank(claim_type: ClaimType) -> int:
        match claim_type:
            case ClaimType.MANUAL_OVERRIDE:
                return 3
            case ClaimType.CONFIRMED:
                return 2
            case ClaimType.RAW:
                return 1
            case ClaimType.SUPERSEDED:
                return 0
