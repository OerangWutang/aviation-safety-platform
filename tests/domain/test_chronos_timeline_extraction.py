"""Chronos v0.1 domain-level tests for timeline extraction."""

from __future__ import annotations

from uuid import uuid4

import pytest

from atlas.application.use_cases.extract_chronos_timeline_from_event import (
    ExtractChronosTimelineFromEvent,
)
from atlas.domain.entities import AccidentEvent, ProjectedAccidentRecord
from atlas.domain.enums import ChronosTimelineEventType, ChronosTimestampPrecision
from tests.domain._fake_uow import InMemoryUnitOfWork


def _make_uow(fields: dict) -> tuple[InMemoryUnitOfWork, object]:
    uow = InMemoryUnitOfWork()
    event_id = uuid4()
    uow._store.events[event_id] = AccidentEvent(id=event_id)
    uow._store.projections[event_id] = ProjectedAccidentRecord(event_id=event_id, fields=fields)
    return uow, event_id


@pytest.mark.asyncio
async def test_extracts_impact_time_from_accident_time():
    uow, event_id = _make_uow({"accident_time": "2023-06-15T08:47:00"})
    result = await ExtractChronosTimelineFromEvent(uow).execute(event_id)
    assert result.timeline_events_created_count == 1
    events = await uow.chronos_timeline_events.list_for_accident_event(event_id)
    assert len(events) == 1
    assert events[0].event_type == ChronosTimelineEventType.IMPACT
    assert events[0].raw_value == "2023-06-15T08:47:00"


@pytest.mark.asyncio
async def test_extracts_takeoff_and_impact_and_creates_link():
    uow, event_id = _make_uow(
        {
            "takeoff_time": "2023-06-15T08:30:00",
            "accident_time": "2023-06-15T08:47:00",
        }
    )
    result = await ExtractChronosTimelineFromEvent(uow).execute(event_id)
    assert result.timeline_events_created_count == 2
    assert result.event_links_created_count == 1
    links = await uow.chronos_event_links.list_for_accident_event(event_id)
    assert len(links) == 1
    assert links[0].relationship_type == "ORDERED_BEFORE"


@pytest.mark.asyncio
async def test_missing_fields_do_not_fail():
    uow, event_id = _make_uow({})
    result = await ExtractChronosTimelineFromEvent(uow).execute(event_id)
    assert result.timeline_events_created_count == 0
    assert result.event_links_created_count == 0


@pytest.mark.asyncio
async def test_disputed_fields_are_skipped():
    from atlas.domain.constants import DISPUTED_MARKER

    uow, event_id = _make_uow({"accident_time": DISPUTED_MARKER})
    result = await ExtractChronosTimelineFromEvent(uow).execute(event_id)
    assert result.timeline_events_created_count == 0


@pytest.mark.asyncio
async def test_extraction_is_idempotent():
    uow, event_id = _make_uow({"accident_time": "2023-06-15T08:47:00"})
    uc = ExtractChronosTimelineFromEvent(uow)
    r1 = await uc.execute(event_id)
    r2 = await uc.execute(event_id)
    assert r1.timeline_events_created_count == 1
    assert r2.timeline_events_created_count == 0
    assert r2.timeline_events_reused_count == 1
    events = await uow.chronos_timeline_events.list_for_accident_event(event_id)
    assert len(events) == 1


@pytest.mark.asyncio
async def test_parses_date_only_as_day_precision():
    uow, event_id = _make_uow({"investigation_start_date": "2023-06-16"})
    await ExtractChronosTimelineFromEvent(uow).execute(event_id)
    events = await uow.chronos_timeline_events.list_for_accident_event(event_id)
    assert len(events) == 1
    assert events[0].timestamp_precision == ChronosTimestampPrecision.DAY
    assert events[0].occurred_at is not None
    assert events[0].occurred_at.year == 2023


@pytest.mark.asyncio
async def test_parses_iso_datetime_as_exact_or_minute():
    from atlas.domain.services.chronos_time_parser import parse_chronos_timestamp

    _, prec_exact = parse_chronos_timestamp("2023-06-15T08:47:33")
    assert prec_exact == ChronosTimestampPrecision.EXACT
    _, prec_minute = parse_chronos_timestamp("2023-06-15T08:47:00")
    assert prec_minute == ChronosTimestampPrecision.MINUTE


@pytest.mark.asyncio
async def test_creates_sequence_review_when_timestamps_conflict():
    uow, event_id = _make_uow(
        {
            "takeoff_time": "2023-06-15T09:00:00",
            "accident_time": "2023-06-15T08:47:00",
        }
    )
    await ExtractChronosTimelineFromEvent(uow).execute(event_id)
    await ExtractChronosTimelineFromEvent(uow).execute(event_id)
    reviews = await uow.chronos_sequence_reviews.list_pending()
    assert len(reviews) == 1
    assert "conflicts" in reviews[0].reason.lower()


@pytest.mark.asyncio
async def test_claim_provenance_only_attached_when_value_matches():
    from atlas.domain.entities import Claim
    from atlas.domain.enums import ClaimType

    uow, event_id = _make_uow({"accident_time": "2023-06-15T08:47:00"})

    matching_claim = Claim(
        event_id=event_id,
        source_id=uuid4(),
        field_name="accident_time",
        field_value="2023-06-15T08:47:00",
        claim_type=ClaimType.CONFIRMED,
    )
    non_matching_claim = Claim(
        event_id=event_id,
        source_id=uuid4(),
        field_name="accident_time",
        field_value="2023-06-15T10:00:00",
        claim_type=ClaimType.MANUAL_OVERRIDE,
    )
    uow._store.claims[matching_claim.id] = matching_claim
    uow._store.claims[non_matching_claim.id] = non_matching_claim

    await ExtractChronosTimelineFromEvent(uow).execute(event_id)
    events = await uow.chronos_timeline_events.list_for_accident_event(event_id)
    assert len(events) == 1
    assert events[0].source_claim_id == matching_claim.id


@pytest.mark.asyncio
async def test_claim_provenance_uses_source_reliability_tier():
    from datetime import UTC, datetime

    from atlas.domain.entities import Claim, Source
    from atlas.domain.enums import ClaimType, SourceKind

    uow, event_id = _make_uow({"accident_time": "2023-06-15T08:47:00"})

    low_trust = Source(name="low", kind=SourceKind.EXTERNAL, reliability_tier=5)
    high_trust = Source(name="high", kind=SourceKind.EXTERNAL, reliability_tier=1)
    uow._store.sources[low_trust.id] = low_trust
    uow._store.sources[high_trust.id] = high_trust

    older_low_trust_claim = Claim(
        event_id=event_id,
        source_id=low_trust.id,
        field_name="accident_time",
        field_value="2023-06-15T08:47:00",
        claim_type=ClaimType.RAW,
        created_at=datetime(2023, 1, 1, tzinfo=UTC),
    )
    newer_high_trust_claim = Claim(
        event_id=event_id,
        source_id=high_trust.id,
        field_name="accident_time",
        field_value="2023-06-15T08:47:00",
        claim_type=ClaimType.RAW,
        created_at=datetime(2023, 1, 2, tzinfo=UTC),
    )
    uow._store.claims[older_low_trust_claim.id] = older_low_trust_claim
    uow._store.claims[newer_high_trust_claim.id] = newer_high_trust_claim

    await ExtractChronosTimelineFromEvent(uow).execute(event_id)
    events = await uow.chronos_timeline_events.list_for_accident_event(event_id)
    assert len(events) == 1
    assert events[0].source_claim_id == newer_high_trust_claim.id
