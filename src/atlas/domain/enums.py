from enum import StrEnum


class Role(StrEnum):
    """Canonical set of user roles for the Atlas API.

    All ``require_role()`` calls and bootstrap user creation must draw from
    this enum.  Arbitrary string roles are rejected at authentication time so
    a typo in a seed script cannot grant or silently deny access.

    Roles
    -----
    analyst  - read-only access (accidents, provenance, conflicts list/detail).
    reviewer - analyst + can resolve/reopen conflicts and action duplicate reviews.
    admin    - reviewer + destructive operations (merge, rebuild, outbox, metrics).
    """

    ANALYST = "analyst"
    REVIEWER = "reviewer"
    ADMIN = "admin"

    @classmethod
    def values(cls) -> frozenset[str]:
        return frozenset(r.value for r in cls)


class ClaimType(StrEnum):
    RAW = "RAW"
    CONFIRMED = "CONFIRMED"
    MANUAL_OVERRIDE = "MANUAL_OVERRIDE"
    SUPERSEDED = "SUPERSEDED"

    @classmethod
    def active_values(cls) -> frozenset[str]:
        """Single source of truth for active claim types used in DB filters."""
        return frozenset(claim_type.value for claim_type in cls if claim_type != cls.SUPERSEDED)


class ConflictStatus(StrEnum):
    OPEN = "OPEN"
    RESOLVED = "RESOLVED"


class SourceKind(StrEnum):
    EXTERNAL = "EXTERNAL"
    INTERNAL = "INTERNAL"


class ModifierType(StrEnum):
    USER = "USER"
    INGESTION = "INGESTION"
    SYSTEM = "SYSTEM"


class ConflictModifierReason(StrEnum):
    INITIAL = "INITIAL"
    NEW_EVIDENCE = "NEW_EVIDENCE"
    EVIDENCE_UPDATED = "EVIDENCE_UPDATED"
    USER_RESOLVED = "USER_RESOLVED"
    USER_REOPENED = "USER_REOPENED"
    SYSTEM_AUTO_CLOSED = "SYSTEM_AUTO_CLOSED"


class OutboxStatus(StrEnum):
    # NOTE: migration 007_uppercase_outbox_status.py uppercases existing DB rows.
    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    PROCESSED = "PROCESSED"
    FAILED = "FAILED"
    DEAD_LETTER = "DEAD_LETTER"


class DuplicateReviewStatus(StrEnum):
    """Lifecycle states for a PendingDuplicateReview.

    Lifecycle
    ---------
    PENDING
        The review was created by the identity-matching pipeline and is waiting
        for a curator decision.  This is the only mutable state - all other
        transitions are terminal.

    REJECTED
        A curator confirmed the two events are *distinct* accidents.  No merge
        is performed.  The pair will not re-surface as a duplicate unless a
        new ingestion triggers fresh identity matching.

    MERGED
        A curator confirmed the duplicate via the review UI and the merge use
        case ran successfully.  The source event is now marked
        ``merged_into_event_id = target``.

    AUTO_MERGED
        The pipeline merged the pair automatically on a high-confidence
        identity match (score above the auto-merge threshold), without waiting
        for curator review.

    Notes
    -----
    ``CONFIRMED_DUPLICATE`` is **retired**.  It was used briefly as an
    intermediate "confirmed but not yet merged" state.  The current flow skips
    that step: confirming a review immediately triggers a merge and transitions
    the status directly to ``MERGED``.  The value is kept in the enum to avoid
    a breaking migration on databases that may have legacy rows, but no new
    rows should ever be written with this status.
    """

    PENDING = "PENDING"  # awaiting curator decision
    REJECTED = "REJECTED"  # curator confirmed NOT a duplicate
    MERGED = "MERGED"  # manually merged via admin review action
    AUTO_MERGED = "AUTO_MERGED"  # system merged on high-confidence match
    # LEGACY - do not use for new rows.  See docstring above.
    CONFIRMED_DUPLICATE = "CONFIRMED_DUPLICATE"


class RequiredField(StrEnum):
    """Canonical set of fields for a complete accident record.

    ``ExternalSourceNormalizer`` uses these as its coercion keys.
    The old ``CanonicalField`` plain-class in ingestion.py was a duplicate
    of this enum and has been removed.
    """

    EVENT_DATE = "event_date"
    LOCATION = "location"
    AIRCRAFT_TYPE = "aircraft_type"
    FATALITIES_TOTAL = "fatalities_total"
    INJURIES_TOTAL = "injuries_total"
    OPERATOR = "operator"
    REGISTRATION = "registration"
    FLIGHT_PHASE = "flight_phase"
    NARRATIVE = "narrative"


class OrionEntityType(StrEnum):
    AIRCRAFT = "AIRCRAFT"
    OPERATOR = "OPERATOR"
    AIRPORT = "AIRPORT"
    AIRCRAFT_TYPE = "AIRCRAFT_TYPE"
    MANUFACTURER = "MANUFACTURER"
    INVESTIGATION_AGENCY = "INVESTIGATION_AGENCY"
    COUNTRY = "COUNTRY"


class OrionRelationshipType(StrEnum):
    INVOLVED_AIRCRAFT = "INVOLVED_AIRCRAFT"
    OPERATED_BY = "OPERATED_BY"
    AIRCRAFT_TYPE = "AIRCRAFT_TYPE"
    MANUFACTURED_BY = "MANUFACTURED_BY"
    OCCURRED_AT = "OCCURRED_AT"
    LOCATED_IN = "LOCATED_IN"
    INVESTIGATED_BY = "INVESTIGATED_BY"


class OrionReviewStatus(StrEnum):
    PENDING = "PENDING"
    MERGED = "MERGED"
    REJECTED = "REJECTED"
    AUTO_MERGED = "AUTO_MERGED"


class ChronosTimelineEventType(StrEnum):
    SCHEDULED_DEPARTURE = "SCHEDULED_DEPARTURE"
    ACTUAL_DEPARTURE = "ACTUAL_DEPARTURE"
    TAKEOFF = "TAKEOFF"
    LAST_CONTACT = "LAST_CONTACT"
    EMERGENCY_DECLARED = "EMERGENCY_DECLARED"
    IMPACT = "IMPACT"
    LANDING = "LANDING"
    RESCUE_STARTED = "RESCUE_STARTED"
    INVESTIGATION_OPENED = "INVESTIGATION_OPENED"
    REPORT_PUBLISHED = "REPORT_PUBLISHED"


class ChronosTimestampPrecision(StrEnum):
    EXACT = "EXACT"
    MINUTE = "MINUTE"
    HOUR = "HOUR"
    DAY = "DAY"
    APPROXIMATE = "APPROXIMATE"
    RELATIVE = "RELATIVE"
    UNKNOWN = "UNKNOWN"


class ChronosSequenceReviewStatus(StrEnum):
    PENDING = "PENDING"
    CONFIRMED = "CONFIRMED"
    REJECTED = "REJECTED"
    AUTO_CONFIRMED = "AUTO_CONFIRMED"


class HermesSourceType(StrEnum):
    OFFICIAL_AGENCY = "OFFICIAL_AGENCY"
    NEWS = "NEWS"
    DATABASE = "DATABASE"
    ARCHIVE = "ARCHIVE"
    OTHER = "OTHER"


class HermesTargetStatus(StrEnum):
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    DISABLED = "DISABLED"


class HermesFetchJobStatus(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class HermesDocumentContentType(StrEnum):
    HTML = "HTML"
    PDF = "PDF"
    TEXT = "TEXT"
    JSON = "JSON"
    XML = "XML"
    BINARY = "BINARY"
    UNKNOWN = "UNKNOWN"


class HermesChangeType(StrEnum):
    FIRST_SEEN = "FIRST_SEEN"
    CONTENT_CHANGED = "CONTENT_CHANGED"
    CONTENT_UNCHANGED = "CONTENT_UNCHANGED"
    FETCH_FAILED = "FETCH_FAILED"


# ── Argus Signal Detection Engine ────────────────────────────────────────────


class ArgusSignalType(StrEnum):
    NEW_SOURCE_CHANGE = "NEW_SOURCE_CHANGE"
    TIMELINE_SEQUENCE_CONFLICT = "TIMELINE_SEQUENCE_CONFLICT"
    HIGH_CONFLICT_ACCIDENT_RECORD = "HIGH_CONFLICT_ACCIDENT_RECORD"
    REPEATED_AIRCRAFT_INVOLVEMENT = "REPEATED_AIRCRAFT_INVOLVEMENT"
    REPEATED_OPERATOR_INVOLVEMENT = "REPEATED_OPERATOR_INVOLVEMENT"
    SOURCE_FETCH_FAILURE_SPIKE = "SOURCE_FETCH_FAILURE_SPIKE"
    ECHO_STRONG_PRECEDENT_MATCH = "ECHO_STRONG_PRECEDENT_MATCH"


class ArgusSeverity(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class ArgusSignalStatus(StrEnum):
    OPEN = "OPEN"
    CONFIRMED = "CONFIRMED"
    DISMISSED = "DISMISSED"
    NEEDS_MORE_REVIEW = "NEEDS_MORE_REVIEW"
    AUTO_RESOLVED = "AUTO_RESOLVED"


class ArgusEvidenceType(StrEnum):
    ATLAS_CLAIM = "ATLAS_CLAIM"
    ATLAS_CONFLICT = "ATLAS_CONFLICT"
    ATLAS_ACCIDENT_EVENT = "ATLAS_ACCIDENT_EVENT"
    ORION_ENTITY = "ORION_ENTITY"
    ORION_RELATIONSHIP = "ORION_RELATIONSHIP"
    CHRONOS_TIMELINE_EVENT = "CHRONOS_TIMELINE_EVENT"
    CHRONOS_SEQUENCE_REVIEW = "CHRONOS_SEQUENCE_REVIEW"
    HERMES_SOURCE_CHANGE = "HERMES_SOURCE_CHANGE"
    HERMES_FETCH_JOB = "HERMES_FETCH_JOB"
    HERMES_FETCHED_DOCUMENT = "HERMES_FETCHED_DOCUMENT"
    ECHO_CROSSREF_RESULT = "ECHO_CROSSREF_RESULT"


class ArgusReviewDecision(StrEnum):
    CONFIRMED = "CONFIRMED"
    DISMISSED = "DISMISSED"
    NEEDS_MORE_REVIEW = "NEEDS_MORE_REVIEW"
