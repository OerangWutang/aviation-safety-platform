"""Tests for Orion v0.1 entity extraction use case."""

from __future__ import annotations

from uuid import uuid4

import pytest

from atlas.application.use_cases.extract_orion_entities_from_event import (
    ExtractOrionEntitiesFromEvent,
)
from atlas.domain.constants import DISPUTED_MARKER
from atlas.domain.entities import AccidentEvent, ProjectedAccidentRecord
from atlas.domain.enums import OrionEntityType, OrionRelationshipType
from tests.domain._fake_uow import InMemoryUnitOfWork


def _make_uow_with_projection(fields: dict) -> tuple[InMemoryUnitOfWork, object]:
    uow = InMemoryUnitOfWork()
    event_id = uuid4()
    uow.store.events[event_id] = AccidentEvent(id=event_id)
    uow.store.projections[event_id] = ProjectedAccidentRecord(event_id=event_id, fields=fields)
    return uow, event_id


@pytest.mark.asyncio
async def test_extracts_aircraft_from_registration():
    uow, event_id = _make_uow_with_projection({"registration": "PH-BXA"})
    result = await ExtractOrionEntitiesFromEvent(uow).execute(event_id)

    assert result.entities_created_count == 1
    assert result.relationships_created_count == 1

    entities = await uow.orion_entities.list_for_event(event_id)
    assert len(entities) == 1
    assert entities[0].entity_type == OrionEntityType.AIRCRAFT
    assert entities[0].canonical_name == "PH-BXA"

    identifiers = await uow.orion_identifiers.list_for_entity(entities[0].id)
    assert any(i.normalized_value == "phbxa" for i in identifiers)


@pytest.mark.asyncio
async def test_reuses_existing_aircraft_entity():
    uow, event_id = _make_uow_with_projection({"registration": "PH-BXA"})
    await ExtractOrionEntitiesFromEvent(uow).execute(event_id)

    event_id2 = uuid4()
    uow.store.events[event_id2] = AccidentEvent(id=event_id2)
    uow.store.projections[event_id2] = ProjectedAccidentRecord(
        event_id=event_id2,
        fields={"registration": "PH-BXA"},
    )

    result2 = await ExtractOrionEntitiesFromEvent(uow).execute(event_id2)
    assert result2.entities_created_count == 0
    assert result2.entities_reused_count == 1

    all_aircraft = [
        e for e in uow.store.orion.entities.values() if e.entity_type == OrionEntityType.AIRCRAFT
    ]
    assert len(all_aircraft) == 1


@pytest.mark.asyncio
async def test_extracts_operator_and_links_aircraft_operated_by():
    uow, event_id = _make_uow_with_projection(
        {"registration": "G-BOAC", "operator": "British Airways"}
    )
    result = await ExtractOrionEntitiesFromEvent(uow).execute(event_id)

    assert result.entities_created_count == 2

    rels = await uow.orion_relationships.list_for_event(event_id)
    rel_types = {r.relationship_type for r in rels}
    assert OrionRelationshipType.INVOLVED_AIRCRAFT in rel_types
    assert OrionRelationshipType.OPERATED_BY in rel_types

    operated_by = [r for r in rels if r.relationship_type == OrionRelationshipType.OPERATED_BY]
    assert len(operated_by) == 1
    aircraft = next(
        e for e in uow.store.orion.entities.values() if e.entity_type == OrionEntityType.AIRCRAFT
    )
    operator = next(
        e for e in uow.store.orion.entities.values() if e.entity_type == OrionEntityType.OPERATOR
    )
    assert operated_by[0].subject_entity_id == aircraft.id
    assert operated_by[0].object_entity_id == operator.id


@pytest.mark.asyncio
async def test_extracts_aircraft_type_and_manufacturer_relationship():
    uow, event_id = _make_uow_with_projection(
        {"aircraft_type": "Boeing 737", "manufacturer": "Boeing"}
    )
    result = await ExtractOrionEntitiesFromEvent(uow).execute(event_id)

    assert result.entities_created_count == 2
    rels = await uow.orion_relationships.list_for_event(event_id)
    assert OrionRelationshipType.MANUFACTURED_BY in {r.relationship_type for r in rels}


@pytest.mark.asyncio
async def test_extracts_airport_country_and_occurred_at_located_in():
    uow, event_id = _make_uow_with_projection({"airport": "EHAM", "country": "Netherlands"})
    await ExtractOrionEntitiesFromEvent(uow).execute(event_id)

    rels = await uow.orion_relationships.list_for_event(event_id)
    rel_types = {r.relationship_type for r in rels}
    assert OrionRelationshipType.OCCURRED_AT in rel_types
    assert OrionRelationshipType.LOCATED_IN in rel_types

    airport = next(
        e for e in uow.store.orion.entities.values() if e.entity_type == OrionEntityType.AIRPORT
    )
    identifiers = await uow.orion_identifiers.list_for_entity(airport.id)
    assert any(i.identifier_type == "icao" for i in identifiers)


@pytest.mark.asyncio
async def test_missing_fields_do_not_fail():
    uow, event_id = _make_uow_with_projection({})
    result = await ExtractOrionEntitiesFromEvent(uow).execute(event_id)
    assert result.entities_created_count == 0
    assert result.relationships_created_count == 0


@pytest.mark.asyncio
async def test_disputed_fields_are_skipped():
    uow, event_id = _make_uow_with_projection({"registration": DISPUTED_MARKER, "operator": "KLM"})
    await ExtractOrionEntitiesFromEvent(uow).execute(event_id)
    entity_types = {e.entity_type for e in uow.store.orion.entities.values()}
    assert OrionEntityType.AIRCRAFT not in entity_types
    assert OrionEntityType.OPERATOR in entity_types


@pytest.mark.asyncio
async def test_extraction_is_idempotent():
    uow, event_id = _make_uow_with_projection(
        {"registration": "D-ABYF", "operator": "Lufthansa", "aircraft_type": "Boeing 747"}
    )
    first = await ExtractOrionEntitiesFromEvent(uow).execute(event_id)
    second = await ExtractOrionEntitiesFromEvent(uow).execute(event_id)

    assert second.entities_created_count == 0
    assert (
        second.entities_reused_count == first.entities_created_count + first.entities_reused_count
    )
    rels = await uow.orion_relationships.list_for_event(event_id)
    assert len(rels) == first.relationships_created_count


@pytest.mark.asyncio
async def test_claim_provenance_uses_source_reliability_tier_for_orion():
    from datetime import UTC, datetime

    from atlas.domain.entities import Claim, Source
    from atlas.domain.enums import ClaimType, SourceKind

    uow, event_id = _make_uow_with_projection({"registration": "PH-BXA"})

    low_trust = Source(name="low", kind=SourceKind.EXTERNAL, reliability_tier=5)
    high_trust = Source(name="high", kind=SourceKind.EXTERNAL, reliability_tier=1)
    uow.store.sources[low_trust.id] = low_trust
    uow.store.sources[high_trust.id] = high_trust

    older_low_trust_claim = Claim(
        event_id=event_id,
        source_id=low_trust.id,
        field_name="registration",
        field_value="PH-BXA",
        claim_type=ClaimType.RAW,
        created_at=datetime(2023, 1, 1, tzinfo=UTC),
    )
    newer_high_trust_claim = Claim(
        event_id=event_id,
        source_id=high_trust.id,
        field_name="registration",
        field_value="PH-BXA",
        claim_type=ClaimType.RAW,
        created_at=datetime(2023, 1, 2, tzinfo=UTC),
    )
    uow.store.claims[older_low_trust_claim.id] = older_low_trust_claim
    uow.store.claims[newer_high_trust_claim.id] = newer_high_trust_claim

    await ExtractOrionEntitiesFromEvent(uow).execute(event_id)
    entity = next(iter(uow.store.orion.entities.values()))
    identifiers = await uow.orion_identifiers.list_for_entity(entity.id)
    registration_identifier = next(i for i in identifiers if i.identifier_type == "registration")
    assert registration_identifier.source_claim_id == newer_high_trust_claim.id
