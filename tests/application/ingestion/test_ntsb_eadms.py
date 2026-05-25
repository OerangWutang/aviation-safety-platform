"""Unit tests for the NTSB eADMS mapping core.

These exercise the pure ``application`` mapping with hand-built rows - no Access,
no CSV, no database - so they are fast and deterministic.  The contract under
test is exactly what ``IngestSourceData`` consumes: a list of
``IngestionClaimDTO`` plus a stable ``source_record_id`` and a content-addressed
idempotency key.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from atlas.application.ingestion.sources.ntsb_eadms import (
    EadmsCodeDecoder,
    build_event_record,
    build_finding_items,
)

FIXED_CAPTURE = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.fixture
def decoder() -> EadmsCodeDecoder:
    return EadmsCodeDecoder.from_dictionary_rows(
        [
            {"Table": "events", "Column": "ev_type", "code_iaids": "ACC", "meaning": "Accident"},
            {
                "Table": "events",
                "Column": "ev_highest_injury",
                "code_iaids": "FATL",
                "meaning": "Fatal",
            },
            {"Table": "aircraft", "Column": "damage", "code_iaids": "DEST", "meaning": "Destroyed"},
            {
                "Table": "aircraft",
                "Column": "far_part",
                "code_iaids": "091",
                "meaning": "Part 91: General Aviation",
            },
        ]
    )


@pytest.fixture
def event_row() -> dict:
    return {
        "ev_id": "20240101X00001",
        "ntsb_no": "ABC24LA001",
        "ev_type": "ACC",
        "ev_date": "01/01/2024 14:30:00",
        "ev_time": "1430",
        "ev_tmzn": "EST",
        "ev_city": "Springfield",
        "ev_state": "IL",
        "ev_country": "USA",
        "latitude": "39.8017",
        "longitude": "-89.6437",
        "ev_highest_injury": "FATL",
        "inj_tot_f": "2",
        "inj_tot_n": "0",
        "light_cond": "DAYL",  # unknown code -> passthrough
    }


@pytest.fixture
def aircraft_rows() -> list[dict]:
    return [
        {
            "ev_id": "20240101X00001",
            "Aircraft_Key": "1",
            "regis_no": "N12345",
            "acft_make": "CESSNA",
            "acft_model": "172",
            "damage": "DEST",
            "far_part": "091",
            "homebuilt": "N",
            "num_eng": "1",
        },
    ]


def _claims_dict(record) -> dict:
    return {c.field_name: c.field_value for c in record.claims}


def test_maps_core_fields_and_decodes_codes(decoder, event_row, aircraft_rows):
    record = build_event_record(
        event_row=event_row,
        aircraft_rows=aircraft_rows,
        narrative_rows=[],
        finding_rows=[],
        decoder=decoder,
        captured_at=FIXED_CAPTURE,
    )
    assert record is not None
    d = _claims_dict(record)
    assert record.source_record_id == "20240101X00001"
    assert d["event_type"] == "Accident"  # decoded
    assert d["aircraft_damage"] == "Destroyed"  # decoded
    assert d["far_part"] == "Part 91: General Aviation"
    assert d["highest_injury_level"] == "Fatal"
    assert d["occurred_on"] == "2024-01-01"
    assert d["occurred_time_local"] == "14:30"
    assert d["latitude"] == pytest.approx(39.8017)
    assert d["fatalities_total"] == 2
    assert d["aircraft_make"] == "CESSNA"
    assert d["amateur_built"] is False


def test_unknown_code_passes_through(decoder, event_row, aircraft_rows):
    record = build_event_record(
        event_row=event_row,
        aircraft_rows=aircraft_rows,
        narrative_rows=[],
        finding_rows=[],
        decoder=decoder,
        captured_at=FIXED_CAPTURE,
    )
    # 'DAYL' is not in the test dictionary; we keep the raw code, never drop it.
    assert _claims_dict(record)["light_condition"] == "DAYL"


def test_blank_and_none_values_are_dropped(decoder):
    record = build_event_record(
        event_row={"ev_id": "X", "ev_city": "  ", "inj_tot_f": "", "ev_state": "TX"},
        aircraft_rows=[],
        narrative_rows=[],
        finding_rows=[],
        decoder=decoder,
        captured_at=FIXED_CAPTURE,
    )
    names = {c.field_name for c in record.claims}
    assert "location_city" not in names  # blank dropped
    assert "fatalities_total" not in names  # empty dropped
    assert "location_state" in names  # real value kept


def test_no_duplicate_field_names_with_multiple_findings(decoder, event_row, aircraft_rows):
    findings = [
        {"finding_code": "01", "finding_description": "a", "Cause_Factor": "C", "finding_no": "1"},
        {"finding_code": "02", "finding_description": "b", "Cause_Factor": "F", "finding_no": "2"},
        {"finding_code": "03", "finding_description": "c", "Cause_Factor": "C", "finding_no": "3"},
    ]
    record = build_event_record(
        event_row=event_row,
        aircraft_rows=aircraft_rows,
        narrative_rows=[],
        finding_rows=findings,
        decoder=decoder,
        captured_at=FIXED_CAPTURE,
    )
    names = [c.field_name for c in record.claims]
    assert len(names) == len(set(names)), "duplicate field names would be rejected by ingestion"
    d = _claims_dict(record)
    assert isinstance(d["causal_findings"], list) and len(d["causal_findings"]) == 3


def test_findings_ordered_causes_before_factors():
    items = build_finding_items(
        [
            {"finding_code": "f1", "Cause_Factor": "F", "finding_description": "factor"},
            {"finding_code": "c1", "Cause_Factor": "C", "finding_description": "cause"},
            {"finding_code": "u1", "Cause_Factor": "", "finding_description": "unspec"},
        ]
    )
    assert [it["role"] for it in items] == ["CAUSE", "FACTOR", "UNSPECIFIED"]


def test_findings_never_emit_synthetic_probability():
    items = build_finding_items(
        [{"finding_code": "c1", "Cause_Factor": "C", "finding_description": "x"}]
    )
    # Epistemic guard: only the Board's recorded role, never a numeric weight.
    assert set(items[0].keys()) == {
        "finding_code",
        "description",
        "category_no",
        "subcategory_no",
        "role",
    }
    assert not any(isinstance(v, float) for v in items[0].values())


def test_idempotency_key_is_deterministic_and_content_addressed(decoder, event_row, aircraft_rows):
    a = build_event_record(
        event_row=event_row,
        aircraft_rows=aircraft_rows,
        narrative_rows=[],
        finding_rows=[],
        decoder=decoder,
        captured_at=FIXED_CAPTURE,
    )
    # Same content, *different* capture time -> identical key (capture time excluded).
    b = build_event_record(
        event_row=event_row,
        aircraft_rows=aircraft_rows,
        narrative_rows=[],
        finding_rows=[],
        decoder=decoder,
        captured_at=datetime(2030, 5, 5, tzinfo=UTC),
    )
    assert a.idempotency_key == b.idempotency_key
    assert a.idempotency_key.startswith("ntsb-eadms:20240101X00001:")

    # Changed content -> different key (so it becomes a new submission that still
    # attaches to the same event via source_record_id).
    changed = dict(event_row, inj_tot_f="5")
    c = build_event_record(
        event_row=changed,
        aircraft_rows=aircraft_rows,
        narrative_rows=[],
        finding_rows=[],
        decoder=decoder,
        captured_at=FIXED_CAPTURE,
    )
    assert c.idempotency_key != a.idempotency_key
    assert c.source_record_id == a.source_record_id  # same record, updated data


def test_missing_ev_id_yields_none(decoder):
    assert (
        build_event_record(
            event_row={"ev_city": "Nowhere"},
            aircraft_rows=[],
            narrative_rows=[],
            finding_rows=[],
            decoder=decoder,
        )
        is None
    )


def test_primary_aircraft_is_lowest_key(decoder, event_row):
    rows = [
        {"ev_id": "x", "Aircraft_Key": "2", "acft_make": "SECOND"},
        {"ev_id": "x", "Aircraft_Key": "1", "acft_make": "FIRST"},
    ]
    record = build_event_record(
        event_row=event_row,
        aircraft_rows=rows,
        narrative_rows=[],
        finding_rows=[],
        decoder=decoder,
        captured_at=FIXED_CAPTURE,
    )
    assert _claims_dict(record)["aircraft_make"] == "FIRST"
    # but the raw payload preserves both aircraft for audit/multi-aircraft work
    assert len(record.raw_payload["aircraft"]) == 2


def test_raw_payload_preserves_source_verbatim(decoder, event_row, aircraft_rows):
    record = build_event_record(
        event_row=event_row,
        aircraft_rows=aircraft_rows,
        narrative_rows=[],
        finding_rows=[],
        decoder=decoder,
        captured_at=FIXED_CAPTURE,
    )
    assert record.raw_payload["event"]["ev_type"] == "ACC"  # un-decoded original kept
    assert record.raw_payload["schema"] == "ntsb-eadms"


def test_multi_aircraft_registrations_are_emitted_as_structured_claim(decoder, event_row):
    aircraft_rows = [
        {"Aircraft_Key": "2", "regis_no": "N222BB", "acft_make": "SECOND"},
        {"Aircraft_Key": "1", "regis_no": "N111AA", "acft_make": "FIRST"},
    ]
    record = build_event_record(
        event_row=event_row,
        aircraft_rows=aircraft_rows,
        narrative_rows=[],
        finding_rows=[],
        decoder=decoder,
        captured_at=FIXED_CAPTURE,
    )
    assert record is not None
    d = _claims_dict(record)
    assert d["registration_number"] == "N111AA"
    assert d["aircraft_registration_numbers"] == ["N111AA", "N222BB"]
    assert d["aircraft_make"] == "FIRST"
    assert record.raw_payload["schema_version"] == 2
    assert len(record.raw_payload["aircraft"]) == 2


def test_multi_aircraft_registration_claim_omits_blank_null_and_duplicate_values(
    decoder, event_row
):
    aircraft_rows = [
        {"Aircraft_Key": "1", "regis_no": "N111AA"},
        {"Aircraft_Key": "2", "regis_no": " "},
        {"Aircraft_Key": "3", "regis_no": None},
        {"Aircraft_Key": "4", "regis_no": "N-111AA"},
        {"Aircraft_Key": "5", "regis_no": "N222BB"},
    ]
    record = build_event_record(
        event_row=event_row,
        aircraft_rows=aircraft_rows,
        narrative_rows=[],
        finding_rows=[],
        decoder=decoder,
        captured_at=FIXED_CAPTURE,
    )
    assert record is not None
    d = _claims_dict(record)
    assert d["aircraft_registration_numbers"] == ["N111AA", "N222BB"]


def test_aircraft_registration_numbers_claim_absent_when_no_registrations_exist(decoder, event_row):
    aircraft_rows = [
        {"Aircraft_Key": "1", "regis_no": None},
        {"Aircraft_Key": "2", "regis_no": " "},
    ]
    record = build_event_record(
        event_row=event_row,
        aircraft_rows=aircraft_rows,
        narrative_rows=[],
        finding_rows=[],
        decoder=decoder,
        captured_at=FIXED_CAPTURE,
    )
    assert record is not None
    d = _claims_dict(record)
    assert "registration_number" not in d
    assert "aircraft_registration_numbers" not in d
