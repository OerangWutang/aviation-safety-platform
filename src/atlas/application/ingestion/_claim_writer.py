"""ClaimWriter - normalise, persist new claims, and supersede old ones."""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID, uuid4

from atlas.application.unit_of_work import UnitOfWork
from atlas.domain.entities import Claim, ClaimConflict, ClaimHistory
from atlas.domain.enums import ClaimType, ModifierType
from atlas.domain.exceptions import DomainValidationError, DuplicateClaimFieldError
from atlas.domain.services.ingestion import SourceNormalizerRegistry, default_normalizer_registry

logger = logging.getLogger(__name__)


def _assert_no_duplicate_fields(
    items: list[dict[str, Any]],
    message_prefix: str,
) -> None:
    """Raise DuplicateClaimFieldError if any field_name appears more than once.

    Centralised here so ``normalise_claims`` and ``write_normalised`` enforce
    the same invariant from the same code path.  When the message needs to
    differ between the two call sites, callers pass a distinct ``message_prefix``.
    """
    seen: set[str] = set()
    duplicates: set[str] = set()
    for item in items:
        field_name = str(item.get("field_name", ""))
        if field_name in seen:
            duplicates.add(field_name)
        seen.add(field_name)
    if duplicates:
        raise DuplicateClaimFieldError(f"{message_prefix}: {sorted(duplicates)}")


class ClaimWriteResult:
    __slots__ = (
        "affected_fields",
        "new_claims",
        "resolved_conflicts_to_reconcile",
        "superseded_claims",
    )

    def __init__(self) -> None:
        self.new_claims: list[Claim] = []
        self.superseded_claims: list[Claim] = []
        self.affected_fields: set[str] = set()
        # Resolved conflicts whose winning claim was superseded; need reconciliation.
        self.resolved_conflicts_to_reconcile: list[tuple[ClaimConflict, Claim]] = []


class ClaimWriter:
    """Write normalised claims for a single ingestion, superseding stale versions.

    For each field in the incoming claims:
    1. Create a new ``Claim`` and its ``ClaimHistory`` row.
    2. Supersede any prior active claims from the same ``(source_id,
       source_record_id)`` for that field.
    3. If a superseded claim was the winner of a RESOLVED conflict, record that
       the conflict needs reconciliation so the caller can fix it.

    The source-kind normaliser is applied before writing so that raw values are
    coerced to canonical form once, on ingestion, rather than at query time.
    """

    def __init__(
        self,
        uow: UnitOfWork,
        normalizer_registry: SourceNormalizerRegistry | None = None,
    ) -> None:
        self._uow = uow
        self._normalizer_registry = normalizer_registry or default_normalizer_registry

    def normalise_claims(
        self,
        source_kind: str,
        claims_data: list[dict[str, Any]],
        *,
        source_id: UUID | None = None,
        ingestion_run_id: UUID | None = None,
        source_field_mapping: dict[str, str] | None = None,
    ) -> list[dict[str, Any]]:
        """Normalize claims once and reject duplicate canonical fields.

        Source-specific field mapping can make two raw fields collide, for
        example ``tailNumber`` and ``registration``.  Reject here so callers
        that use ``ClaimWriter`` directly get the same guard as the top-level
        ingestion use case.
        """
        normalised = self._normalizer_registry.normalize(
            source_kind,
            claims_data,
            source_id=source_id,
            ingestion_run_id=ingestion_run_id,
            source_field_mapping=source_field_mapping,
        )
        _assert_no_duplicate_fields(
            normalised,
            "Ingestion payload contains duplicate canonical field_name entries "
            "after source field mapping / normalization",
        )
        return normalised

    async def write(
        self,
        event_id: UUID,
        source_id: UUID,
        snapshot_id: UUID,
        source_kind: str,
        claims_data: list[dict[str, Any]],
        ingestion_run_id: UUID,
        source_record_id: str | None,
        *,
        source_field_mapping: dict[str, str] | None,
    ) -> ClaimWriteResult:
        """Normalize and write claims using the caller's durable source mapping.

        The mapping argument is intentionally keyword-only and has no default:
        callers must explicitly pass ``Source.field_mapping_json`` or ``None``
        so direct use of this helper cannot silently diverge from the top-level
        ingestion use case's canonical field-name resolution.
        """
        normalised = self.normalise_claims(
            source_kind,
            claims_data,
            source_id=source_id,
            ingestion_run_id=ingestion_run_id,
            source_field_mapping=source_field_mapping,
        )
        return await self.write_normalised(
            event_id=event_id,
            source_id=source_id,
            snapshot_id=snapshot_id,
            normalised_claims=normalised,
            ingestion_run_id=ingestion_run_id,
            source_record_id=source_record_id,
        )

    async def write_normalised(
        self,
        event_id: UUID,
        source_id: UUID,
        snapshot_id: UUID,
        normalised_claims: list[dict[str, Any]],
        ingestion_run_id: UUID,
        source_record_id: str | None,
    ) -> ClaimWriteResult:
        result = ClaimWriteResult()
        # Validate duplicate canonical fields even when a caller bypasses
        # ``write`` and supplies already-normalized claims directly.
        _assert_no_duplicate_fields(
            normalised_claims,
            "Ingestion payload contains duplicate canonical field_name entries",
        )

        # Build a lookup of prior active claims by field_name. When a stable
        # source_record_id is present, supersede only the previous claims for
        # that source record. When it is absent, fall back to the narrower
        # source_id + event_id + field_name scope so repeated same-source
        # ingestions do not leave multiple active claims that conflict detection
        # intentionally ignores as same-source disagreement.
        prior_claims_by_field: dict[str, list[Claim]] = {}
        if source_record_id is not None:
            prior = await self._uow.claims.find_active_by_source_record(source_id, source_record_id)
            for old in prior:
                prior_claims_by_field.setdefault(old.field_name, []).append(old)
        else:
            field_names = {str(item.get("field_name", "")) for item in normalised_claims}
            for field_name in field_names:
                prior = await self._uow.claims.find_active_by_event_field(event_id, field_name)
                prior_claims_by_field[field_name] = [
                    old for old in prior if old.source_id == source_id
                ]

        for item in normalised_claims:
            field_name = item["field_name"]
            if "field_value" not in item:
                raise DomainValidationError(
                    f"claims_data item for '{field_name}' is missing required key 'field_value'"
                )
            value = item["field_value"]

            claim = Claim(
                id=uuid4(),
                event_id=event_id,
                source_id=source_id,
                raw_snapshot_id=snapshot_id,
                field_name=field_name,
                field_value=value,
                claim_type=ClaimType.RAW,
            )
            await self._uow.claims.add(claim)
            # Flush the claim before adding its history row.  The ORM
            # layer declares no relationship() between ClaimModel and
            # ClaimHistoryModel, and ClaimModel carries a
            # self-referential FK (superseded_by_claim_id), which
            # defeats SQLAlchemy's automatic insert-ordering: without
            # this flush it emits the claim_history INSERT before the
            # claims INSERT and Postgres rejects it with a foreign-key
            # violation (claim_history_claim_id_fkey).  Flushing here
            # guarantees the claim row exists before the history row
            # references it.  Verified against live PostgreSQL.
            await self._uow.flush()
            await self._uow.claim_history.add(
                ClaimHistory(
                    id=uuid4(),
                    claim_id=claim.id,
                    event_id=event_id,
                    from_value=None,
                    to_value=value,
                    from_claim_type=None,
                    to_claim_type=ClaimType.RAW,
                    action="created",
                    reason="Initial ingestion",
                    modifier_type=ModifierType.INGESTION,
                    modifier_id=ingestion_run_id,
                )
            )
            result.new_claims.append(claim)

            superseded = await self._uow.claims.bulk_supersede(
                [old.id for old in prior_claims_by_field.get(field_name, [])],
                by_claim_id=claim.id,
            )
            for old in superseded:
                result.affected_fields.add(field_name)
                result.superseded_claims.append(old)
                await self._uow.claim_history.add(
                    ClaimHistory(
                        id=uuid4(),
                        claim_id=old.id,
                        event_id=old.event_id,
                        from_value=old.field_value,
                        to_value=old.field_value,
                        from_claim_type=old.claim_type,
                        to_claim_type=ClaimType.SUPERSEDED,
                        action="superseded",
                        reason=(
                            "Superseded by same-source re-ingestion "
                            f"({source_record_id or 'no source_record_id'})"
                        ),
                        modifier_type=ModifierType.INGESTION,
                        modifier_id=ingestion_run_id,
                    )
                )

        # Batch-lookup resolved conflicts for all superseded claims in one query
        # instead of issuing one DB round-trip per superseded claim.
        if result.superseded_claims:
            resolved_by_winner = await self._uow.conflicts.find_resolved_by_winning_claims(
                [c.id for c in result.superseded_claims]
            )
            for old in result.superseded_claims:
                resolved_won = resolved_by_winner.get(old.id)
                if resolved_won is not None:
                    # Find the new claim that replaced this one.
                    replacement = next(
                        (nc for nc in result.new_claims if nc.field_name == old.field_name),
                        None,
                    )
                    if replacement is not None:
                        result.resolved_conflicts_to_reconcile.append((resolved_won, replacement))

        return result

    def extract_normalised_fields(
        self,
        source_kind: str,
        claims_data: list[dict[str, Any]],
        *,
        source_id: UUID | None = None,
        ingestion_run_id: UUID | None = None,
        source_field_mapping: dict[str, str] | None,
    ) -> dict[str, Any]:
        """Return a field_name -> field_value dict for the normalised claims.

        This uses the same normalization path as ``write()``, including durable
        source field mapping, so callers do not accidentally inspect a different
        canonical field set from the one that would be persisted.
        """
        normalised = self.normalise_claims(
            source_kind,
            claims_data,
            source_id=source_id,
            ingestion_run_id=ingestion_run_id,
            source_field_mapping=source_field_mapping,
        )
        return {item["field_name"]: item.get("field_value") for item in normalised}
