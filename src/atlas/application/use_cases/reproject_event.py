from __future__ import annotations

import hashlib
import json
import logging
from typing import Any
from uuid import UUID, uuid4

from atlas.application.dto import ProjectionDTO
from atlas.application.unit_of_work import UnitOfWork
from atlas.domain.constants import replace_disputed
from atlas.domain.entities import AccidentProjectionHistory, ProjectedAccidentRecord
from atlas.domain.exceptions import EventNotFoundError, InvariantViolationError
from atlas.domain.services.projection_builder import ProjectionBuilder
from atlas.domain.utils import utc_now

logger = logging.getLogger(__name__)


def _canonical_json_bytes(value: Any, *, label: str) -> bytes:
    """Return stable JSON bytes for projection audit hashes.

    Projection snapshots are persisted as JSONB, so the audit hash must be based
    only on values that are genuinely JSON-serializable.  Avoid ``default=str``:
    it can silently turn UUIDs, datetimes, Decimal values, or NaN into strings
    that differ from the stored JSONB semantics.
    """
    try:
        return json.dumps(
            replace_disputed(value),
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
    except (TypeError, ValueError) as exc:
        raise InvariantViolationError(f"{label} must be JSON-serializable") from exc


def _projection_content_hash(
    *,
    fields: dict[str, object],
    unresolved_conflict_fields: list[str],
    completeness_score: float,
) -> str:
    """Hash projected content, intentionally excluding projection_version."""
    content_snapshot = {
        "fields": fields,
        "unresolved_conflict_fields": unresolved_conflict_fields,
        "completeness_score": completeness_score,
    }
    return hashlib.sha256(
        _canonical_json_bytes(content_snapshot, label="projection content")
    ).hexdigest()


def _changed_fields(
    current_proj: ProjectedAccidentRecord | None,
    projection: ProjectedAccidentRecord,
) -> list[str]:
    if current_proj is None:
        return sorted(projection.fields.keys())

    changed = [
        field
        for field in sorted(set(current_proj.fields) | set(projection.fields))
        if current_proj.fields.get(field) != projection.fields.get(field)
    ]
    if current_proj.unresolved_conflict_fields != projection.unresolved_conflict_fields:
        changed.append("unresolved_conflict_fields")
    if current_proj.completeness_score != projection.completeness_score:
        changed.append("completeness_score")
    return changed


class ReProjectEvent:
    """Reproject a single event from its current active claims and conflicts.

    Audit semantics
    ---------------
    Projection history records actual *content* changes to the projected
    accident record. A no-op reprojection (same field values and dispute
    markers as the current row) intentionally skips writing a new
    projection-history row, because no observable data changed.

    Outbox events record processing/delivery state: the system received and
    attempted to propagate a message. Operators should therefore not expect a
    1:1 ratio of processed outbox rows to projection-history rows. High outbox
    counts with few projection-history rows is normal under steady-state
    re-ingestion with no value changes.

    Merged events
    -------------
    A merged/absorbed event must never be rebuilt into a stale public accident
    record.  When reprojection is explicitly requested for a merged event, the
    read model is replaced with a minimal tombstone that points at the canonical
    survivor. The merge use case also deletes the absorbed event's old projection
    immediately so direct projection reads do not expose stale data between the
    merge and the next maintenance rebuild.
    """

    def __init__(self, uow: UnitOfWork):
        self._uow = uow

    async def execute(
        self,
        event_id: UUID,
        caused_by_conflict_id: UUID | None = None,
        caused_by_ingestion_run_id: UUID | None = None,
        caused_by_outbox_event_id: UUID | None = None,
        commit: bool = True,
        force_history: bool = False,
    ) -> ProjectionDTO:
        # Serialize concurrent reprojections of the same event. Two workers
        # racing here would both compute ``current_proj.projection_version + 1``
        # and produce the same version number, hitting the
        # ``uq_projection_history_version`` unique index. The advisory lock is
        # held only for this transaction; reprojections of different events
        # do not contend.
        await self._uow.events.lock_for_reprojection(event_id)

        event = await self._uow.events.get(event_id)
        current_proj = await self._uow.projections.get(event_id)

        if caused_by_outbox_event_id:
            existing = await self._uow.projection_history.find_by_outbox_event(
                caused_by_outbox_event_id
            )
            if existing and (event is None or not event.is_merged):
                if event is None:
                    # The accident_events row no longer exists (deleted, or the
                    # outbox id refers to a stale event).  We honour the outbox
                    # idempotency contract and return/restore the existing
                    # projection rather than raising, because the outbox worker
                    # needs a successful acknowledgement to stop retrying.  Log a
                    # prominent warning so operators can investigate the data gap.
                    logger.warning(
                        "reproject: AccidentEvent %s not found but outbox history %s "
                        "exists — returning idempotent projection without rebuilding.  "
                        "This may indicate a deleted or migrated event; verify data consistency.",
                        event_id,
                        existing.id,
                    )
                if current_proj is None:
                    logger.warning(
                        "Projection row missing for event=%s despite outbox history=%s; "
                        "restoring projection row from history snapshot.",
                        event_id,
                        existing.id,
                    )
                    restored = await self._restore_projection_from_history(
                        event_id=event_id,
                        history=existing,
                        commit=commit,
                    )
                    return ProjectionDTO(**restored.model_dump())
                return ProjectionDTO(**current_proj.model_dump())
            if existing and event is not None and event.is_merged:
                # A pre-merge outbox/history row must not resurrect an absorbed
                # event's old public projection.  Rebuild the merged tombstone
                # below, but do not reuse the already-consumed outbox id for a
                # second projection-history row.
                logger.info(
                    "Ignoring existing projection history %s for merged event %s; "
                    "ensuring tombstone projection instead.",
                    existing.id,
                    event_id,
                )
                caused_by_outbox_event_id = None

        if event is not None and event.is_merged:
            next_version = (current_proj.projection_version + 1) if current_proj else 1
            projection = ProjectedAccidentRecord(
                event_id=event_id,
                projection_version=next_version,
                fields={
                    "is_merged": True,
                    "merged_into_event_id": str(event.merged_into_event_id),
                },
                completeness_score=0.0,
                unresolved_conflict_fields=[],
                updated_at=utc_now(),
            )
            return await self._persist_projection(
                projection=projection,
                current_proj=current_proj,
                caused_by_conflict_id=caused_by_conflict_id,
                caused_by_ingestion_run_id=caused_by_ingestion_run_id,
                caused_by_outbox_event_id=caused_by_outbox_event_id,
                commit=commit,
                force_history=force_history,
            )

        claims = await self._uow.claims.find_active_by_event(event_id)
        conflicts = await self._uow.conflicts.find_by_event(event_id)

        # Guard: if the event row genuinely doesn't exist (not merged — that
        # branch is handled above), raise a typed 404 rather than silently
        # building an empty projection whose FK would later blow up at the DB
        # layer.  This can happen when the outbox fires for an already-deleted
        # event or when reprojection is called with a bogus id.
        #
        # We also check ``not claims`` because strict FK constraints on
        # claims → accident_events make orphan claims impossible in a
        # consistent database.  If the DB is consistent, event=None implies
        # no claims.  If the DB is inconsistent (e.g. FK constraints were
        # temporarily disabled during a migration), we still attempt to
        # reproject rather than silently dropping the request — the resulting
        # projection's event_id FK will fail at persist time, which is a
        # cleaner failure mode than a silent 404.
        if event is None and not claims:
            raise EventNotFoundError(f"AccidentEvent {event_id} not found; cannot reproject.")

        # Batch-fetch sources to avoid N+1 round-trips. ``get_by_ids`` returns
        # the empty list for an empty input, so the gating check is cheap.
        source_ids = list({claim.source_id for claim in claims})
        sources = await self._uow.sources.get_by_ids(source_ids) if source_ids else []
        sources_by_id = {source.id: source for source in sources}

        next_version = (current_proj.projection_version + 1) if current_proj else 1

        projection = ProjectionBuilder().build(
            event_id=event_id,
            claims=claims,
            conflicts=conflicts,
            sources_by_id=sources_by_id,
            projection_version=next_version,
        )

        return await self._persist_projection(
            projection=projection,
            current_proj=current_proj,
            caused_by_conflict_id=caused_by_conflict_id,
            caused_by_ingestion_run_id=caused_by_ingestion_run_id,
            caused_by_outbox_event_id=caused_by_outbox_event_id,
            commit=commit,
            force_history=force_history,
        )

    async def _restore_projection_from_history(
        self,
        *,
        event_id: UUID,
        history: AccidentProjectionHistory,
        commit: bool,
    ) -> ProjectedAccidentRecord:
        """Restore the current projection row from an idempotency history row.

        The outbox idempotency path may find an existing history row while the
        current read-model row is missing due to manual repair, corruption, or a
        failed restore. In that case, do not run a fresh rebuild with the same
        ``caused_by_outbox_event_id``: that would violate the unique outbox-event
        history constraint. Instead, recreate the projection row from the exact
        audited snapshot that made the outbox event idempotent.
        """
        if history.accident_event_id != event_id:
            raise RuntimeError(
                "Projection history event mismatch: "
                f"history={history.accident_event_id} requested={event_id}"
            )

        snapshot: dict[str, Any] = history.projected_record_snapshot
        projection = ProjectedAccidentRecord(
            event_id=event_id,
            projection_version=history.projection_version,
            fields=dict(snapshot.get("fields") or {}),
            unresolved_conflict_fields=list(snapshot.get("unresolved_conflict_fields") or []),
            completeness_score=float(snapshot.get("completeness_score") or 0.0),
            updated_at=utc_now(),
        )
        await self._uow.projections.upsert(projection)
        if commit:
            await self._uow.commit()
        return projection

    async def _persist_projection(
        self,
        *,
        projection: ProjectedAccidentRecord,
        current_proj: ProjectedAccidentRecord | None,
        caused_by_conflict_id: UUID | None,
        caused_by_ingestion_run_id: UUID | None,
        caused_by_outbox_event_id: UUID | None,
        commit: bool,
        force_history: bool,
    ) -> ProjectionDTO:
        content_snapshot = {
            "fields": projection.fields,
            "unresolved_conflict_fields": projection.unresolved_conflict_fields,
            "completeness_score": projection.completeness_score,
        }
        content_hash = _projection_content_hash(
            fields=projection.fields,
            unresolved_conflict_fields=projection.unresolved_conflict_fields,
            completeness_score=projection.completeness_score,
        )

        # Skip no-op writes by default. This keeps projection history focused on
        # content changes rather than repeated rebuild deliveries. If callers
        # need an explicit audit marker even for unchanged content, they can pass
        # force_history=True and accept the resulting version bump.
        if current_proj is not None and not force_history:
            current_content_hash = _projection_content_hash(
                fields=current_proj.fields,
                unresolved_conflict_fields=current_proj.unresolved_conflict_fields,
                completeness_score=current_proj.completeness_score,
            )
            if content_hash == current_content_hash:
                if commit:
                    await self._uow.commit()
                return ProjectionDTO(**current_proj.model_dump())

        await self._uow.projections.upsert(projection)

        versioned_snapshot = {**content_snapshot, "version": projection.projection_version}
        snapshot_hash = hashlib.sha256(
            _canonical_json_bytes(versioned_snapshot, label="projection history snapshot")
        ).hexdigest()

        history = AccidentProjectionHistory(
            id=uuid4(),
            accident_event_id=projection.event_id,
            projection_version=projection.projection_version,
            caused_by_conflict_id=caused_by_conflict_id,
            caused_by_ingestion_run_id=caused_by_ingestion_run_id,
            caused_by_outbox_event_id=caused_by_outbox_event_id,
            projected_record_snapshot=versioned_snapshot,
            projected_record_hash=snapshot_hash,
            changed_fields=_changed_fields(current_proj, projection),
            created_at=utc_now(),
        )
        await self._uow.projection_history.add(history)

        if commit:
            await self._uow.commit()

        return ProjectionDTO(**projection.model_dump())
