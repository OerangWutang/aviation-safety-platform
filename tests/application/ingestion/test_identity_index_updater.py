from __future__ import annotations

from uuid import uuid4

from atlas.application.ingestion._identity_index_updater import _build_identity_entry


def test_build_identity_entry_includes_multi_aircraft_registration_aliases():
    incoming_fields = {
        "event_date": "2024-01-01",
        "registration": "N111AA",
        "aircraft_registration_numbers": ["N111AA", "N-222BB", "", None, "N 222BB"],
    }
    entry = _build_identity_entry(
        event_id=uuid4(),
        incoming_fields=incoming_fields,
        source_record_id="REC-1",
    )
    assert entry.registration_norm == "n111aa"
    assert entry.registration_norms == ["n111aa", "n222bb"]
    assert entry.fields["registration"] == "n111aa"
    assert entry.fields["registration_norms"] == ["n222bb"]


def test_build_identity_entry_accepts_aircraft_registration_numbers_without_primary_registration():
    incoming_fields = {
        "event_date": "2024-01-01",
        "aircraft_registration_numbers": ["N111AA", "N222BB"],
    }
    entry = _build_identity_entry(
        event_id=uuid4(),
        incoming_fields=incoming_fields,
        source_record_id="REC-2",
    )
    assert entry.registration_norm is None
    assert entry.registration_norms == ["n111aa", "n222bb"]
    assert entry.fields["registration_norms"] == ["n111aa", "n222bb"]
