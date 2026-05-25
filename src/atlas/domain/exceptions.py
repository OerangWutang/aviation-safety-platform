"""Domain exception hierarchy."""

from __future__ import annotations

from uuid import UUID


class AtlasError(Exception):
    code: str = "ATLAS_ERROR"

    def __init__(self, message: object = "") -> None:
        self.message = str(message)
        super().__init__(self.message)

    def __str__(self) -> str:
        return f"[{self.code}] {self.message}"


# Backwards-compatible base name used by earlier code/tests.
AtlasDomainError = AtlasError


class MappingError(AtlasError):
    """Raised when an ORM model cannot be mapped to a domain entity."""

    code = "MAPPING_ERROR"


class IngestionInProgressError(AtlasError):
    """Raised when a duplicate ingestion is detected before the first commit is visible."""

    code = "INGESTION_IN_PROGRESS"


class IdempotencyKeyPayloadMismatchError(AtlasError):
    """Raised when an idempotency key/run is reused with different submission bytes."""

    code = "IDEMPOTENCY_KEY_PAYLOAD_MISMATCH"


class NotFoundError(AtlasError):
    code = "NOT_FOUND"


class SourceNotFoundError(NotFoundError):
    code = "SOURCE_NOT_FOUND"


class EventNotFoundError(NotFoundError):
    code = "EVENT_NOT_FOUND"


class ConflictNotFoundError(NotFoundError):
    code = "CONFLICT_NOT_FOUND"


class ClaimNotFoundError(NotFoundError):
    code = "CLAIM_NOT_FOUND"


class ArgusSignalNotFoundError(NotFoundError):
    code = "ARGUS_SIGNAL_NOT_FOUND"


class DomainValidationError(AtlasError):
    code = "DOMAIN_VALIDATION_ERROR"


class PayloadTooLargeError(DomainValidationError):
    code = "PAYLOAD_TOO_LARGE"


class TooManyClaimsError(DomainValidationError):
    code = "TOO_MANY_CLAIMS"


class ConflictAlreadyResolvedError(AtlasError):
    code = "CONFLICT_ALREADY_RESOLVED"


class ClaimNotInConflictError(AtlasError):
    code = "CLAIM_NOT_IN_CONFLICT"


class ConflictModifiedError(AtlasError):
    code = "CONFLICT_MODIFIED"

    def __init__(
        self,
        conflict_id: UUID,
        current_version: int,
        current_conflict=None,
        current_projection=None,
        latest_activity=None,
        modifier_reason=None,
    ) -> None:
        self.conflict_id = conflict_id
        self.current_version = current_version
        self.current_conflict = current_conflict
        self.current_projection = current_projection
        self.latest_activity = latest_activity
        self.modifier_reason = modifier_reason
        super().__init__(f"Conflict {conflict_id} modified (v{current_version})")


class ArgusSignalModifiedError(AtlasError):
    """Raised when a reviewer's ``expected_version`` no longer matches the
    persisted ``ArgusSignal.version`` — i.e. someone else has already reviewed
    or updated the signal since the client loaded it.

    The response surfaces ``current_version`` plus the current signal so the
    client can re-render with the latest state and let the reviewer decide
    whether to retry.  Mirrors the shape of ``ConflictModifiedError`` but
    omits projection / activity-log payload (Argus signals don't have those).
    """

    code = "ARGUS_SIGNAL_MODIFIED"

    def __init__(
        self,
        signal_id: UUID,
        current_version: int,
        current_signal=None,
    ) -> None:
        self.signal_id = signal_id
        self.current_version = current_version
        self.current_signal = current_signal
        super().__init__(f"Argus signal {signal_id} modified (v{current_version})")


class InvalidManualOverrideError(DomainValidationError):
    code = "INVALID_MANUAL_OVERRIDE"


class ClaimNotEligibleError(DomainValidationError):
    """Raised when an existing claim is not eligible to be selected as winner."""

    code = "CLAIM_NOT_ELIGIBLE"


class NoWinningClaimError(DomainValidationError):
    code = "NO_WINNING_CLAIM"


class EventAlreadyMergedError(AtlasError):
    code = "EVENT_ALREADY_MERGED"


class SourceRecordEventMismatchError(AtlasError):
    """Raised when a stable source record is explicitly pointed at the wrong event."""

    code = "SOURCE_RECORD_EVENT_MISMATCH"


class CannotMergeIntoSelfError(AtlasError):
    code = "CANNOT_MERGE_INTO_SELF"


class DuplicateClaimFieldError(DomainValidationError):
    """Raised when a single ingestion payload contains two claims for the same field.

    One source cannot coherently assert two different values for the same field
    in a single submission.  The caller must either de-duplicate the claims
    before submitting or submit separate ingestion runs.
    """

    code = "DUPLICATE_CLAIM_FIELD"


class ReviewNotFoundError(NotFoundError):
    code = "REVIEW_NOT_FOUND"


class ReviewAlreadyResolvedError(AtlasError):
    code = "REVIEW_ALREADY_RESOLVED"


class ConflictReconciliationError(AtlasError):
    """Raised when conflict reconciliation exhausts its optimistic retry budget."""

    code = "CONFLICT_RECONCILIATION_FAILED"

    def __init__(self, conflict_id: UUID, operation: str, retries: int) -> None:
        self.conflict_id = conflict_id
        self.operation = operation
        self.retries = retries
        super().__init__(f"Conflict {conflict_id}: {operation} failed after {retries} retries")


class PersistenceCorruptionError(AtlasError):
    """Raised when persisted data fails to deserialize or violates internal invariants.

    This is an internal/server error (5xx), not a client error. It means
    previously-committed data is malformed or structurally inconsistent.
    Callers should not map this to a 400 response.
    """

    code = "PERSISTENCE_CORRUPTION"


class InvariantViolationError(AtlasError):
    """Raised when a domain invariant that should be guaranteed by the system is broken.

    Like PersistenceCorruptionError, this indicates a server-side bug or data
    integrity problem, not a client input error.
    """

    code = "INVARIANT_VIOLATION"


class IngestionRunSourceMismatchError(DomainValidationError):
    """Raised when an ingestion run id already belongs to another source."""

    code = "INGESTION_RUN_SOURCE_MISMATCH"

    def __init__(
        self,
        run_id: UUID,
        expected_source_id: UUID,
        actual_source_id: UUID,
    ) -> None:
        self.run_id = run_id
        self.expected_source_id = expected_source_id
        self.actual_source_id = actual_source_id
        super().__init__(
            f"IngestionRun {run_id} already exists for source "
            f"{actual_source_id}, not {expected_source_id}"
        )


class ConcurrentUpsertError(AtlasError):
    """Raised when an ``ON CONFLICT DO NOTHING`` upsert fires but the
    re-select immediately afterwards returns no row.

    This indicates a concurrent DELETE or a partial-index discrepancy that
    occurred between the INSERT attempt and the re-select — i.e. the
    conflicting row was removed in the narrow window between the two
    operations.

    Callers should typically retry the whole unit of work once (the
    conflicting row may have been replaced by a new insert already).  The
    exception code ``CONCURRENT_UPSERT_RETRY`` is surfaced via the global
    handler as a 503 Service Unavailable so clients can apply backoff.

    Unlike ``RuntimeError``, this class is a first-class ``AtlasError`` so
    the global exception handler can map it to a clean HTTP response rather
    than a generic 500.
    """

    code = "CONCURRENT_UPSERT_RETRY"

    def __init__(self, message: str) -> None:
        super().__init__(message)
