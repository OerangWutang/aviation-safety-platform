import asyncio
from uuid import uuid4

import pytest

from atlas.application.dto import IngestionClaimDTO
from atlas.application.use_cases.ingest_source_data import IngestSourceData
from atlas.config import get_settings
from atlas.domain.exceptions import PayloadTooLargeError, TooManyClaimsError
from atlas.presentation.api.schemas.ingestion import IngestionRequest


def _set_required_settings(monkeypatch, *, max_claims="2", max_payload="32"):
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://user:pass@localhost:5432/atlas")
    monkeypatch.setenv("DATABASE_SYNC_URL", "postgresql://user:pass@localhost:5432/atlas")
    monkeypatch.setenv("POSTGRES_USER", "user")
    monkeypatch.setenv("POSTGRES_PASSWORD", "pass")
    monkeypatch.setenv("POSTGRES_DB", "atlas")
    monkeypatch.setenv("MAX_CLAIMS_PER_REQUEST", max_claims)
    monkeypatch.setenv("MAX_RAW_PAYLOAD_BYTES", max_payload)
    get_settings.cache_clear()


def test_ingestion_request_rejects_empty_claims():
    with pytest.raises(ValueError, match="claims must not be empty"):
        IngestionRequest(raw_payload={}, claims=[])


def test_ingest_source_data_rejects_too_many_claims(monkeypatch):
    _set_required_settings(monkeypatch, max_claims="1", max_payload="1024")
    claims = [
        IngestionClaimDTO(field_name="event_date", field_value="2024-01-01"),
        IngestionClaimDTO(field_name="location", field_value="Amsterdam"),
    ]

    async def run():
        with pytest.raises(TooManyClaimsError, match="Too many claims"):
            await IngestSourceData(object()).execute(
                source_id=uuid4(),
                raw_payload={},
                ingestion_run_id=uuid4(),
                claims_data=claims,
            )

    asyncio.run(run())


def test_ingest_source_data_rejects_oversized_raw_payload(monkeypatch):
    _set_required_settings(monkeypatch, max_claims="10", max_payload="20")
    claims = [IngestionClaimDTO(field_name="event_date", field_value="2024-01-01")]

    async def run():
        with pytest.raises(PayloadTooLargeError, match="raw_payload is too large"):
            await IngestSourceData(object()).execute(
                source_id=uuid4(),
                raw_payload={"text": "x" * 100},
                ingestion_run_id=uuid4(),
                claims_data=claims,
            )

    asyncio.run(run())


def test_ingestion_claim_dto_rejects_blank_field_name():
    import pytest

    from atlas.application.dto import IngestionClaimDTO

    with pytest.raises(ValueError, match="field_name must not be blank"):
        IngestionClaimDTO(field_name="   ", field_value="x")
