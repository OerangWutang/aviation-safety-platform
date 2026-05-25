"""Orion v0.1 deterministic entity resolution helpers."""

from __future__ import annotations

import re
from dataclasses import dataclass

from atlas.domain.enums import OrionEntityType
from atlas.domain.services.orion_normalizers import (
    is_blank,
    normalize_airport_code,
    normalize_country,
    normalize_name,
    normalize_registration,
)


@dataclass(frozen=True)
class IdentifierSpec:
    """One identifier to attach to an Orion entity."""

    identifier_type: str
    identifier_value: str
    normalized_value: str
    confidence: float


@dataclass
class ResolutionCandidate:
    """Instructions produced by a resolver for the use case to persist."""

    entity_type: OrionEntityType
    canonical_name: str
    primary_identifier: IdentifierSpec | None
    extra_identifiers: list[IdentifierSpec]
    new_entity_confidence: float


def _ident(
    identifier_type: str,
    raw_value: str,
    norm_value: str,
    confidence: float,
) -> IdentifierSpec:
    return IdentifierSpec(
        identifier_type=identifier_type,
        identifier_value=raw_value,
        normalized_value=norm_value,
        confidence=confidence,
    )


def resolve_aircraft(registration: str) -> ResolutionCandidate | None:
    if is_blank(registration):
        return None
    norm = normalize_registration(registration)
    if not norm:
        return None
    ident = _ident("registration", registration, norm, 0.98)
    return ResolutionCandidate(
        entity_type=OrionEntityType.AIRCRAFT,
        canonical_name=registration.strip().upper(),
        primary_identifier=ident,
        extra_identifiers=[],
        new_entity_confidence=0.85,
    )


def _name_candidate(
    entity_type: OrionEntityType,
    name: str,
    *,
    identifier_type: str = "name",
    new_confidence: float = 0.80,
    id_confidence: float = 0.90,
    canonical_name: str | None = None,
) -> ResolutionCandidate | None:
    if is_blank(name):
        return None
    norm = normalize_name(name)
    if not norm:
        return None
    ident = _ident(identifier_type, name, norm, id_confidence)
    return ResolutionCandidate(
        entity_type=entity_type,
        canonical_name=canonical_name or name.strip(),
        primary_identifier=ident,
        extra_identifiers=[],
        new_entity_confidence=new_confidence,
    )


def resolve_operator(name: str) -> ResolutionCandidate | None:
    return _name_candidate(OrionEntityType.OPERATOR, name)


_PURE_ALPHA_RE = re.compile(r"^[A-Za-z]+$")


def resolve_airport(
    code: str | None = None,
    name: str | None = None,
) -> ResolutionCandidate | None:
    """Resolve an airport, treating only 3/4 pure alpha values as IATA/ICAO codes."""
    primary: IdentifierSpec | None = None
    extras: list[IdentifierSpec] = []

    code_is_strong = (
        not is_blank(code)
        and code is not None
        and _PURE_ALPHA_RE.match(code.strip()) is not None
        and len(code.strip()) in (3, 4)
    )

    if code_is_strong:
        assert code is not None
        norm_code = normalize_airport_code(code)
        id_type = "icao" if len(norm_code) == 4 else "iata"
        primary = _ident(id_type, code, norm_code, 0.98)
        canonical_name = norm_code
        if not is_blank(name):
            assert name is not None
            extras.append(_ident("name", name, normalize_name(name), 0.90))
    elif not is_blank(name):
        assert name is not None
        primary = _ident("name", name, normalize_name(name), 0.90)
        canonical_name = name.strip()
        if not is_blank(code) and code is not None and code.strip() != name.strip():
            extras.append(_ident("name", code, normalize_name(code), 0.80))
    elif not is_blank(code):
        assert code is not None
        primary = _ident("name", code, normalize_name(code), 0.80)
        canonical_name = code.strip()
    else:
        return None

    return ResolutionCandidate(
        entity_type=OrionEntityType.AIRPORT,
        canonical_name=canonical_name,
        primary_identifier=primary,
        extra_identifiers=extras,
        new_entity_confidence=0.82,
    )


def resolve_aircraft_type(model: str) -> ResolutionCandidate | None:
    return _name_candidate(
        OrionEntityType.AIRCRAFT_TYPE,
        model,
        identifier_type="model",
        new_confidence=0.82,
    )


def resolve_manufacturer(name: str) -> ResolutionCandidate | None:
    return _name_candidate(OrionEntityType.MANUFACTURER, name)


def resolve_investigation_agency(name: str) -> ResolutionCandidate | None:
    return _name_candidate(OrionEntityType.INVESTIGATION_AGENCY, name)


def resolve_country(name: str) -> ResolutionCandidate | None:
    candidate = _name_candidate(
        OrionEntityType.COUNTRY,
        name,
        new_confidence=0.80,
        canonical_name=name.strip().title() if not is_blank(name) else None,
    )
    if candidate and candidate.primary_identifier:
        candidate.primary_identifier = _ident(
            candidate.primary_identifier.identifier_type,
            candidate.primary_identifier.identifier_value,
            normalize_country(candidate.primary_identifier.identifier_value),
            candidate.primary_identifier.confidence,
        )
    return candidate
