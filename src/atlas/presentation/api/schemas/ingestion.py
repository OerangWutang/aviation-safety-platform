from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator


class IngestionClaimRequest(BaseModel):
    field_name: str
    field_value: Any

    @field_validator("field_name")
    @classmethod
    def field_name_non_empty(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("field_name must not be blank")
        return value


class IngestionRequest(BaseModel):
    raw_payload: dict[str, Any]
    # The configured MAX_CLAIMS_PER_REQUEST setting is enforced by the
    # application use case. Keeping the HTTP schema unconstrained here prevents
    # a hard-coded OpenAPI/API limit from drifting away from operational config.
    claims: list[IngestionClaimRequest] = Field(default_factory=list)
    captured_at: datetime | None = None
    event_id: UUID | None = None

    # ── Ingestion identity fields ─────────────────────────────────────────
    # ``idempotency_key`` makes HTTP retries safe. The router derives a
    # deterministic ``ingestion_run_id`` from ``(source_id, idempotency_key)``;
    # the use case then compares the full submission hash (raw payload, claims,
    # source_record_id, event_id, captured_at). Exact retries return the
    # stored result, canonicalizing event_id if the original event was later
    # merged; changed submissions with the same key are rejected.
    #
    # ``source_record_id`` is the source system's own stable identifier for
    # this accident record (e.g. NTSB accession number, IATA incident ID).
    # When provided, re-submissions of updated data for the same record are
    # attached to the original event rather than creating a new one.
    idempotency_key: str | None = Field(
        default=None,
        description=(
            "Client-assigned key that makes this request idempotent. "
            "Retrying with the same key and identical full submission returns "
            "the stored result without adding claims. If the original event "
            "was later merged, the response uses the current canonical event_id. "
            "Changed submissions with the same key are rejected. Max 200 chars."
        ),
        max_length=200,
    )
    source_record_id: str | None = Field(
        default=None,
        description=(
            "Stable identifier assigned by the source system to this accident record "
            "(e.g. NTSB accession number, IATA incident ID). When provided, "
            "re-ingestions of updated data for the same record are attached to "
            "the original event rather than creating a new one. Max 255 chars."
        ),
        max_length=255,
    )

    @field_validator("source_record_id")
    @classmethod
    def source_record_id_trim_blank(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value or None

    @model_validator(mode="after")
    def validate_claims_not_empty(self) -> IngestionRequest:
        if not self.claims:
            raise ValueError("claims must not be empty")
        return self


class IngestionResponse(BaseModel):
    event_id: UUID
    # Backward-compatible alias for created_this_request.  False for exact
    # idempotent replays even if the original ingestion created the event.
    created: bool = True
    # True only when this HTTP request created a brand-new AccidentEvent.
    created_this_request: bool = True
    # Whether the original ingestion operation represented by this response
    # created the event.  This may be true on idempotent replay.
    event_created: bool = True
    ingestion_run_id: UUID | None = None
    # Set when a medium-confidence or ambiguous identity match was detected:
    # curator review needed. Kept for backward compatibility; callers that
    # need the full ambiguous tie context should read ``pending_review_ids``.
    pending_review_id: UUID | None = None
    pending_review_ids: list[UUID] = Field(default_factory=list)
    # Operational metadata: lets clients and operators distinguish a fresh
    # write from an idempotent retry/source-record correction/identity match.
    snapshot_created: bool = True
    idempotent_replay: bool = False
    attached_by: str = ""
