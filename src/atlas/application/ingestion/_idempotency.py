"""IngestionIdempotencyService - guard against duplicate run submissions."""

from __future__ import annotations

import logging
from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictBool, ValidationError

from atlas.application.dto import IngestionResult
from atlas.application.unit_of_work import UnitOfWork
from atlas.domain.entities import AccidentEvent, RawSnapshot
from atlas.domain.exceptions import (
    EventAlreadyMergedError,
    EventNotFoundError,
    IdempotencyKeyPayloadMismatchError,
    IngestionInProgressError,
    PersistenceCorruptionError,
)

logger = logging.getLogger(__name__)


class StoredIngestionResult(BaseModel):
    """Durable JSON schema for idempotent replay results."""

    model_config = ConfigDict(extra="ignore")

    schema_version: int = 1
    event_id_at_completion: UUID | None = None
    event_id: UUID | None = None
    event_created: StrictBool = False
    snapshot_created: StrictBool = True
    pending_review_id: UUID | None = None
    pending_review_ids: list[UUID] = Field(default_factory=list)
    attached_by: str = "idempotent_replay"
    completed_at: datetime | None = None

    @property
    def replay_seed_event_id(self) -> UUID:
        seed = self.event_id_at_completion or self.event_id
        if seed is None:
            raise ValueError("Stored ingestion result is missing event_id_at_completion/event_id")
        return seed


class IngestionIdempotencyService:
    """Detect and short-circuit duplicate ingestion submissions.

    Given a ``(source_id, ingestion_run_id, submission_hash)`` triple, checks
    whether this exact run has already been persisted and, if so, returns an
    ``IngestionResult`` representing the idempotent replay - allowing the
    caller to return early without writing anything new.

    Replays prefer the durable ``RawSnapshot.ingestion_result_json`` written at
    the end of the original successful ingestion.  Falling back to scanning
    claims is kept only for rows created before that column existed.

    Raises
    ------
    IdempotencyKeyPayloadMismatchError
        If the same run identity is reused with a *different* submission hash.
    IngestionInProgressError
        If the snapshot exists but no completed result/claims are committed yet
        (race window or crashed writer).
    """

    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    async def check(
        self,
        source_id: UUID,
        ingestion_run_id: UUID,
        submission_hash: str,
        raw_payload_hash: str,
    ) -> IngestionResult | None:
        """Return a completed ``IngestionResult`` if this is a duplicate, else ``None``."""

        existing_for_run = await self._uow.snapshots.find_by_source_run(source_id, ingestion_run_id)
        if existing_for_run is None:
            return None

        if not self.snapshot_hash_matches(
            existing_for_run,
            submission_hash=submission_hash,
            raw_payload_hash=raw_payload_hash,
        ):
            raise IdempotencyKeyPayloadMismatchError(
                "Idempotency key / ingestion run was reused with a different ingestion submission. "
                "Retry with the original submission or use a new idempotency key for a new ingestion."
            )

        replay = await self._result_from_snapshot(existing_for_run)
        if replay is not None:
            logger.info(
                "Idempotent ingestion (run guard): returning event %s from stored result",
                replay.event_id,
            )
            return replay

        raise IngestionInProgressError(
            "Duplicate ingestion submission detected for the same source and ingestion run. "
            "The original ingestion is still in progress. Retry after the first ingestion commits."
        )

    def snapshot_hash_matches(
        self,
        snapshot: RawSnapshot,
        *,
        submission_hash: str,
        raw_payload_hash: str,
    ) -> bool:
        """Return whether ``snapshot`` represents this submission.

        Modern rows compare the explicit full-submission hash.  Legacy rows
        from before submission fingerprints existed may have stored only the
        raw-payload hash in ``payload_hash``; those are accepted only when the
        new audit columns are absent, preserving replay compatibility for old
        data without weakening new idempotency semantics.
        """
        stored_submission_hash = snapshot.submission_hash or snapshot.payload_hash
        if stored_submission_hash == submission_hash:
            return True

        is_legacy_raw_payload_only = (
            snapshot.submission_fingerprint_json is None
            and snapshot.raw_payload_hash is None
            and stored_submission_hash == raw_payload_hash
        )
        if is_legacy_raw_payload_only:
            logger.info(
                "Accepting legacy idempotency replay where payload_hash stores raw_payload hash",
                extra={
                    "snapshot_id": str(snapshot.id),
                    "source_id": str(snapshot.source_id),
                    "ingestion_run_id": str(snapshot.ingestion_run_id),
                    "reason": "legacy_raw_payload_hash_only",
                },
            )
            return True
        return False

    async def replay_from_snapshot(self, snapshot: RawSnapshot) -> IngestionResult | None:
        """Return replay result for a snapshot already selected by a race guard."""
        return await self._result_from_snapshot(snapshot)

    async def _result_from_snapshot(self, snapshot: RawSnapshot) -> IngestionResult | None:
        if snapshot.ingestion_result_json:
            return await self._result_from_json(snapshot.ingestion_result_json)

        # Backward-compat fallback for old rows.  This cannot recover
        # pending_review_id(s)/attached_by exactly, so all new successful ingestions
        # must write ingestion_result_json before commit.
        existing_event_id = await self._uow.claims.find_event_id_by_raw_snapshot_id(snapshot.id)
        if existing_event_id is None:
            return None
        return IngestionResult(
            event_id=await self._canonical_event_id(existing_event_id),
            event_created=False,
            snapshot_created=False,
            idempotent_replay=True,
            attached_by="idempotent_replay_legacy_claim_lookup",
        )

    async def _result_from_json(self, data: dict[str, object]) -> IngestionResult:
        # New rows store event_id_at_completion explicitly; older rows stored
        # event_id only.  The response returns the current canonical event id,
        # but the persisted JSON remains an audit record of the original result.
        try:
            stored = StoredIngestionResult.model_validate(data)
        except (ValidationError, ValueError) as exc:
            raise PersistenceCorruptionError(
                "Malformed stored ingestion_result_json: persisted result could not be "
                "deserialized. This is an internal data-integrity problem, not a client error."
            ) from exc
        if stored.schema_version != 1:
            raise PersistenceCorruptionError(
                f"Unsupported ingestion_result_json schema_version {stored.schema_version}. "
                "The stored record was written by a newer version of Atlas. "
                "This is an internal data-integrity problem, not a client error."
            )
        try:
            seed_event_id = stored.replay_seed_event_id
        except ValueError as exc:
            raise PersistenceCorruptionError(
                "Malformed stored ingestion_result_json: missing event id "
                "(event_id_at_completion and event_id are both absent). "
                "This is an internal data-integrity problem, not a client error."
            ) from exc
        event_id = await self._canonical_event_id(seed_event_id)
        pending_review_ids = list(dict.fromkeys(stored.pending_review_ids))
        if stored.pending_review_id and stored.pending_review_id not in pending_review_ids:
            pending_review_ids.insert(0, stored.pending_review_id)
        return IngestionResult(
            event_id=event_id,
            event_created=stored.event_created,
            # No new snapshot is created by a replay, but the original result is
            # still used for all durable domain metadata.
            snapshot_created=False,
            idempotent_replay=True,
            pending_review_id=stored.pending_review_id,
            pending_review_ids=tuple(pending_review_ids),
            attached_by=stored.attached_by or "idempotent_replay",
        )

    async def _canonical_event_id(self, event_id: UUID) -> UUID:
        """Return the current canonical event id, following merge pointers.

        Idempotent replay should not infer from copied/superseded claims after a
        merge.  If the originally returned event has since been merged, returning
        the current canonical id is more useful to clients while the stored JSON
        still preserves the original completed result for audit.
        """
        seen: set[UUID] = set()
        current_id = event_id
        while True:
            event: AccidentEvent | None = await self._uow.events.get(current_id)
            if event is None:
                raise EventNotFoundError(
                    f"Stored ingestion result references missing event {current_id}"
                )
            if not event.is_merged:
                return current_id
            if event.merged_into_event_id is None:
                raise EventAlreadyMergedError(
                    f"Event {event.id} is marked merged but has no canonical target"
                )
            if event.id in seen:
                raise EventAlreadyMergedError(
                    f"Cycle detected while canonicalizing replay event {event_id}"
                )
            seen.add(event.id)
            current_id = event.merged_into_event_id
