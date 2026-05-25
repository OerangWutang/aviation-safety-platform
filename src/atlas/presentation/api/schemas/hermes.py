from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel

from atlas.domain.enums import (
    HermesChangeType,
    HermesDocumentContentType,
    HermesFetchJobStatus,
    HermesSourceType,
    HermesTargetStatus,
)


class HermesSourceCreateRequest(BaseModel):
    name: str
    source_type: HermesSourceType
    base_url: str | None = None
    reliability_tier: str | None = None


class HermesSourceResponse(BaseModel):
    id: UUID
    name: str
    source_type: HermesSourceType
    base_url: str | None
    reliability_tier: str | None
    is_active: bool
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class HermesCrawlTargetCreateRequest(BaseModel):
    source_id: UUID
    url: str
    label: str | None = None


class HermesCrawlTargetResponse(BaseModel):
    id: UUID
    source_id: UUID
    url: str
    normalized_url: str
    status: HermesTargetStatus
    label: str | None
    last_fetch_job_id: UUID | None
    last_fetched_document_id: UUID | None
    last_content_sha256: str | None
    last_http_status: int | None
    last_fetched_at: datetime | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class HermesFetchJobEnqueueRequest(BaseModel):
    priority: int = 100
    scheduled_at: datetime | None = None


class HermesFetchJobResponse(BaseModel):
    id: UUID
    target_id: UUID
    status: HermesFetchJobStatus
    priority: int
    attempt_count: int
    max_attempts: int
    scheduled_at: datetime | None
    started_at: datetime | None
    finished_at: datetime | None
    error_message: str | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class HermesFetchedDocumentResponse(BaseModel):
    id: UUID
    target_id: UUID
    fetch_job_id: UUID
    url: str
    final_url: str | None
    http_status: int | None
    content_type: HermesDocumentContentType
    content_sha256: str
    content_length: int
    title: str | None
    storage_path: str | None
    raw_text_preview: str | None
    fetched_at: datetime
    created_at: datetime

    model_config = {"from_attributes": True}


class HermesSourceChangeResponse(BaseModel):
    id: UUID
    target_id: UUID
    fetch_job_id: UUID | None
    previous_document_id: UUID | None
    new_document_id: UUID | None
    change_type: HermesChangeType
    previous_sha256: str | None
    new_sha256: str | None
    detected_at: datetime
    created_at: datetime

    model_config = {"from_attributes": True}


class HermesFetchResultResponse(BaseModel):
    job_id: UUID
    target_id: UUID
    status: HermesFetchJobStatus
    document_id: UUID | None = None
    change_id: UUID | None = None
    change_type: HermesChangeType | None = None
    content_sha256: str | None = None
    error_message: str | None = None
