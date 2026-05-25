"""Extract canonical Orion entities from an Atlas accident projection."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from uuid import UUID

from atlas.application.unit_of_work import UnitOfWork
from atlas.application.use_cases._provenance import select_projected_claims_by_field
from atlas.domain.constants import DISPUTED_MARKER
from atlas.domain.entities import (
    Claim,
    OrionEntity,
    OrionEntityClaimLink,
    OrionEntityIdentifier,
    OrionExtractionResult,
    OrionRelationship,
)
from atlas.domain.enums import OrionRelationshipType
from atlas.domain.services.orion_normalizers import normalize_name
from atlas.domain.services.orion_resolution import (
    ResolutionCandidate,
    resolve_aircraft,
    resolve_aircraft_type,
    resolve_airport,
    resolve_country,
    resolve_investigation_agency,
    resolve_manufacturer,
    resolve_operator,
)

logger = logging.getLogger(__name__)

_REGISTRATION_FIELDS = ("registration", "aircraft_registration")
_OPERATOR_FIELDS = ("operator", "airline")
_AIRCRAFT_TYPE_FIELDS = ("aircraft_type",)
_MANUFACTURER_FIELDS = ("manufacturer",)
_COUNTRY_FIELDS = ("country",)
_AGENCY_FIELDS = ("investigation_agency", "agency")


def _safe_str(value: object) -> str | None:
    """Return value as a clean string only if it is a non-disputed primitive."""
    if value is None:
        return None
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        if stripped == DISPUTED_MARKER or stripped.startswith(DISPUTED_MARKER):
            return None
        return stripped
    if not isinstance(value, (int, float)):
        return None
    return str(value)


def _first_field(fields: dict[str, object], *names: str) -> tuple[str, str] | tuple[None, None]:
    """Return (field_name, value) for the first non-blank, non-disputed field."""
    for name in names:
        val = _safe_str(fields.get(name))
        if val is not None:
            return name, val
    return None, None


@dataclass
class _ExtractionContext:
    """Shared mutable state threaded through the per-entity extraction helpers.

    Promoting the inner async closures of ``ExtractOrionEntitiesFromEvent.execute``
    to private methods required capturing the three things every helper needs.
    Using a small context object avoids adding four parameters to every signature.
    """

    uow: UnitOfWork
    event_id: UUID
    claims_by_field: dict[str, Claim]


def _count_entity(result: OrionExtractionResult, entity: OrionEntity, created: bool) -> None:
    if created:
        result.entities_created_count += 1
    else:
        result.entities_reused_count += 1
    if entity.id not in result.entity_ids:
        result.entity_ids.append(entity.id)


def _count_rel(result: OrionExtractionResult, rel: OrionRelationship, created: bool) -> None:
    if created:
        result.relationships_created_count += 1
    if rel.id not in result.relationship_ids:
        result.relationship_ids.append(rel.id)


class ExtractOrionEntitiesFromEvent:
    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    async def execute(self, event_id: UUID) -> OrionExtractionResult:
        uow = self._uow
        result = OrionExtractionResult(event_id=event_id)

        projection = await uow.projections.get(event_id)
        if projection is None:
            logger.warning("orion_extract: no projection for event_id=%s", event_id)
            return result

        fields: dict[str, object] = projection.fields or {}
        claims_by_field = await select_projected_claims_by_field(
            uow,
            event_id=event_id,
            fields=fields,
            safe_str=_safe_str,
        )

        ctx = _ExtractionContext(uow=uow, event_id=event_id, claims_by_field=claims_by_field)

        aircraft_entity: OrionEntity | None = None
        reg_field, reg_value = _first_field(fields, *_REGISTRATION_FIELDS)
        if reg_value:
            candidate = resolve_aircraft(reg_value)
            if candidate and candidate.primary_identifier:
                aircraft_entity, created = await self._resolve_or_create(ctx, candidate)
                _count_entity(result, aircraft_entity, created)
                primary = candidate.primary_identifier
                await self._attach_identifier(
                    ctx,
                    aircraft_entity,
                    primary.identifier_type,
                    primary.identifier_value,
                    primary.normalized_value,
                    primary.confidence,
                    claim_field=reg_field,
                )
                await self._maybe_claim_link(ctx, aircraft_entity, reg_field)
                rel, created_rel = await self._upsert_rel(
                    ctx,
                    OrionRelationshipType.INVOLVED_AIRCRAFT,
                    aircraft_entity.id,
                    claim_field=reg_field,
                )
                _count_rel(result, rel, created_rel)

        op_field, op_value = _first_field(fields, *_OPERATOR_FIELDS)
        if op_value:
            candidate = resolve_operator(op_value)
            if candidate and candidate.primary_identifier:
                operator_entity, created = await self._resolve_or_create(ctx, candidate)
                _count_entity(result, operator_entity, created)
                primary = candidate.primary_identifier
                await self._attach_identifier(
                    ctx,
                    operator_entity,
                    primary.identifier_type,
                    primary.identifier_value,
                    primary.normalized_value,
                    primary.confidence,
                    claim_field=op_field,
                )
                await self._maybe_claim_link(ctx, operator_entity, op_field)
                if aircraft_entity is not None:
                    rel, created_rel = await self._upsert_rel(
                        ctx,
                        OrionRelationshipType.OPERATED_BY,
                        operator_entity.id,
                        subject_entity_id=aircraft_entity.id,
                        claim_field=op_field,
                    )
                    _count_rel(result, rel, created_rel)

        aircraft_type_entity: OrionEntity | None = None
        at_field, at_value = _first_field(fields, *_AIRCRAFT_TYPE_FIELDS)
        if at_value:
            candidate = resolve_aircraft_type(at_value)
            if candidate and candidate.primary_identifier:
                aircraft_type_entity, created = await self._resolve_or_create(ctx, candidate)
                _count_entity(result, aircraft_type_entity, created)
                primary = candidate.primary_identifier
                await self._attach_identifier(
                    ctx,
                    aircraft_type_entity,
                    primary.identifier_type,
                    primary.identifier_value,
                    primary.normalized_value,
                    primary.confidence,
                    claim_field=at_field,
                )
                await self._maybe_claim_link(ctx, aircraft_type_entity, at_field)
                if aircraft_entity is not None:
                    rel, created_rel = await self._upsert_rel(
                        ctx,
                        OrionRelationshipType.AIRCRAFT_TYPE,
                        aircraft_type_entity.id,
                        subject_entity_id=aircraft_entity.id,
                        claim_field=at_field,
                    )
                    _count_rel(result, rel, created_rel)

        mfr_field, mfr_value = _first_field(fields, *_MANUFACTURER_FIELDS)
        if mfr_value:
            candidate = resolve_manufacturer(mfr_value)
            if candidate and candidate.primary_identifier:
                manufacturer_entity, created = await self._resolve_or_create(ctx, candidate)
                _count_entity(result, manufacturer_entity, created)
                primary = candidate.primary_identifier
                await self._attach_identifier(
                    ctx,
                    manufacturer_entity,
                    primary.identifier_type,
                    primary.identifier_value,
                    primary.normalized_value,
                    primary.confidence,
                    claim_field=mfr_field,
                )
                await self._maybe_claim_link(ctx, manufacturer_entity, mfr_field)
                if aircraft_type_entity is not None:
                    rel, created_rel = await self._upsert_rel(
                        ctx,
                        OrionRelationshipType.MANUFACTURED_BY,
                        manufacturer_entity.id,
                        subject_entity_id=aircraft_type_entity.id,
                        claim_field=mfr_field,
                    )
                    _count_rel(result, rel, created_rel)

        airport_entity: OrionEntity | None = None
        airport_raw = _safe_str(fields.get("airport"))
        location_raw = _safe_str(fields.get("location"))
        airport_candidate = resolve_airport(code=airport_raw, name=location_raw)
        if airport_candidate:
            airport_entity, created = await self._resolve_or_create(ctx, airport_candidate)
            _count_entity(result, airport_entity, created)
            airport_field = "airport" if airport_raw else "location"
            if airport_candidate.primary_identifier:
                primary = airport_candidate.primary_identifier
                await self._attach_identifier(
                    ctx,
                    airport_entity,
                    primary.identifier_type,
                    primary.identifier_value,
                    primary.normalized_value,
                    primary.confidence,
                    claim_field=airport_field,
                )
            for extra in airport_candidate.extra_identifiers:
                await self._attach_identifier(
                    ctx,
                    airport_entity,
                    extra.identifier_type,
                    extra.identifier_value,
                    extra.normalized_value,
                    extra.confidence,
                )
            await self._maybe_claim_link(ctx, airport_entity, airport_field)
            rel, created_rel = await self._upsert_rel(
                ctx, OrionRelationshipType.OCCURRED_AT, airport_entity.id, claim_field=airport_field
            )
            _count_rel(result, rel, created_rel)

        country_field, country_value = _first_field(fields, *_COUNTRY_FIELDS)
        if country_value:
            candidate = resolve_country(country_value)
            if candidate and candidate.primary_identifier:
                country_entity, created = await self._resolve_or_create(ctx, candidate)
                _count_entity(result, country_entity, created)
                primary = candidate.primary_identifier
                await self._attach_identifier(
                    ctx,
                    country_entity,
                    primary.identifier_type,
                    primary.identifier_value,
                    primary.normalized_value,
                    primary.confidence,
                    claim_field=country_field,
                )
                await self._maybe_claim_link(ctx, country_entity, country_field)
                if airport_entity is not None:
                    rel, created_rel = await self._upsert_rel(
                        ctx,
                        OrionRelationshipType.LOCATED_IN,
                        country_entity.id,
                        subject_entity_id=airport_entity.id,
                        claim_field=country_field,
                    )
                    _count_rel(result, rel, created_rel)

        agency_field, agency_value = _first_field(fields, *_AGENCY_FIELDS)
        if agency_value:
            candidate = resolve_investigation_agency(agency_value)
            if candidate and candidate.primary_identifier:
                agency_entity, created = await self._resolve_or_create(ctx, candidate)
                _count_entity(result, agency_entity, created)
                primary = candidate.primary_identifier
                await self._attach_identifier(
                    ctx,
                    agency_entity,
                    primary.identifier_type,
                    primary.identifier_value,
                    primary.normalized_value,
                    primary.confidence,
                    claim_field=agency_field,
                )
                await self._maybe_claim_link(ctx, agency_entity, agency_field)
                rel, created_rel = await self._upsert_rel(
                    ctx,
                    OrionRelationshipType.INVESTIGATED_BY,
                    agency_entity.id,
                    claim_field=agency_field,
                )
                _count_rel(result, rel, created_rel)

        await uow.commit()
        return result

    # ── Private helpers ───────────────────────────────────────────────────────

    async def _resolve_or_create(
        self,
        ctx: _ExtractionContext,
        candidate: ResolutionCandidate,
    ) -> tuple[OrionEntity, bool]:
        """Find an existing entity via identifier or name, or create a new one.

        Acquires the advisory lock for strong identifiers before looking up so
        concurrent extractors cannot race to create duplicate active entities.
        """
        entity: OrionEntity | None = None
        if candidate.primary_identifier is not None:
            primary = candidate.primary_identifier
            await ctx.uow.orion_entities.lock_for_identifier_identity(
                candidate.entity_type,
                primary.identifier_type,
                primary.normalized_value,
            )
            entity = await ctx.uow.orion_entities.find_by_identifier(
                candidate.entity_type,
                primary.identifier_type,
                primary.normalized_value,
            )
        if entity is None:
            entity = await ctx.uow.orion_entities.find_by_canonical_name(
                candidate.entity_type,
                normalize_name(candidate.canonical_name),
            )
        if entity is not None:
            return entity, False

        new_entity = OrionEntity(
            entity_type=candidate.entity_type,
            canonical_name=candidate.canonical_name,
            confidence=candidate.new_entity_confidence,
        )
        await ctx.uow.orion_entities.add(new_entity)
        return new_entity, True

    async def _attach_identifier(
        self,
        ctx: _ExtractionContext,
        entity: OrionEntity,
        id_type: str,
        id_value: str,
        norm_value: str,
        confidence: float,
        claim_field: str | None = None,
    ) -> None:
        claim = ctx.claims_by_field.get(claim_field) if claim_field else None
        ident = OrionEntityIdentifier(
            entity_id=entity.id,
            entity_type=entity.entity_type,
            identifier_type=id_type,
            identifier_value=id_value,
            normalized_value=norm_value,
            source_claim_id=getattr(claim, "id", None),
            raw_snapshot_id=getattr(claim, "raw_snapshot_id", None),
            confidence=confidence,
        )
        await ctx.uow.orion_identifiers.try_add(ident)

    async def _upsert_rel(
        self,
        ctx: _ExtractionContext,
        rel_type: OrionRelationshipType,
        object_entity_id: UUID,
        subject_entity_id: UUID | None = None,
        claim_field: str | None = None,
    ) -> tuple[OrionRelationship, bool]:
        claim = ctx.claims_by_field.get(claim_field) if claim_field else None
        rel = OrionRelationship(
            subject_entity_id=subject_entity_id,
            relationship_type=rel_type,
            object_entity_id=object_entity_id,
            accident_event_id=ctx.event_id,
            source_claim_id=getattr(claim, "id", None),
            raw_snapshot_id=getattr(claim, "raw_snapshot_id", None),
            confidence=0.95,
        )
        return await ctx.uow.orion_relationships.upsert_relationship(rel)

    async def _maybe_claim_link(
        self,
        ctx: _ExtractionContext,
        entity: OrionEntity,
        field_name: str | None,
    ) -> None:
        if not field_name:
            return
        claim = ctx.claims_by_field.get(field_name)
        if claim is None:
            return
        claim_id = getattr(claim, "id", None)
        source_id = getattr(claim, "source_id", None)
        if claim_id is None or source_id is None:
            return
        link = OrionEntityClaimLink(
            entity_id=entity.id,
            claim_id=claim_id,
            raw_snapshot_id=getattr(claim, "raw_snapshot_id", None),
            source_id=source_id,
            accident_event_id=ctx.event_id,
            confidence=0.95,
        )
        await ctx.uow.orion_claim_links.add(link)
