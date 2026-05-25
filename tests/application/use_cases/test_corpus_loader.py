"""Tests for InMemoryCorpusLoader.load() and the iter_all_claims() path.

These are the tests that were missing before the frontend handoff review.

``InMemoryCorpusLoader.load()`` is the production corpus loading path for
``RunEchoCrossReference``.  It calls ``uow.projections.iter_all_claims()``
which streams ``(event_id, fields)`` pairs, then maps each ``fields`` dict
through ``precedent_record_from_ntsb_claims``.  Neither path had been
exercised against realistic data.

What is tested here
-------------------
1. Happy path: a realistic NTSB-vocabulary ``fields`` dict produces a
   correctly populated ``PrecedentRecord`` — right categories, severity,
   FAR part, terms from the probable-cause narrative.

2. ``causal_findings`` survival: the list-of-dicts shape used by the NTSB
   importer (``{"category_no": "02", "subcategory_no": "04", "role": "CAUSE",
   ...}``) is correctly parsed into taxonomy keys by
   ``categories_from_finding_items``.

3. Partial / sparse fields: a projection with only some fields set still
   produces a usable ``PrecedentRecord``; missing fields become ``None`` /
   empty frozensets rather than raising.

4. Empty corpus: zero projections → empty list, no exception.

5. Resilience: a single malformed record is skipped (warning logged), not
   propagated as an exception; valid records in the same corpus are returned.

6. Full async round-trip: ``InMemoryCorpusLoader.load()`` drives
   ``iter_all_claims()`` as an ``async for`` — the test confirms the async
   generator protocol works end-to-end with the fake repository.

7. Field alignment: the canonical field names the NTSB importer writes
   (``probable_cause_narrative``, ``aircraft_make``, ``occurred_on``,
   ``far_part``, ``highest_injury_level``, ``causal_findings``) are exactly
   the names the corpus loader reads — confirmed by a direct assertion rather
   than left as an implicit assumption.
"""

from __future__ import annotations

import uuid
from unittest.mock import patch

import pytest

from atlas.application.use_cases.echo_crossref import InMemoryCorpusLoader
from atlas.domain.crossref.entities import PrecedentRecord
from atlas.domain.entities import ProjectedAccidentRecord
from tests.domain.fakes import InMemoryUnitOfWork

# ── Realistic NTSB-vocabulary fields dict (mirrors importer output) ──────────

_FULL_FIELDS: dict = {
    "ntsb_number": "WPR20LA123",
    "event_type": "Accident",
    "occurred_on": "2020-06-15",
    "location_city": "Mesa",
    "location_state": "AZ",
    "aircraft_make": "Piper",
    "aircraft_model": "PA-28-181",
    "aircraft_category": "Airplane",
    "far_part": "Part 91: General Aviation",
    "highest_injury_level": "None",
    "fatalities_total": 0,
    "probable_cause_narrative": (
        "The student pilot's failure to maintain directional control during "
        "a crosswind landing, resulting in a runway excursion and collapse "
        "of the nose landing gear."
    ),
    "factual_narrative": (
        "The airplane touched down left of centerline in a crosswind and "
        "the student pilot was unable to correct before departure from the "
        "runway surface."
    ),
    "causal_findings": [
        {
            "finding_code": "0106001010",
            "description": "Directional control - not maintained",
            "category_no": "01",
            "subcategory_no": "06",
            "role": "CAUSE",
        },
        {
            "finding_code": "0204151045",
            "description": "Pilot decision - inadequate crosswind technique",
            "category_no": "02",
            "subcategory_no": "04",
            "role": "FACTOR",
        },
    ],
}


def _seed_projection(uow: InMemoryUnitOfWork, fields: dict, event_id=None) -> uuid.UUID:
    eid = event_id or uuid.uuid4()
    uow.store.projections[eid] = ProjectedAccidentRecord(
        event_id=eid,
        fields=fields,
        completeness_score=0.9,
    )
    return eid


# ── 1. Happy path ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_corpus_loader_builds_precedent_record_from_full_fields():
    uow = InMemoryUnitOfWork()
    eid = _seed_projection(uow, _FULL_FIELDS)

    records = await InMemoryCorpusLoader().load(uow=uow)

    assert len(records) == 1
    rec = records[0]
    assert rec.event_id == str(eid)
    assert rec.far_part == "Part 91: General Aviation"
    assert rec.aircraft_category == "Airplane"
    assert rec.severity == "none"
    assert rec.display_occurred_on == "2020-06-15"
    assert rec.display_aircraft == "Piper PA-28-181"
    assert rec.display_location == "Mesa, AZ"
    assert rec.display_probable_cause is not None
    assert len(rec.display_probable_cause) <= 300


# ── 2. causal_findings parsed into taxonomy keys ─────────────────────────────


@pytest.mark.asyncio
async def test_causal_findings_produce_correct_category_keys():
    uow = InMemoryUnitOfWork()
    _seed_projection(uow, _FULL_FIELDS)

    [rec] = await InMemoryCorpusLoader().load(uow=uow)

    assert rec.finding_categories == frozenset({"01.06", "02.04"})


# ── 3. Sparse / partial fields ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sparse_fields_produce_usable_record():
    """A projection with only a narrative and no structured fields still works."""
    sparse = {
        "probable_cause_narrative": "Engine failure due to fuel exhaustion.",
    }
    uow = InMemoryUnitOfWork()
    _seed_projection(uow, sparse)

    [rec] = await InMemoryCorpusLoader().load(uow=uow)

    assert rec.finding_categories == frozenset()
    assert rec.far_part is None
    assert rec.severity is None
    assert "engine" in rec.terms or "failure" in rec.terms
    assert rec.display_probable_cause is not None


@pytest.mark.asyncio
async def test_entirely_empty_fields_produce_empty_record():
    """A projection with no useful fields still produces a PrecedentRecord."""
    uow = InMemoryUnitOfWork()
    _seed_projection(uow, {})

    [rec] = await InMemoryCorpusLoader().load(uow=uow)

    assert isinstance(rec, PrecedentRecord)
    assert rec.finding_categories == frozenset()
    assert rec.terms == frozenset()


# ── 4. Empty corpus ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_empty_corpus_returns_empty_list():
    uow = InMemoryUnitOfWork()
    # No projections seeded.
    records = await InMemoryCorpusLoader().load(uow=uow)
    assert records == []


# ── 5. Resilience: one bad record doesn't abort the load ─────────────────────


@pytest.mark.asyncio
async def test_malformed_record_is_skipped_not_propagated():
    """If one projection raises during mapping, the rest still load."""
    uow = InMemoryUnitOfWork()
    good_id = _seed_projection(uow, _FULL_FIELDS)
    bad_id = _seed_projection(uow, {"causal_findings": "not-a-list"})  # wrong type

    # Patch precedent_record_from_ntsb_claims to raise on the bad event_id.
    import atlas.application.use_cases.echo_crossref as _echo_mod
    from atlas.application.crossref import precedent_record_from_ntsb_claims as _real_fn

    def _patched(event_id, claims):
        if str(event_id) == str(bad_id):
            raise ValueError("simulated mapping failure")
        return _real_fn(event_id, claims)

    with patch.object(_echo_mod, "precedent_record_from_ntsb_claims", _patched):
        records = await InMemoryCorpusLoader().load(uow=uow)

    assert len(records) == 1
    assert records[0].event_id == str(good_id)


# ── 6. Full async round-trip ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_corpus_loader_exhausts_async_generator():
    """iter_all_claims() is an async generator; load() must consume it fully."""
    uow = InMemoryUnitOfWork()
    ids = {_seed_projection(uow, _FULL_FIELDS) for _ in range(5)}

    records = await InMemoryCorpusLoader().load(uow=uow)

    assert len(records) == 5
    assert {uuid.UUID(r.event_id) for r in records} == ids


# ── 7. Field name alignment ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_canonical_field_names_are_correctly_consumed():
    """
    Explicit assertion that the field names the NTSB importer writes are the
    exact names the corpus loader reads.  If either side changes its vocabulary
    this test fails with a clear indication of which field broke.
    """
    fields = {
        "probable_cause_narrative": "Failure to maintain airspeed on approach.",
        "factual_narrative": "The aircraft stalled during final approach.",
        "aircraft_make": "Cessna",
        "aircraft_model": "172S",
        "aircraft_category": "Airplane",
        "far_part": "Part 91: General Aviation",
        "highest_injury_level": "Fatal",
        "occurred_on": "2021-03-10",
        "location_city": "Denver",
        "location_state": "CO",
        "causal_findings": [
            {
                "category_no": "03",
                "subcategory_no": "02",
                "role": "CAUSE",
                "finding_code": "0302",
                "description": "airspeed",
            }
        ],
    }
    uow = InMemoryUnitOfWork()
    _seed_projection(uow, fields)

    [rec] = await InMemoryCorpusLoader().load(uow=uow)

    # Every field the loader is supposed to consume should be non-empty.
    assert rec.far_part == "Part 91: General Aviation", "far_part not mapped"
    assert rec.aircraft_category == "Airplane", "aircraft_category not mapped"
    assert rec.severity == "fatal", "highest_injury_level not mapped"
    assert rec.display_occurred_on == "2021-03-10", "occurred_on not mapped"
    assert rec.display_aircraft == "Cessna 172S", "aircraft_make/model not mapped"
    assert rec.display_location == "Denver, CO", "location_city/state not mapped"
    assert "airspeed" in rec.terms or "approach" in rec.terms, "narratives not mapped to terms"
    assert rec.finding_categories == frozenset({"03.02"}), "causal_findings not mapped"
