"""Unit tests for the source-specific normalization pipeline."""

import pytest

from atlas.domain.enums import SourceKind
from atlas.domain.services.ingestion import (
    ExternalSourceNormalizer,
    NormalizationError,
    SourceNormalizer,
    SourceNormalizerRegistry,
    coerce_date,
    coerce_non_negative_int,
    coerce_string,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2024-01-15", "2024-01-15"),
        ("2024/01/15", "2024-01-15"),
        ("2024.01.15", "2024-01-15"),
        ("2024-01-15T00:00:00", "2024-01-15"),
        (None, None),
    ],
)
def test_coerce_date(raw, expected):
    assert coerce_date(raw) == expected


def test_coerce_date_raises_on_unparseable_string():
    with pytest.raises(NormalizationError):
        coerce_date("not-a-date")


def test_coerce_date_raises_on_partial_date():
    with pytest.raises(NormalizationError):
        coerce_date("2024-99")


@pytest.mark.parametrize("raw", ["06/03/2024", "15/01/2024", "15-01-2024"])
def test_coerce_date_rejects_year_last_ambiguous_dates(raw):
    with pytest.raises(NormalizationError, match="Ambiguous date"):
        coerce_date(raw)


def test_normalizer_rejects_identity_critical_normalization_error(caplog):
    normalizer = ExternalSourceNormalizer()
    claims = [{"field_name": "event_date", "field_value": "not-a-date"}]
    with pytest.raises(NormalizationError):
        normalizer.normalize(claims, source_kind=SourceKind.EXTERNAL)
    assert "Identity-critical normalization failed" in caplog.text


def test_normalizer_keeps_non_identity_raw_value_on_normalization_error(caplog):
    normalizer = ExternalSourceNormalizer()
    claims = [{"field_name": "fatalities_total", "field_value": "not-a-number"}]
    assert normalizer.normalize(claims, source_kind=SourceKind.EXTERNAL) == claims
    assert "Normalization failed" in caplog.text


def test_normalizer_preserves_canonical_alias_name_on_non_identity_error(caplog):
    normalizer = ExternalSourceNormalizer()
    claims = [{"field_name": "fatalities", "field_value": "unknown"}]

    result = normalizer.normalize(claims, source_kind=SourceKind.EXTERNAL)

    assert result == [{"field_name": "fatalities_total", "field_value": "unknown"}]
    assert "Normalization failed" in caplog.text


@pytest.mark.parametrize(("raw", "expected"), [(5, 5), ("5", 5), ("5.0", 5), (0, 0), (None, None)])
def test_coerce_non_negative_int_valid(raw, expected):
    assert coerce_non_negative_int(raw) == expected


def test_coerce_non_negative_int_rejects_negative():
    with pytest.raises(NormalizationError):
        coerce_non_negative_int(-1)


def test_coerce_non_negative_int_rejects_garbage():
    with pytest.raises(NormalizationError):
        coerce_non_negative_int("abc")


def test_coerce_string_collapses_whitespace():
    assert coerce_string("  Amsterdam   Schiphol  ") == "Amsterdam Schiphol"


def test_coerce_string_returns_none_for_blank():
    assert coerce_string("   ") is None


def test_normalizer_coerces_known_fields():
    normalizer = ExternalSourceNormalizer()
    claims = [
        {"field_name": "fatalities_total", "field_value": "5"},
        {"field_name": "event_date", "field_value": "2024/01/15"},
        {"field_name": "location", "field_value": "  New York  "},
    ]
    result = normalizer.normalize(claims)
    assert result[0]["field_value"] == 5
    assert result[1]["field_value"] == "2024-01-15"
    assert result[2]["field_value"] == "New York"


def test_normalizer_passes_through_unknown_fields():
    normalizer = ExternalSourceNormalizer()
    claims = [{"field_name": "custom_field", "field_value": "anything"}]
    assert normalizer.normalize(claims) == claims


def test_registry_returns_external_normalizer():
    registry = SourceNormalizerRegistry()
    assert isinstance(registry.get(SourceKind.EXTERNAL), ExternalSourceNormalizer)


def test_registry_accepts_source_kind_strings():
    registry = SourceNormalizerRegistry()
    assert isinstance(registry.get(SourceKind.EXTERNAL.value), ExternalSourceNormalizer)
    assert isinstance(registry.get("external"), ExternalSourceNormalizer)


def test_registry_custom_registration():
    registry = SourceNormalizerRegistry()
    custom = SourceNormalizer()

    registry.register(SourceKind.INTERNAL, custom)

    assert registry.get(SourceKind.INTERNAL) is custom


def test_registry_custom_registration_accepts_lowercase_string():
    registry = SourceNormalizerRegistry()
    custom = SourceNormalizer()

    registry.register("internal", custom)

    assert registry.get(SourceKind.INTERNAL) is custom


def test_normalizer_registration_is_uppercased():
    normalizer = ExternalSourceNormalizer()
    result = normalizer.normalize([{"field_name": "registration", "field_value": "ph-bxo"}])
    assert result[0]["field_value"] == "PH-BXO"


def test_normalizer_registration_none_stays_none():
    normalizer = ExternalSourceNormalizer()
    result = normalizer.normalize([{"field_name": "registration", "field_value": None}])
    assert result[0]["field_value"] is None


def test_normalizer_registration_blank_stays_none():
    normalizer = ExternalSourceNormalizer()
    result = normalizer.normalize([{"field_name": "registration", "field_value": "   "}])
    assert result[0]["field_value"] is None


def test_coerce_non_negative_int_rejects_fractional_value():
    with pytest.raises(NormalizationError):
        coerce_non_negative_int("1.7")


def test_normalizer_flight_phase_is_lowercased():
    normalizer = ExternalSourceNormalizer()
    claims = [{"field_name": "flight_phase", "field_value": "  Cruise  "}]
    result = normalizer.normalize(claims)
    assert result[0]["field_value"] == "cruise"


def test_external_normalizer_maps_common_field_aliases():
    normalizer = ExternalSourceNormalizer()
    claims = normalizer.normalize(
        [
            {"field_name": "tail_number", "field_value": "ph-bxo"},
            {"field_name": "accident_date", "field_value": "2024/01/05"},
            {"field_name": "airline_name", "field_value": "  Example   Air "},
        ]
    )
    by_field = {claim["field_name"]: claim["field_value"] for claim in claims}
    assert by_field["registration"] == "PH-BXO"
    assert by_field["event_date"] == "2024-01-05"
    assert by_field["operator"] == "Example Air"


def test_external_normalizer_maps_field_aliases_tolerantly():
    normalizer = ExternalSourceNormalizer()
    claims = normalizer.normalize(
        [
            {"field_name": "Tail Number", "field_value": "ph-bxo"},
            {"field_name": "aircraftRegistration", "field_value": "n-123"},
            {"field_name": "operator-name", "field_value": "  Example Air "},
            {"field_name": "Aircraft Model", "field_value": "  A320 "},
        ]
    )

    by_field = {claim["field_name"]: claim["field_value"] for claim in claims}
    assert by_field["registration"] == "N-123"
    assert by_field["operator"] == "Example Air"
    assert by_field["aircraft_type"] == "A320"


def test_external_normalizer_rejects_present_blank_event_date():
    normalizer = ExternalSourceNormalizer()
    with pytest.raises(NormalizationError, match="event_date was present but blank"):
        normalizer.normalize([{"field_name": "event_date", "field_value": "   "}])


def test_claim_writer_accepts_injected_normalizer_registry():
    from atlas.application.ingestion._claim_writer import ClaimWriter
    from atlas.domain.enums import SourceKind
    from tests.domain._fake_uow import InMemoryUnitOfWork

    class CustomNormalizer(SourceNormalizer):
        def normalize(self, claims, **kwargs):
            return [{**claim, "field_name": "customised"} for claim in claims]

    registry = SourceNormalizerRegistry()
    registry.register(SourceKind.EXTERNAL, CustomNormalizer())

    writer = ClaimWriter(InMemoryUnitOfWork(), normalizer_registry=registry)
    assert writer.normalise_claims(
        SourceKind.EXTERNAL.value,
        [{"field_name": "rawName", "field_value": 123}],
    ) == [{"field_name": "customised", "field_value": 123}]


def test_plain_date_is_not_globally_mapped_to_event_date():
    normalizer = ExternalSourceNormalizer()
    result = normalizer.normalize(
        [
            {"field_name": "date", "field_value": "2024/01/05"},
        ]
    )

    assert result == [{"field_name": "date", "field_value": "2024/01/05"}]


def test_source_specific_mapper_can_map_plain_date_to_event_date():
    from uuid import uuid4

    registry = SourceNormalizerRegistry()
    source_id = uuid4()
    registry.register_source_mapper(source_id, {"date": "event_date"})

    result = registry.normalize(
        SourceKind.EXTERNAL,
        [{"field_name": "date", "field_value": "2024/01/05"}],
        source_id=source_id,
    )

    assert result == [{"field_name": "event_date", "field_value": "2024-01-05"}]


def test_source_field_mapper_validates_targets():
    from atlas.domain.services.ingestion import SourceFieldMapper

    with pytest.raises(ValueError, match="Unknown canonical field mapping target"):
        SourceFieldMapper({"date": "event_dat"})


def test_source_field_mapper_normalizes_target_spellings():
    from atlas.domain.services.ingestion import SourceFieldMapper

    mapper = SourceFieldMapper({"date": "eventDate"})
    assert mapper.map_field_name("date") == "event_date"


def test_durable_source_mapping_overrides_registered_mapper():
    from uuid import uuid4

    registry = SourceNormalizerRegistry()
    source_id = uuid4()
    registry.register_source_mapper(source_id, {"date": "narrative"})

    result = registry.normalize(
        SourceKind.EXTERNAL,
        [{"field_name": "date", "field_value": "2024/01/05"}],
        source_id=source_id,
        source_field_mapping={"date": "event_date"},
    )

    assert result == [{"field_name": "event_date", "field_value": "2024-01-05"}]
