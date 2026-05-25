"""Source-specific claim normalization pipeline."""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Mapping
from datetime import date, datetime
from typing import Any, ClassVar
from uuid import UUID

from atlas.domain.enums import RequiredField, SourceKind
from atlas.domain.exceptions import DomainValidationError

logger = logging.getLogger(__name__)


class NormalizationError(DomainValidationError):
    """Raised when a claim value cannot be normalized to the canonical type."""

    code = "NORMALIZATION_ERROR"


_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_YMD_DATE_RE = re.compile(r"^(\d{4})[/\.](\d{1,2})[/\.](\d{1,2})$")
_AMBIGUOUS_DMY_OR_MDY_RE = re.compile(r"^(\d{1,2})[/\.\-](\d{1,2})[/\.\-](\d{4})$")


def _validate_iso_date(text: str) -> str:
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError as exc:
        raise NormalizationError(f"Cannot convert {text!r} to ISO date") from exc


def coerce_date(value: Any) -> str | None:
    """Return an ISO-8601 date string (YYYY-MM-DD) or None.

    Accepted generic formats are deliberately limited to unambiguous shapes:
    - ``datetime`` / ``date`` objects
    - ISO-8601 strings (``YYYY-MM-DD`` or ``YYYY-MM-DDTHH:MM:SS[Z]``)
    - Year-first date strings using slash or dot separators (``YYYY/MM/DD`` or
      ``YYYY.MM.DD``)

    Ambiguous two-component year-last strings such as ``06/03/2024`` are
    rejected instead of silently choosing DD/MM/YYYY or MM/DD/YYYY.  Sources
    that use a year-last format must register a source-specific normaliser that
    converts the field to ISO before the generic coercer runs.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()

    text = str(value).strip()
    if not text:
        return None
    if _ISO_DATE_RE.match(text):
        return _validate_iso_date(text)

    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        pass

    match = _YMD_DATE_RE.match(text)
    if match:
        year, month, day = map(int, match.groups())
        try:
            return date(year, month, day).isoformat()
        except ValueError as exc:
            raise NormalizationError(f"Cannot convert {text!r} to ISO date") from exc

    if _AMBIGUOUS_DMY_OR_MDY_RE.match(text):
        raise NormalizationError(
            f"Ambiguous date {text!r}: register a source-specific normaliser "
            "that converts MM/DD/YYYY or DD/MM/YYYY to ISO YYYY-MM-DD before "
            "generic coercion."
        )

    raise NormalizationError(f"Cannot convert {text!r} to ISO date")


def coerce_non_negative_int(value: Any) -> int | None:
    """Return a non-negative integer or None.

    Rejects strings with a non-zero fractional part instead of silently
    truncating (for example, ``"1.7"`` raises ``NormalizationError``).
    """
    if value is None:
        return None
    text = str(value).strip()
    if "." in text:
        try:
            as_float = float(text)
        except ValueError as exc:
            raise NormalizationError(f"Cannot convert {value!r} to int") from exc
        if as_float != int(as_float):
            raise NormalizationError(
                f"Expected integer, got fractional value {value!r}. "
                "Round before ingestion if this is intentional."
            )
    try:
        result = int(float(text))
    except (ValueError, TypeError) as exc:
        raise NormalizationError(f"Cannot convert {value!r} to int") from exc
    if result < 0:
        raise NormalizationError(f"Expected non-negative integer, got {value!r}")
    return result


def coerce_string(value: Any) -> str | None:
    """Collapse whitespace and strip; return None for blank strings."""
    if value is None:
        return None
    cleaned = " ".join(str(value).strip().split())
    return cleaned or None


def coerce_registration(value: Any) -> str | None:
    """Normalize aircraft registration text (upper-cased, whitespace-collapsed)."""
    cleaned = coerce_string(value)
    return cleaned.upper() if cleaned else None


def coerce_flight_phase(value: Any) -> str | None:
    """Normalize flight phase to lower-case so casing variants compare equal."""
    cleaned = coerce_string(value)
    return cleaned.lower() if cleaned else None


def normalise_field_key(value: str) -> str:
    """Return a tolerant snake_case key for source field-name mapping.

    Real feeds use a mix of snake_case, camelCase, spaces, hyphens, and
    inconsistent capitalization.  Field-name mapping should not depend on an
    exact spelling such as ``tail_number`` when callers commonly send
    ``Tail Number`` or ``tailNumber``.
    """
    text = str(value).strip()
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", text)
    text = re.sub(r"[^0-9A-Za-z]+", "_", text)
    return text.lower().strip("_")


def _canonical_field_value(value: str | RequiredField) -> str:
    """Return a normalized canonical field target or raise on typos.

    Field-mapping configuration is allowed to translate raw source names only
    to Atlas' known canonical claim fields.  Unknown source fields can still be
    ingested unchanged without a mapping; a mapping target typo such as
    ``event_dat`` should fail loudly instead of bypassing identity-critical
    coercion.  Tolerant input such as ``eventDate`` is accepted and normalized
    to ``event_date``.
    """
    raw = value.value if isinstance(value, RequiredField) else str(value)
    key = normalise_field_key(raw)
    allowed = {field.value for field in RequiredField}
    if key not in allowed:
        raise ValueError(
            f"Unknown canonical field mapping target {value!r}; expected one of {sorted(allowed)}"
        )
    return key


class SourceFieldMapper:
    """Map a source field name onto an Atlas canonical field name.

    Mappers are intentionally separate from value normalizers.  A field name
    such as ``date`` is source-dependent: for one source it can mean accident
    date, while for another it can mean publication or scrape date.  Register a
    mapper per source when generic aliases are too broad.
    """

    def __init__(self, aliases: Mapping[str, str] | None = None) -> None:
        self._aliases = {
            normalise_field_key(raw): _canonical_field_value(canonical)
            for raw, canonical in (aliases or {}).items()
        }

    @property
    def aliases(self) -> dict[str, str]:
        """Return a normalized copy of the configured aliases."""
        return dict(self._aliases)

    def map_field_name(self, raw_field_name: str) -> str:
        key = normalise_field_key(raw_field_name)
        return self._aliases.get(key, key)


class GenericExternalFieldMapper(SourceFieldMapper):
    """Conservative generic aliases shared by external sources.

    Do not add ambiguous names like ``date`` here.  Those must be registered via
    a source-specific mapper so report/publication dates are not accidentally
    treated as identity-critical ``event_date`` values.
    """

    def __init__(self) -> None:
        super().__init__(
            {
                "accident_date": RequiredField.EVENT_DATE,
                "incident_date": RequiredField.EVENT_DATE,
                "eventdate": RequiredField.EVENT_DATE,
                "tail_number": RequiredField.REGISTRATION,
                "tail_num": RequiredField.REGISTRATION,
                "tailnumber": RequiredField.REGISTRATION,
                "aircraft_registration": RequiredField.REGISTRATION,
                "aircraftregistration": RequiredField.REGISTRATION,
                "registration_number": RequiredField.REGISTRATION,
                "registrationnumber": RequiredField.REGISTRATION,
                "reg": RequiredField.REGISTRATION,
                "airline_name": RequiredField.OPERATOR,
                "operator_name": RequiredField.OPERATOR,
                "operatorname": RequiredField.OPERATOR,
                "aircraft_model": RequiredField.AIRCRAFT_TYPE,
                "aircraftmodel": RequiredField.AIRCRAFT_TYPE,
                "aircraft_make_model": RequiredField.AIRCRAFT_TYPE,
                "fatalities": RequiredField.FATALITIES_TOTAL,
                "injuries": RequiredField.INJURIES_TOTAL,
            }
        )


class SourceNormalizer:
    """Identity normalizer - passes claims through unchanged."""

    def normalize(
        self,
        claims: list[dict[str, Any]],
        *,
        source_kind: str | SourceKind | None = None,
        source_id: UUID | None = None,
        ingestion_run_id: UUID | None = None,
        field_mapper: SourceFieldMapper | None = None,
    ) -> list[dict[str, Any]]:
        return claims


class ExternalSourceNormalizer(SourceNormalizer):
    """Apply field-name mapping and canonical coercions to known fields.

    Unknown fields keep their raw value but have their field name normalized to a
    tolerant snake_case key so spelling/casing variants do not fragment the
    claim model. Common *unambiguous* aliases are mapped by
    ``GenericExternalFieldMapper``; ambiguous aliases such as plain ``date``
    should be registered with ``SourceNormalizerRegistry.register_source_mapper``
    for specific feeds.
    """

    _field_coercers: ClassVar[dict[str, Callable[[Any], Any]]] = {
        RequiredField.EVENT_DATE: coerce_date,
        RequiredField.FATALITIES_TOTAL: coerce_non_negative_int,
        RequiredField.INJURIES_TOTAL: coerce_non_negative_int,
        RequiredField.LOCATION: coerce_string,
        RequiredField.AIRCRAFT_TYPE: coerce_string,
        RequiredField.OPERATOR: coerce_string,
        RequiredField.REGISTRATION: coerce_registration,
        RequiredField.FLIGHT_PHASE: coerce_flight_phase,
        RequiredField.NARRATIVE: coerce_string,
    }
    # Bad values in these fields directly affect event identity/matching.
    # Preserve-and-warn is acceptable for display/count fields, but accepting an
    # ambiguous event_date or malformed registration can silently create
    # duplicate events.
    _identity_critical_fields: ClassVar[set[str]] = {
        RequiredField.EVENT_DATE,
        RequiredField.REGISTRATION,
    }

    def __init__(self, default_field_mapper: SourceFieldMapper | None = None) -> None:
        self._default_field_mapper = default_field_mapper or GenericExternalFieldMapper()

    @staticmethod
    def _is_present_blank(value: Any) -> bool:
        return value is not None and str(value).strip() == ""

    def normalize(
        self,
        claims: list[dict[str, Any]],
        *,
        source_kind: str | SourceKind | None = None,
        source_id: UUID | None = None,
        ingestion_run_id: UUID | None = None,
        field_mapper: SourceFieldMapper | None = None,
    ) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        for claim in claims:
            raw_field_name = str(claim.get("field_name", ""))
            if field_mapper is not None:
                field_name = field_mapper.map_field_name(raw_field_name)
                # Source-specific mapping wins for configured aliases, but
                # unconfigured fields should still get conservative generic
                # aliases such as tailNumber -> registration.
                if field_name == normalise_field_key(raw_field_name):
                    field_name = self._default_field_mapper.map_field_name(raw_field_name)
            else:
                field_name = self._default_field_mapper.map_field_name(raw_field_name)
            coercer = self._field_coercers.get(field_name)
            if coercer is None:
                normalized.append({**claim, "field_name": field_name})
                continue
            if field_name == RequiredField.EVENT_DATE and self._is_present_blank(
                claim.get("field_value")
            ):
                raise NormalizationError(
                    "event_date was present but blank; omit the field when unknown "
                    "or provide an ISO YYYY-MM-DD value"
                )
            try:
                normalized.append(
                    {
                        **claim,
                        "field_name": field_name,
                        "field_value": coercer(claim.get("field_value")),
                    }
                )
            except NormalizationError as exc:
                extra = {
                    "source_kind": str(source_kind) if source_kind is not None else None,
                    "source_id": str(source_id) if source_id is not None else None,
                    "field_name": field_name,
                    "raw_value": claim.get("field_value"),
                    "ingestion_run_id": str(ingestion_run_id) if ingestion_run_id else None,
                    "error": str(exc),
                }
                if field_name in self._identity_critical_fields:
                    logger.warning(
                        "Identity-critical normalization failed; rejecting claim",
                        extra=extra,
                    )
                    raise

                # Preserve non-identity raw values rather than dropping the
                # claim, but make the failure visible to operators.
                logger.warning(
                    "Normalization failed; preserving raw claim value",
                    extra=extra,
                )
                normalized.append(
                    {
                        **claim,
                        "field_name": field_name,
                        "field_value": claim.get("field_value"),
                    }
                )
        return normalized


class SourceNormalizerRegistry:
    """Registry of source-kind normalizers and source-specific field mappers.

    The registry stores state per instance so tests and plugin code can use an
    isolated registry without mutating global class-level state.
    """

    def __init__(self) -> None:
        self._registry: dict[SourceKind, SourceNormalizer] = {
            SourceKind.EXTERNAL: ExternalSourceNormalizer(),
            SourceKind.INTERNAL: SourceNormalizer(),
        }
        self._source_mappers: dict[UUID, SourceFieldMapper] = {}

    def _coerce_kind(self, source_kind: SourceKind | str | None) -> SourceKind | None:
        if isinstance(source_kind, SourceKind):
            return source_kind
        try:
            return SourceKind(str(source_kind))
        except ValueError:
            try:
                return SourceKind(str(source_kind).upper())
            except ValueError:
                return None

    def get(self, source_kind: SourceKind | str | None) -> SourceNormalizer:
        kind = self._coerce_kind(source_kind)
        if kind is None:
            logger.warning(
                "Unknown source kind for normalizer; using identity normalizer",
                extra={"source_kind": source_kind},
            )
            return SourceNormalizer()
        return self._registry.get(kind, SourceNormalizer())

    def register(self, source_kind: SourceKind | str, normalizer: SourceNormalizer) -> None:
        """Override the normalizer for a source kind at runtime."""
        kind = self._coerce_kind(source_kind)
        if kind is None:
            raise ValueError(f"Unknown source kind: {source_kind!r}")
        self._registry[kind] = normalizer

    def register_source_mapper(
        self,
        source_id: UUID,
        mapper: SourceFieldMapper | Mapping[str, str],
    ) -> None:
        """Register field-name aliases for one concrete source/feed."""
        self._source_mappers[source_id] = (
            mapper if isinstance(mapper, SourceFieldMapper) else SourceFieldMapper(mapper)
        )

    def get_source_mapper(self, source_id: UUID | None) -> SourceFieldMapper | None:
        return self._source_mappers.get(source_id) if source_id is not None else None

    def normalize(
        self,
        source_kind: SourceKind | str | None,
        claims: list[dict[str, Any]],
        *,
        source_id: UUID | None = None,
        ingestion_run_id: UUID | None = None,
        source_field_mapping: Mapping[str, str] | None = None,
    ) -> list[dict[str, Any]]:
        """Normalize with the appropriate source-kind normalizer and mapper.

        Durable source configuration wins over in-memory registry state so
        multiple workers normalize a source consistently after restart.  The
        in-memory source mapper remains useful for tests and plugin-based
        deployments that intentionally register mappers at startup.
        """
        normalizer = self.get(source_kind)
        mapper = (
            SourceFieldMapper(source_field_mapping)
            if source_field_mapping
            else self.get_source_mapper(source_id)
        )
        return normalizer.normalize(
            claims,
            source_kind=source_kind,
            source_id=source_id,
            ingestion_run_id=ingestion_run_id,
            field_mapper=mapper,
        )


default_normalizer_registry = SourceNormalizerRegistry()
