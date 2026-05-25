"""Shared helpers for conflict selection and reconciliation."""

from __future__ import annotations

from atlas.domain.entities import ClaimConflict
from atlas.domain.enums import ConflictStatus


def latest_resolved_conflicts_by_field(
    conflicts: list[ClaimConflict],
) -> dict[str, ClaimConflict]:
    """Return the authoritative RESOLVED conflict per field.

    Ordering key: ``(version, updated_at)``. The highest version wins; when two
    rows share a version, the latest ``updated_at`` wins. This avoids relying on
    repository ordering and keeps projection building and conflict reopening in
    agreement.
    """
    resolved: dict[str, ClaimConflict] = {}
    for conflict in conflicts:
        if conflict.status != ConflictStatus.RESOLVED:
            continue
        existing = resolved.get(conflict.field_name)
        if existing is None or (conflict.version, conflict.updated_at) > (
            existing.version,
            existing.updated_at,
        ):
            resolved[conflict.field_name] = conflict
    return resolved
