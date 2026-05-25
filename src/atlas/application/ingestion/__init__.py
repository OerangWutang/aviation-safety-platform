"""Ingestion collaborator services.

These are extracted from the monolithic ``IngestSourceData.execute_with_result``
to make each concern independently testable and to reduce the cognitive load of
reading the top-level use case.

Public surface
--------------
``IngestionIdempotencyService``   - guards against duplicate run submissions.
``SourceRecordContinuityService`` - resolves the prior event for a stable
                                    source_record_id (includes advisory lock).
``EventResolutionService``        - identity-index match -> attach/review/new.
``ClaimWriter``                   - normalise + persist new claims, supersede old.
``ConflictReconciler``            - reconcile conflicts after claim writes.
``ProjectionUpdater``             - queue outbox event for projection rebuild.
``IdentityIndexUpdater``          - maintain the synchronous identity substrate.
"""

from atlas.application.ingestion._claim_writer import ClaimWriter
from atlas.application.ingestion._conflict_reconciler import ConflictReconciler
from atlas.application.ingestion._continuity import SourceRecordContinuityService
from atlas.application.ingestion._event_resolution import EventResolutionService
from atlas.application.ingestion._idempotency import IngestionIdempotencyService
from atlas.application.ingestion._identity_index_updater import IdentityIndexUpdater
from atlas.application.ingestion._projection_updater import ProjectionUpdater

__all__ = [
    "ClaimWriter",
    "ConflictReconciler",
    "EventResolutionService",
    "IdentityIndexUpdater",
    "IngestionIdempotencyService",
    "ProjectionUpdater",
    "SourceRecordContinuityService",
]
