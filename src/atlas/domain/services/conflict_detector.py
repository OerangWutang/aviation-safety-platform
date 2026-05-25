from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from typing import Any
from uuid import uuid4

from atlas.domain.entities import Claim, ClaimConflict
from atlas.domain.enums import ConflictModifierReason, ConflictStatus


def _coerce_numeric(value: str) -> str | int | float:
    """Try to parse a normalized string as an int or float for comparison."""
    stripped = value.strip()
    try:
        as_int = int(stripped)
        # Do not coerce values with significant leading zeros such as aircraft
        # registrations or codes ("007"). Plain "0" is still numeric.
        if str(as_int) == stripped and (stripped == "0" or not stripped.startswith("0")):
            return as_int
    except ValueError:
        pass
    try:
        return float(stripped)
    except ValueError:
        return value


def normalize_value(value: Any) -> Any:
    """Normalize a claim value for conflict equality comparison."""
    if value is None:
        return None
    if isinstance(value, bool):
        return ("__bool__", value)
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, str):
        collapsed = " ".join(value.strip().lower().split())
        return _coerce_numeric(collapsed)
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, dict):
        return {k: normalize_value(v) for k, v in sorted(value.items())}
    if isinstance(value, list):
        return [normalize_value(v) for v in value]
    return value


def _make_hashable(value: Any) -> Any:
    """Recursively convert a normalised value to something hashable for set membership.

    Exposed at module level so callers in ``_conflict_reconciler`` can reuse it
    without reaching into ``ConflictDetector._hashable`` (a private method).
    """
    if isinstance(value, dict):
        return tuple((k, _make_hashable(v)) for k, v in sorted(value.items()))
    if isinstance(value, list):
        return tuple(_make_hashable(v) for v in value)
    return value


def unique_normalised_values(claims: list[Claim]) -> set[Any]:
    """Return the set of distinct normalised field values across *claims*.

    Convenience helper used by reconciliation logic to check whether all
    active claims for a field agree without instantiating ``ConflictDetector``.
    """
    return {_make_hashable(normalize_value(c.field_value)) for c in claims}


class ConflictDetector:
    """Detect conflicting active claims for the same (event_id, field_name) pair.

    Conflict policy
    ---------------
    By default (``require_multiple_sources=True``) a conflict is only raised
    when **at least two distinct sources** assert different values for the same
    field.  A single source sending contradictory values in one payload is
    classified as malformed source evidence rather than a genuine data conflict;
    rejecting same-source "self-conflicts" prevents a buggy ingestion client
    from flooding the conflict queue.

    Set ``require_multiple_sources=False`` if you explicitly want to surface
    intra-source contradictions (e.g. for source-quality auditing).
    """

    def __init__(self, require_multiple_sources: bool = True) -> None:
        self._require_multiple_sources = require_multiple_sources

    def detect(self, claims: list[Claim]) -> list[ClaimConflict]:
        claims_by_event_field: dict[tuple[Any, str], list[Claim]] = defaultdict(list)
        for claim in claims:
            if claim.is_active:
                claims_by_event_field[(claim.event_id, claim.field_name)].append(claim)

        conflicts: list[ClaimConflict] = []
        for (event_id, field_name), field_claims in claims_by_event_field.items():
            unique_values = {_make_hashable(normalize_value(c.field_value)) for c in field_claims}
            if len(unique_values) <= 1:
                continue

            # Cross-source policy: skip conflicts where all claims share the
            # same source.  One source contradicting itself is malformed input,
            # not a genuine cross-source disagreement.
            if self._require_multiple_sources:
                distinct_sources = {c.source_id for c in field_claims}
                if len(distinct_sources) < 2:
                    continue

            conflicts.append(
                ClaimConflict(
                    id=uuid4(),
                    event_id=event_id,
                    field_name=field_name,
                    status=ConflictStatus.OPEN,
                    version=1,
                    last_modified_reason=ConflictModifierReason.INITIAL,
                    claim_ids=[c.id for c in field_claims],
                )
            )
        return conflicts

    def _hashable(self, value: Any) -> Any:
        """Kept for backward compatibility; delegates to module-level helper."""
        return _make_hashable(value)
