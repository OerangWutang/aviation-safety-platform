from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Response, status

from atlas.application.dto import CurrentUser, IngestionClaimDTO
from atlas.application.unit_of_work import UnitOfWork
from atlas.application.use_cases.ingest_source_data import IngestSourceData
from atlas.domain.enums import Role
from atlas.presentation.api.dependencies import get_uow, require_role
from atlas.presentation.api.schemas.ingestion import IngestionRequest, IngestionResponse

router = APIRouter(prefix="/ingestion", tags=["ingestion"])


@router.post(
    "/sources/{source_id}",
    response_model=IngestionResponse,
    status_code=status.HTTP_201_CREATED,
    responses={status.HTTP_200_OK: {"model": IngestionResponse}},
)
async def ingest_from_source(
    source_id: UUID,
    body: IngestionRequest,
    response: Response,
    uow: UnitOfWork = Depends(get_uow, scope="function"),
    current_user: CurrentUser = Depends(require_role(Role.ADMIN, Role.REVIEWER)),
) -> IngestionResponse:
    """Ingest claims from a source into the accident record system.

    Idempotency
    -----------
    Supply ``idempotency_key`` to make HTTP retries safe. The router derives
    a deterministic run id from ``(source_id, idempotency_key)``. Retrying
    the same key with the same full ingestion submission (raw payload, claims,
    source_record_id, event_id, and captured_at) returns the stored result without adding new claims or events.
    If that event has since been merged, the replay response returns the
    current canonical ``event_id``. Reusing the key with a
    changed submission returns an idempotency mismatch.

    Source record continuity
    ------------------------
    Supply ``source_record_id`` when the source has its own stable identifier
    for the accident (e.g. NTSB accession number).  Re-ingestions of updated
    data for the same record are attached to the original event rather than
    creating a new one.

    Event matching
    --------------
    When neither ``event_id`` nor a known ``source_record_id`` is provided,
    the system scores the incoming claims against all existing events whose
    ``event_date`` is within ±1 day.  A high-confidence match (>=0.75)
    attaches the claims to the existing event.  A medium-confidence match
    (0.40-0.75) creates a new event and sets ``pending_review_id``/
    ``pending_review_ids`` so a curator can confirm or reject the pairing.
    """
    # Derive a deterministic ingestion_run_id from the idempotency key.
    # When no key is supplied we generate a fresh UUID (non-idempotent path,
    # preserved for backward compatibility with callers that generate their
    # own key upstream or don't need retry safety).
    if body.idempotency_key:
        ingestion_run_id = IngestSourceData.derive_ingestion_run_id(source_id, body.idempotency_key)
    else:
        ingestion_run_id = uuid4()

    claims = [
        IngestionClaimDTO(field_name=claim.field_name, field_value=claim.field_value)
        for claim in body.claims
    ]

    result = await IngestSourceData(uow).execute_with_result(
        source_id=source_id,
        raw_payload=body.raw_payload,
        ingestion_run_id=ingestion_run_id,
        claims_data=claims,
        captured_at=body.captured_at,
        event_id=body.event_id,
        source_record_id=body.source_record_id,
    )

    created_this_request = result.event_created and not result.idempotent_replay
    response_body = IngestionResponse(
        event_id=result.event_id,
        created=created_this_request,
        created_this_request=created_this_request,
        event_created=result.event_created,
        ingestion_run_id=ingestion_run_id,
        pending_review_id=result.pending_review_id,
        pending_review_ids=list(result.pending_review_ids),
        snapshot_created=result.snapshot_created,
        idempotent_replay=result.idempotent_replay,
        attached_by=result.attached_by,
    )

    if result.idempotent_replay:
        response.status_code = status.HTTP_200_OK

    return response_body
