"""Argus severity heuristics — deterministic, no ML."""

from __future__ import annotations

from atlas.domain.enums import ArgusSeverity, HermesChangeType

# Ordered low→high.  Used by ``upsert_signal`` to "ratchet" the severity of an
# existing signal whose underlying evidence has grown more serious (e.g. a
# fetch-failure spike that crossed the next threshold).  We never downgrade,
# because a curator may already have triaged the prior, lower severity.
_SEVERITY_RANK: dict[ArgusSeverity, int] = {
    ArgusSeverity.LOW: 0,
    ArgusSeverity.MEDIUM: 1,
    ArgusSeverity.HIGH: 2,
    ArgusSeverity.CRITICAL: 3,
}


def severity_rank(severity: ArgusSeverity) -> int:
    """Return a comparable rank for an ``ArgusSeverity``.

    Higher number → more severe.  Used by signal upserts to decide whether
    rising evidence should escalate an existing OPEN signal.
    """
    return _SEVERITY_RANK[severity]


def severity_for_chronos_sequence_conflict() -> tuple[ArgusSeverity, float]:
    return ArgusSeverity.MEDIUM, 0.95


def severity_for_hermes_source_change(change_type: HermesChangeType) -> tuple[ArgusSeverity, float]:
    match change_type:
        case HermesChangeType.FIRST_SEEN:
            return ArgusSeverity.LOW, 0.80
        case HermesChangeType.CONTENT_CHANGED:
            return ArgusSeverity.MEDIUM, 0.90
        case HermesChangeType.CONTENT_UNCHANGED:
            return ArgusSeverity.LOW, 0.60
        case HermesChangeType.FETCH_FAILED:
            return ArgusSeverity.LOW, 0.70
        case _:
            return ArgusSeverity.LOW, 0.50


def severity_for_hermes_fetch_failure_spike(count: int) -> tuple[ArgusSeverity, float]:
    if count >= 10:
        return ArgusSeverity.HIGH, 0.90
    if count >= 5:
        return ArgusSeverity.MEDIUM, 0.80
    return ArgusSeverity.LOW, 0.60


def severity_for_atlas_high_conflict(open_conflict_count: int) -> tuple[ArgusSeverity, float]:
    """Severity bands for ``HIGH_CONFLICT_ACCIDENT_RECORD`` signals.

    An accident event with many simultaneously OPEN conflicts is a strong cue
    that something structural is wrong — either a noisy/contested source, a
    misconfigured mapping, or a legitimately ambiguous event that needs
    curator attention.  We escalate proportionally to the number of conflicts:

    - ``>= 20`` open: CRITICAL — almost certainly a data quality emergency
    - ``>= 10`` open: HIGH — needs prompt curator review
    - ``>= 5``  open: MEDIUM — worth triaging
    - else:           LOW — surface in queues but don't alert

    Confidence is high because the underlying signal is just a count of
    persisted DB rows — no inference involved.
    """
    if open_conflict_count >= 20:
        return ArgusSeverity.CRITICAL, 0.95
    if open_conflict_count >= 10:
        return ArgusSeverity.HIGH, 0.90
    if open_conflict_count >= 5:
        return ArgusSeverity.MEDIUM, 0.85
    return ArgusSeverity.LOW, 0.75
