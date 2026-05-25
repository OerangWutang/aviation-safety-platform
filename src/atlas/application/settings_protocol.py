"""Settings protocols.

Use cases consume only a small slice of the full ``atlas.config.Settings``
object. Keeping the contract narrow lets tests pass a ``SimpleNamespace`` stub
without depending on env vars, and keeps the type checker honest about which
settings each use case actually reads.
"""

from __future__ import annotations

from typing import Protocol
from uuid import UUID


class IngestionSettings(Protocol):
    max_claims_per_request: int
    max_raw_payload_bytes: int
    max_duplicate_reviews_per_ingestion: int


class CuratorOverrideSettings(Protocol):
    curator_override_source_id: UUID
    curator_override_source_name: str
