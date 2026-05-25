from __future__ import annotations

from uuid import UUID

from atlas.application.unit_of_work import UnitOfWork
from atlas.domain.entities import ProjectedAccidentRecord
from atlas.domain.services.projection_builder import ProjectionBuilder


class VerifyProjectionConsistency:
    """Read-only projection consistency checker.

    Returns ``None`` when no stored projection exists and absence is not an
    expected state. Absorbed/merged events are special: the merge use case
    deletes their public projection immediately to avoid exposing stale accident
    records, while explicit reprojection may later recreate a minimal tombstone.
    Both states are valid and therefore return a successful payload.
    """

    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    async def execute(self, event_id: UUID) -> dict[str, object] | None:
        event = await self._uow.events.get(event_id)
        stored = await self._uow.projections.get(event_id)

        if event is not None and event.is_merged and stored is None:
            return {
                "status": "consistent",
                "event_id": str(event_id),
                "stored_version": None,
                "projection_absent_expected": True,
                "field_diff": {},
                "unresolved_conflict_fields_diff": {},
                "completeness_score_diff": {},
            }

        if stored is None:
            return None

        if event is not None and event.is_merged:
            recomputed = ProjectedAccidentRecord(
                event_id=event_id,
                projection_version=stored.projection_version,
                fields={
                    "is_merged": True,
                    "merged_into_event_id": str(event.merged_into_event_id),
                },
                completeness_score=0.0,
                unresolved_conflict_fields=[],
            )
        else:
            claims = await self._uow.claims.find_active_by_event(event_id)
            conflicts = await self._uow.conflicts.find_by_event(event_id)
            source_ids = list({claim.source_id for claim in claims})
            sources = await self._uow.sources.get_by_ids(source_ids) if source_ids else []
            sources_by_id = {source.id: source for source in sources}

            recomputed = ProjectionBuilder().build(
                event_id=event_id,
                claims=claims,
                conflicts=conflicts,
                sources_by_id=sources_by_id,
                projection_version=stored.projection_version,
            )

        fields_match = recomputed.fields == stored.fields
        conflicts_match = sorted(recomputed.unresolved_conflict_fields) == sorted(
            stored.unresolved_conflict_fields
        )
        completeness_match = (
            abs(float(recomputed.completeness_score) - float(stored.completeness_score)) < 1e-9
        )
        unresolved_conflict_fields_diff = (
            {
                "stored": sorted(stored.unresolved_conflict_fields),
                "recomputed": sorted(recomputed.unresolved_conflict_fields),
            }
            if not conflicts_match
            else {}
        )
        return {
            "status": "consistent"
            if fields_match and conflicts_match and completeness_match
            else "inconsistent",
            "event_id": str(event_id),
            "stored_version": stored.projection_version,
            "projection_absent_expected": False,
            "field_diff": {
                key: {"stored": stored.fields.get(key), "recomputed": recomputed.fields.get(key)}
                for key in set(stored.fields) | set(recomputed.fields)
                if stored.fields.get(key) != recomputed.fields.get(key)
            }
            if not fields_match
            else {},
            "unresolved_conflict_fields_diff": unresolved_conflict_fields_diff,
            "completeness_score_diff": {
                "stored": stored.completeness_score,
                "recomputed": recomputed.completeness_score,
            }
            if not completeness_match
            else {},
        }
