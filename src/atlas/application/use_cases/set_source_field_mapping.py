"""SetSourceFieldMapping - replace Source.field_mapping_json after validation.

Why a dedicated use case rather than a generic ``SourceRepository.save``
-----------------------------------------------------------------------
``Source.field_mapping_json`` is the only mutable field a curator needs to
edit at runtime; the rest of the Source entity (``name``, ``kind``,
``reliability_tier``) drives historical claim provenance and must not be
changed in place.  Exposing a narrow use case keeps that surface small and
makes the audit story obvious: a single endpoint, validated targets, one
``ingestion`` worker-cycle later the new mapping is durably applied for
every worker.

Validation happens here, *before* the repository call, so the same loud
``DomainValidationError`` is raised whether the caller is the API, the CLI,
or a future internal admin script.  The underlying ``SourceFieldMapper``
constructor catches typo'd canonical targets but raises plain ``ValueError``;
we adapt it to the typed domain error here so callers see a consistent
exception type.
"""

from __future__ import annotations

import logging
from uuid import UUID

from atlas.application.unit_of_work import UnitOfWork
from atlas.domain.entities import Source
from atlas.domain.exceptions import DomainValidationError, SourceNotFoundError
from atlas.domain.services.ingestion import SourceFieldMapper, normalise_field_key

logger = logging.getLogger(__name__)


class SetSourceFieldMapping:
    """Replace the durable source-specific field-name mapping for one source."""

    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    async def execute(
        self,
        source_id: UUID,
        field_mapping: dict[str, str],
    ) -> Source:
        if not isinstance(field_mapping, dict):
            raise DomainValidationError(
                "field_mapping must be a JSON object mapping raw source field "
                "names to canonical Atlas field names"
            )

        # Reject keys that would normalise to the same canonical key.  The
        # mapper itself silently overwrites duplicates on construction; doing
        # this check here turns a silent overwrite into a loud rejection
        # before the row is persisted.
        seen_keys: dict[str, str] = {}
        collisions: list[tuple[str, str]] = []
        for raw_field in field_mapping:
            normalised = normalise_field_key(raw_field)
            if normalised in seen_keys:
                collisions.append((seen_keys[normalised], raw_field))
            else:
                seen_keys[normalised] = raw_field
        if collisions:
            details = ", ".join(f"{a!r}<->{b!r}" for a, b in collisions)
            raise DomainValidationError(
                f"field_mapping has raw keys that collide under tolerant "
                f"normalisation: {details}.  Each canonical target must come "
                "from exactly one raw key."
            )

        # Validate each canonical target by constructing a SourceFieldMapper.
        # This raises ``ValueError`` on the first unknown target so the curator
        # sees the bad target name in the error.  Adapt it to a typed error so
        # the API layer can map it consistently to 422.
        try:
            SourceFieldMapper(field_mapping)
        except ValueError as exc:
            raise DomainValidationError(
                f"Invalid canonical target in field_mapping: {exc}"
            ) from exc

        existing = await self._uow.sources.get(source_id)
        if existing is None:
            raise SourceNotFoundError(f"Source {source_id} not found")

        updated = await self._uow.sources.update_field_mapping(source_id, field_mapping)
        if updated is None:
            # Concurrent delete between the existence check and the update.
            raise SourceNotFoundError(f"Source {source_id} not found at write time")
        await self._uow.commit()
        logger.info(
            "Source field mapping updated",
            extra={
                "source_id": str(source_id),
                "entry_count": len(field_mapping),
            },
        )
        return updated
