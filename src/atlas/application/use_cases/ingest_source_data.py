from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from atlas.application.dto import IngestionClaimDTO, IngestionResult
from atlas.application.ingestion import (
    ClaimWriter,
    ConflictReconciler,
    EventResolutionService,
    IdentityIndexUpdater,
    IngestionIdempotencyService,
    ProjectionUpdater,
    SourceRecordContinuityService,
)
from atlas.application.settings_protocol import IngestionSettings
from atlas.application.unit_of_work import UnitOfWork
from atlas.config import get_settings
from atlas.domain.entities import RawSnapshot
from atlas.domain.exceptions import (
    DomainValidationError,
    DuplicateClaimFieldError,
    EventAlreadyMergedError,
    EventNotFoundError,
    IdempotencyKeyPayloadMismatchError,
    IngestionInProgressError,
    PayloadTooLargeError,
    SourceNotFoundError,
    TooManyClaimsError,
)
from atlas.domain.services.ingestion import NormalizationError
from atlas.domain.utils import utc_now

logger = logging.getLogger(__name__)


def _canonical_json_bytes(value: Any, *, label: str) -> bytes:
    """Return stable JSON bytes, rejecting values that are not real JSON.

    API callers already send JSON, but use-case callers can pass Python-only
    objects such as Decimal, UUID, or datetime.  Do not hash those with
    ``default=str`` and then fail or mutate later when writing JSONB.
    """
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    except (TypeError, ValueError) as exc:
        raise DomainValidationError(f"{label} must be JSON-serializable") from exc


def _normalise_source_record_id(source_record_id: str | None) -> str | None:
    """Trim source record IDs; blank IDs are treated as missing.

    Source-specific case-folding is intentionally not applied here because some
    external systems have case-sensitive identifiers.  Whitespace, however, is
    never a meaningful identifier boundary for the continuity index.
    """
    if source_record_id is None:
        return None
    cleaned = str(source_record_id).strip()
    return cleaned or None


def _source_mapping_hash(field_mapping_json: dict[str, str] | None) -> str:
    """Hash durable source field-mapping config for audit provenance."""
    return hashlib.sha256(
        _canonical_json_bytes(field_mapping_json or {}, label="source field mapping")
    ).hexdigest()


def _claim_fingerprint_item(claim: IngestionClaimDTO) -> dict[str, Any]:
    """Return a deterministic JSON-serialisable representation of one claim."""
    return claim.model_dump(mode="json")


def _ingestion_submission_hash_material(
    *,
    raw_payload: dict[str, Any],
    claims_data: list[IngestionClaimDTO],
    source_record_id: str | None,
    event_id: UUID | None,
    captured_at: datetime | None,
) -> dict[str, Any]:
    """Return the full JSON material used for the idempotency hash.

    This includes ``raw_payload`` so changing the source object changes the
    submission hash.  Do not store this object directly on ``RawSnapshot``: the
    raw payload is already persisted in ``payload_json`` and can be large.
    """
    fingerprint = _ingestion_submission_fingerprint(
        raw_payload_hash=None,
        claims_data=claims_data,
        source_record_id=source_record_id,
        event_id=event_id,
        captured_at=captured_at,
        source_mapping_hash=None,
        normalizer_version=None,
    )
    # Keep idempotency stable across mapper/normalizer configuration changes:
    # the submission hash represents exactly what the client submitted, not
    # which server-side mapping version happened to process it.  Provenance is
    # stored on RawSnapshot.submission_fingerprint_json for audit, but it is not
    # part of the idempotency hash material.
    fingerprint.pop("source_mapping_hash", None)
    fingerprint.pop("normalizer_version", None)
    return {"raw_payload": raw_payload, **fingerprint}


def _ingestion_submission_fingerprint(
    *,
    raw_payload_hash: str | None,
    claims_data: list[IngestionClaimDTO],
    source_record_id: str | None,
    event_id: UUID | None,
    captured_at: datetime | None,
    source_mapping_hash: str | None = None,
    normalizer_version: str | None = "external-v1",
) -> dict[str, Any]:
    """Return durable, non-payload submission context for audit/replay.

    ``RawSnapshot.payload_json`` is already the authoritative raw payload.  This
    JSON stores the pieces that are not otherwise captured there, plus the raw
    payload hash, so operators can audit the full submission without duplicating
    a potentially large payload blob.
    """
    claims = [_claim_fingerprint_item(claim) for claim in claims_data]
    # Claim order in a request is not semantically meaningful once duplicate
    # canonical field names are rejected, so sort to keep the fingerprint stable
    # across clients that emit JSON arrays in different orders.
    claims.sort(
        key=lambda item: (
            str(item.get("field_name", "")),
            _canonical_json_bytes(item.get("field_value"), label="claim field_value").decode(),
        )
    )
    return {
        "schema_version": 1,
        "raw_payload_hash": raw_payload_hash,
        "claims_data": claims,
        "source_record_id": source_record_id,
        "event_id": str(event_id) if event_id else None,
        "captured_at": captured_at.isoformat() if captured_at else None,
        "source_mapping_hash": source_mapping_hash,
        "normalizer_version": normalizer_version,
    }


def _canonical_ingestion_submission(
    *,
    raw_payload: dict[str, Any],
    claims_data: list[IngestionClaimDTO],
    source_record_id: str | None,
    event_id: UUID | None,
    captured_at: datetime | None,
) -> bytes:
    """Canonical bytes for idempotency and use-case request-size checks.

    ``RawSnapshot.payload_hash`` historically stores this hash, so the database
    column name remains ``payload_hash`` for compatibility.  Semantically this
    is the full ingestion submission fingerprint, not just ``raw_payload``: the
    evidence written by ingestion is the raw source object plus extracted claims
    and routing hints. Reusing an idempotency key with different claims must be
    rejected, even if the raw payload object is unchanged.
    """
    return _canonical_json_bytes(
        _ingestion_submission_hash_material(
            raw_payload=raw_payload,
            claims_data=claims_data,
            source_record_id=source_record_id,
            event_id=event_id,
            captured_at=captured_at,
        ),
        label="ingestion submission",
    )


def _ingestion_result_json(result: IngestionResult) -> dict[str, Any]:
    """Serialize the completed use-case result for durable idempotent replay.

    ``event_id_at_completion`` is the audit value.  ``event_id`` is kept for
    backward compatibility with snapshots created while the persisted replay
    format was being introduced.  Replay canonicalizes this stored id before
    returning it to clients if the event has since been merged.
    """
    return {
        "schema_version": 1,
        "event_id_at_completion": str(result.event_id),
        "event_id": str(result.event_id),
        "event_created": result.event_created,
        "snapshot_created": result.snapshot_created,
        "pending_review_id": str(result.pending_review_id) if result.pending_review_id else None,
        "pending_review_ids": [str(review_id) for review_id in result.pending_review_ids],
        "attached_by": result.attached_by,
        "completed_at": utc_now().isoformat(),
    }


class IngestSourceData:
    """Top-level ingestion use case.

    Orchestrates the following collaborators in a single database transaction:

    1. ``IngestionIdempotencyService``   - short-circuit duplicate run submissions.
    2. ``SourceRecordContinuityService`` - resolve prior event for re-ingested
                                          source records (holds advisory lock).
    3. ``EventResolutionService``        - identity-index match -> attach/review/new.
    4. ``ClaimWriter``                   - normalise, write, supersede claims.
    5. ``ConflictReconciler``            - fix / create conflicts after claim writes.
    6. ``ProjectionUpdater``             - queue outbox event for async rebuild.
    7. ``IdentityIndexUpdater``          - synchronous identity substrate maintenance
                                          for explicit-event_id / continuity paths.
    """

    def __init__(self, uow: UnitOfWork, settings: IngestionSettings | None = None) -> None:
        self._uow = uow
        self._settings = settings

    @staticmethod
    def derive_ingestion_run_id(source_id: UUID, idempotency_key: str) -> UUID:
        """Deterministically derive an ingestion_run_id from a client key.

        The router calls this so that the same (source_id, idempotency_key) pair
        always maps to the same UUID. The idempotency guard rejects mismatched
        payloads for that run identity and returns the stored ingestion result
        for exact retries without writing new domain state. If the originally
        returned event was later merged, replay returns the current canonical
        event id.

        Using SHA-256 rather than UUID5 keeps the derivation simple and avoids
        the namespace collisions that UUID5 can produce with different source_ids
        when the key space is small.
        """
        digest = hashlib.sha256(f"{source_id}:{idempotency_key}".encode()).digest()
        return UUID(bytes=digest[:16])

    async def execute(
        self,
        source_id: UUID,
        raw_payload: dict[str, Any],
        ingestion_run_id: UUID,
        claims_data: list[IngestionClaimDTO],
        captured_at: datetime | None = None,
        event_id: UUID | None = None,
        source_record_id: str | None = None,
    ) -> UUID:
        """Ingest data and return only the canonical event id.

        Kept as the stable use-case API for existing callers/tests.  New callers
        that need truthful response metadata should use ``execute_with_result``.
        """
        result = await self.execute_with_result(
            source_id=source_id,
            raw_payload=raw_payload,
            ingestion_run_id=ingestion_run_id,
            claims_data=claims_data,
            captured_at=captured_at,
            event_id=event_id,
            source_record_id=source_record_id,
        )
        return result.event_id

    async def execute_with_result(
        self,
        source_id: UUID,
        raw_payload: dict[str, Any],
        ingestion_run_id: UUID,
        claims_data: list[IngestionClaimDTO],
        captured_at: datetime | None = None,
        event_id: UUID | None = None,
        source_record_id: str | None = None,
    ) -> IngestionResult:
        settings = self._settings or get_settings()
        source_record_id = _normalise_source_record_id(source_record_id)

        # Phase 0: validate payload size/shape, compute hashes.
        hashes = self._validate_and_hash(
            raw_payload, claims_data, source_record_id, event_id, captured_at, settings
        )

        # Phase 1: idempotency check — may return early with a replay.
        idempotency_svc = IngestionIdempotencyService(self._uow)
        early_result = await idempotency_svc.check(
            source_id,
            ingestion_run_id,
            submission_hash=hashes["submission_hash"],
            raw_payload_hash=hashes["raw_payload_hash"],
        )
        if early_result is not None:
            return early_result

        # Phase 2: load source, validate source + explicit event, normalise claims.
        # ``source`` itself is not used downstream — it's loaded and validated inside
        # ``_load_and_normalise`` for the source_mapping_hash and not needed again here.
        (
            _source,
            explicit_event,
            normalised_claims,
            normalised_claim_fields,
            submission_fingerprint,
        ) = await self._load_and_normalise(
            source_id, event_id, ingestion_run_id, claims_data, hashes, settings
        )

        logger.info(
            "Ingestion starting",
            extra={
                "source_id": str(source_id),
                "ingestion_run_id": str(ingestion_run_id),
                "event_id": str(event_id) if event_id else None,
                "claim_count": len(claims_data),
                "source_record_id": source_record_id,
            },
        )
        await self._uow.ingestion_runs.ensure_started(ingestion_run_id, source_id)

        existing_run = await self._uow.ingestion_runs.get(ingestion_run_id)
        if existing_run is not None and existing_run.source_id != source_id:
            raise DomainValidationError(
                f"Ingestion run {ingestion_run_id} already exists for source "
                f"{existing_run.source_id}, cannot reuse for source {source_id}"
            )

        # Phase 3: source-record continuity (advisory lock before snapshot insert).
        existing_event_from_record_id = None
        if source_record_id is not None:
            continuity_svc = SourceRecordContinuityService(self._uow)
            existing_event_from_record_id = await continuity_svc.resolve(
                source_id, source_record_id, explicit_event
            )

        # Phase 4: insert snapshot with idempotency guard.
        now = utc_now()
        snapshot_or_replay = await self._insert_snapshot(
            source_id,
            ingestion_run_id,
            raw_payload,
            captured_at,
            now,
            hashes,
            source_record_id,
            submission_fingerprint,
            idempotency_svc,
        )
        # _insert_snapshot may return an idempotent IngestionResult when a
        # concurrent race produces a completed snapshot before this path inserts.
        # Guard here so the caller never tries to access .id on an IngestionResult.
        if isinstance(snapshot_or_replay, IngestionResult):
            return snapshot_or_replay
        snapshot = snapshot_or_replay

        # Phase 5: event resolution (attach to existing or create new).
        (
            event,
            pending_reviews,
            event_created,
            attached_by,
            identity_index_maintained,
        ) = await self._resolve_event(
            source_id,
            event_id,
            explicit_event,
            existing_event_from_record_id,
            ingestion_run_id,
            source_record_id,
            normalised_claim_fields,
            settings,
        )

        # Phase 6: lock canonical event, update identity index if needed, write claims.
        claim_writer = ClaimWriter(self._uow)
        event, claim_result = await self._lock_and_write_claims(
            event,
            identity_index_maintained,
            normalised_claims,
            normalised_claim_fields,
            source_id,
            snapshot.id,
            ingestion_run_id,
            source_record_id,
            claim_writer,
        )

        # Phase 7: conflict reconciliation.
        reconciler = ConflictReconciler(self._uow)
        await reconciler.reconcile_superseded_winners(
            claim_result.resolved_conflicts_to_reconcile, ingestion_run_id
        )
        await reconciler.auto_resolve_stale_open_conflicts(
            event.id, claim_result.affected_fields, ingestion_run_id
        )
        await reconciler.detect_and_apply_new_conflicts(event.id, ingestion_run_id)

        # Phase 8: queue projection rebuild.
        await ProjectionUpdater(self._uow).queue(event.id, source_id, ingestion_run_id)

        # Commit and finalise.
        result = IngestionResult(
            event_id=event.id,
            event_created=event_created,
            snapshot_created=True,
            idempotent_replay=False,
            pending_review_id=pending_reviews[0].id if pending_reviews else None,
            pending_review_ids=tuple(review.id for review in pending_reviews),
            attached_by=attached_by,
        )
        await self._uow.snapshots.update_ingestion_result(
            snapshot.id, _ingestion_result_json(result)
        )
        await self._uow.ingestion_runs.update_status(
            ingestion_run_id, "finished", finished_at=utc_now()
        )
        await self._uow.commit()
        logger.info(
            "Ingestion complete",
            extra={
                "source_id": str(source_id),
                "ingestion_run_id": str(ingestion_run_id),
                "event_id": str(event.id),
                "pending_review_id": (
                    str(result.pending_review_id) if result.pending_review_id else None
                ),
                "pending_review_ids": [str(rid) for rid in result.pending_review_ids],
            },
        )
        return result

    # ── Private phase methods ─────────────────────────────────────────────────

    def _validate_and_hash(
        self,
        raw_payload: dict[str, Any],
        claims_data: list[IngestionClaimDTO],
        source_record_id: str | None,
        event_id: UUID | None,
        captured_at: datetime | None,
        settings: IngestionSettings,
    ) -> dict[str, Any]:
        """Validate payload shape/size and return hash dict.

        Raises ``DomainValidationError``, ``TooManyClaimsError``,
        ``PayloadTooLargeError``, or ``DuplicateClaimFieldError`` on bad input.
        All checks here run before any I/O so callers get clean rejection
        without orphaned database rows.
        """
        if not claims_data:
            raise DomainValidationError(
                "claims_data must not be empty: at least one claim is required per ingestion"
            )
        if len(claims_data) > settings.max_claims_per_request:
            raise TooManyClaimsError(
                f"Too many claims in request: {len(claims_data)} > {settings.max_claims_per_request}"
            )

        canonical_raw_payload = _canonical_json_bytes(raw_payload, label="raw_payload")
        raw_payload_hash = hashlib.sha256(canonical_raw_payload).hexdigest()
        payload_size = len(canonical_raw_payload)
        if payload_size > settings.max_raw_payload_bytes:
            raise PayloadTooLargeError(
                f"raw_payload is too large: {payload_size} bytes > {settings.max_raw_payload_bytes}"
            )

        submission_hash_material = _ingestion_submission_hash_material(
            raw_payload=raw_payload,
            claims_data=claims_data,
            source_record_id=source_record_id,
            event_id=event_id,
            captured_at=captured_at,
        )
        canonical_submission = _canonical_json_bytes(
            submission_hash_material, label="ingestion submission"
        )
        submission_size = len(canonical_submission)
        if submission_size > settings.max_raw_payload_bytes:
            raise PayloadTooLargeError(
                f"ingestion submission is too large: {submission_size} bytes > "
                f"{settings.max_raw_payload_bytes}"
            )
        submission_hash = hashlib.sha256(canonical_submission).hexdigest()

        seen_fields: set[str] = set()
        duplicate_fields: list[str] = []
        for claim in claims_data:
            if claim.field_name in seen_fields:
                duplicate_fields.append(claim.field_name)
            seen_fields.add(claim.field_name)
        if duplicate_fields:
            raise DuplicateClaimFieldError(
                f"Ingestion payload contains duplicate field_name entries: "
                f"{sorted(set(duplicate_fields))}. "
                "A single source may only assert one value per field per submission."
            )

        return {
            "raw_payload_hash": raw_payload_hash,
            "submission_hash": submission_hash,
            "source_record_id": source_record_id,
            "captured_at": captured_at,
        }

    async def _load_and_normalise(
        self,
        source_id: UUID,
        event_id: UUID | None,
        ingestion_run_id: UUID,
        claims_data: list[IngestionClaimDTO],
        hashes: dict[str, Any],
        settings: IngestionSettings,
    ) -> tuple[Any, Any | None, list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
        """Load the source + optional explicit event; normalise claims.

        Returns ``(source, explicit_event, normalised_claims,
        normalised_claim_fields, submission_fingerprint)``.
        Raises ``SourceNotFoundError``, ``EventNotFoundError``,
        ``EventAlreadyMergedError``, ``NormalizationError``, or
        ``DomainValidationError`` as appropriate.
        """
        source = await self._uow.sources.get(source_id)
        if source is None:
            raise SourceNotFoundError(f"Source {source_id} not found")

        source_mapping_hash = _source_mapping_hash(source.field_mapping_json)
        submission_fingerprint = _ingestion_submission_fingerprint(
            raw_payload_hash=hashes["raw_payload_hash"],
            claims_data=claims_data,
            source_record_id=hashes.get("source_record_id"),
            event_id=event_id,
            captured_at=hashes.get("captured_at"),
            source_mapping_hash=source_mapping_hash,
            normalizer_version=(
                "external-v1" if source.kind.value == "EXTERNAL" else "identity-v1"
            ),
        )

        explicit_event = None
        if event_id is not None:
            explicit_event = await self._uow.events.get(event_id)
            if explicit_event is None:
                raise EventNotFoundError(f"Event {event_id} not found")
            if explicit_event.is_merged:
                raise EventAlreadyMergedError(
                    f"Event {explicit_event.id} has been merged into "
                    f"{explicit_event.merged_into_event_id}; "
                    "ingest into the canonical event instead."
                )

        claim_writer = ClaimWriter(self._uow)
        try:
            normalised_claims = claim_writer.normalise_claims(
                source.kind.value,
                [c.model_dump(mode="json") for c in claims_data],
                source_id=source_id,
                ingestion_run_id=ingestion_run_id,
                source_field_mapping=source.field_mapping_json,
            )
        except NormalizationError:
            raise
        except ValueError as exc:
            raise DomainValidationError(
                f"Invalid Source.field_mapping_json for source {source_id}: {exc}"
            ) from exc

        normalised_claim_fields = {
            item["field_name"]: item.get("field_value") for item in normalised_claims
        }
        return (
            source,
            explicit_event,
            normalised_claims,
            normalised_claim_fields,
            submission_fingerprint,
        )

    async def _insert_snapshot(
        self,
        source_id: UUID,
        ingestion_run_id: UUID,
        raw_payload: dict[str, Any],
        captured_at: datetime | None,
        now: datetime,
        hashes: dict[str, Any],
        source_record_id: str | None,
        submission_fingerprint: dict[str, Any],
        idempotency_svc: IngestionIdempotencyService,
    ) -> RawSnapshot | IngestionResult:
        """Insert the RawSnapshot row, handling concurrent insert races."""
        snapshot = RawSnapshot(
            id=uuid4(),
            source_id=source_id,
            ingestion_run_id=ingestion_run_id,
            payload_hash=hashes["submission_hash"],
            raw_payload_hash=hashes["raw_payload_hash"],
            submission_hash=hashes["submission_hash"],
            submission_fingerprint_json=submission_fingerprint,
            payload_json=raw_payload,
            captured_at=captured_at or now,
            source_record_id=source_record_id,
        )
        inserted_snapshot = await self._uow.snapshots.try_add_unique(snapshot)
        if not inserted_snapshot:
            existing_snapshot = await self._uow.snapshots.find_by_source_run(
                source_id=source_id,
                ingestion_run_id=ingestion_run_id,
            )
            if existing_snapshot is not None:
                if not idempotency_svc.snapshot_hash_matches(
                    existing_snapshot,
                    submission_hash=hashes["submission_hash"],
                    raw_payload_hash=hashes["raw_payload_hash"],
                ):
                    raise IdempotencyKeyPayloadMismatchError(
                        "Idempotency key / ingestion run was reused with a different "
                        "ingestion submission. Retry with the original submission or "
                        "use a new idempotency key for a new ingestion."
                    )
                replay = await idempotency_svc.replay_from_snapshot(existing_snapshot)
                if replay is not None:
                    logger.info(
                        "Idempotent ingestion (snapshot guard): returning event %s",
                        replay.event_id,
                    )
                    return replay
            raise IngestionInProgressError(
                "Duplicate ingestion submission detected for the same source and ingestion run. "
                "The original ingestion is still in progress. Retry after the first ingestion commits."
            )
        return snapshot

    async def _resolve_event(
        self,
        source_id: UUID,
        event_id: UUID | None,
        explicit_event: Any,
        existing_event_from_record_id: Any,
        ingestion_run_id: UUID,
        source_record_id: str | None,
        normalised_claim_fields: dict[str, Any],
        settings: IngestionSettings,
    ) -> tuple[Any, list[Any], bool, str, bool]:
        """Return (event, pending_reviews, event_created, attached_by, identity_index_maintained)."""
        if explicit_event is not None:
            return explicit_event, [], False, "explicit_event_id", False
        if existing_event_from_record_id is not None:
            return existing_event_from_record_id, [], False, "source_record_id", False

        resolution_svc = EventResolutionService(self._uow)
        (
            event,
            pending_reviews,
            event_created,
            attached_by,
        ) = await resolution_svc.resolve_with_reviews(
            source_id=source_id,
            claims_data_fields=normalised_claim_fields,
            ingestion_run_id=ingestion_run_id,
            source_record_id=source_record_id,
            max_duplicate_reviews=settings.max_duplicate_reviews_per_ingestion,
        )
        return event, pending_reviews, event_created, attached_by, True

    async def _lock_and_write_claims(
        self,
        event: Any,
        identity_index_maintained: bool,
        normalised_claims: list[dict[str, Any]],
        normalised_claim_fields: dict[str, Any],
        source_id: UUID,
        snapshot_id: UUID,
        ingestion_run_id: UUID,
        source_record_id: str | None,
        claim_writer: Any,
    ) -> tuple[Any, Any]:
        """Lock the canonical event row, update identity index, write claims.

        Returns ``(locked_event, claim_result)``.  The row lock closes the merge
        x ingestion TOCTOU window.
        """
        locked_event = await self._uow.events.lock_for_update(event.id)
        if locked_event is None:
            raise EventNotFoundError(f"Event {event.id} not found")
        if locked_event.is_merged:
            raise EventAlreadyMergedError(
                f"Event {locked_event.id} has been merged into "
                f"{locked_event.merged_into_event_id}; "
                "ingest into the canonical event instead."
            )

        if not identity_index_maintained:
            await IdentityIndexUpdater(self._uow).update(
                locked_event.id, normalised_claim_fields, source_record_id
            )

        claim_result = await claim_writer.write_normalised(
            event_id=locked_event.id,
            source_id=source_id,
            snapshot_id=snapshot_id,
            normalised_claims=normalised_claims,
            ingestion_run_id=ingestion_run_id,
            source_record_id=source_record_id,
        )
        return locked_event, claim_result

    async def _canonical_event_for(self, event_id: UUID) -> object | None:
        """Kept for backward-compat with any callers; delegates to continuity service."""
        from atlas.application.ingestion._continuity import SourceRecordContinuityService

        svc = SourceRecordContinuityService(self._uow)
        return await svc._canonical_event_for(event_id)
