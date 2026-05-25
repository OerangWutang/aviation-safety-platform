"""IdentityIndexUpdater - maintain the synchronous event identity substrate."""

from __future__ import annotations

import re
from typing import Any
from uuid import UUID

from atlas.application.unit_of_work import UnitOfWork
from atlas.domain.entities import EventIdentityIndex
from atlas.domain.services.event_matching import _norm, _norm_date


def _norm_reg(val: Any) -> str | None:
    if not val:
        return None
    return re.sub(r"[-/\s]", "", _norm(val)) or None


def _iter_registration_alias_values(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list | tuple):
        return list(value)
    if isinstance(value, set):
        return sorted(value)
    return []


def _registration_norm_aliases(incoming_fields: dict[str, Any]) -> list[str]:
    aliases: list[str] = []
    seen: set[str] = set()

    def append(value: Any) -> None:
        norm = _norm_reg(value)
        if norm is None or norm in seen:
            return
        seen.add(norm)
        aliases.append(norm)

    append(incoming_fields.get("registration"))

    for value in _iter_registration_alias_values(
        incoming_fields.get("aircraft_registration_numbers")
    ):
        append(value)

    return aliases


def _build_identity_entry(
    event_id: UUID,
    incoming_fields: dict[str, Any],
    source_record_id: str | None,
) -> EventIdentityIndex:
    """Construct an ``EventIdentityIndex`` from raw ingestion claim fields.

    Normalisation mirrors ``event_matching._norm`` / ``_norm_date`` so that
    stored values are directly comparable to normalised incoming fields that the
    matcher scores against.

    ``registration_norms`` is seeded as a single-element list when a
    registration is present; the upsert layer unions it with whatever is already
    stored so the list grows across ingestions and every known alias is retained.
    """
    reg_norm = _norm_reg(incoming_fields.get("registration"))
    registration_norms = _registration_norm_aliases(incoming_fields)
    return EventIdentityIndex(
        event_id=event_id,
        event_date_norm=_norm_date(str(incoming_fields["event_date"]))
        if incoming_fields.get("event_date")
        else None,
        registration_norm=reg_norm,
        operator_norm=_norm(incoming_fields.get("operator")) or None,
        location_norm=_norm(incoming_fields.get("location")) or None,
        aircraft_type_norm=_norm(incoming_fields.get("aircraft_type")) or None,
        source_record_ids=[source_record_id] if source_record_id else [],
        registration_norms=registration_norms,
    )


class IdentityIndexUpdater:
    """Upsert the synchronous event identity index after a non-matching ingestion.

    The identity index is written in the same database transaction as ingestion,
    so it is visible to the very next ingestion that commits after ours - unlike
    ``projected_accident_records``, which is populated asynchronously by the
    outbox worker.

    This service is called for paths that bypass anonymous event matching
    (explicit ``event_id`` ingestion and ``source_record_id`` continuity), so
    that future anonymous ingestions with the same identity-bearing claims can
    find the event and avoid creating duplicates.
    """

    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    async def update(
        self,
        event_id: UUID,
        normalized_fields: dict[str, Any],
        source_record_id: str | None,
    ) -> None:
        """Upsert the identity entry for ``event_id`` with the given fields."""
        await self._uow.identity_index.upsert(
            _build_identity_entry(event_id, normalized_fields, source_record_id)
        )
