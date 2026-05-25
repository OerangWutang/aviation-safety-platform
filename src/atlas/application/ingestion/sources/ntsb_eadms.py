"""NTSB eADMS (``avall.mdb``) -> Atlas ingestion mapping.

This module is the **pure, side-effect-free core** of the NTSB importer.  It
turns one accident record (a row from ``events`` plus its related ``aircraft``,
``narratives`` and ``Findings`` rows) into the exact submission contract the
``IngestSourceData`` use case already expects:

* ``raw_payload``      - the joined source record, preserved verbatim for audit.
* ``claims``           - a list of ``IngestionClaimDTO`` (field_name/field_value)
                         expressed in Atlas' source-neutral canonical vocabulary.
* ``source_record_id`` - the NTSB accident number (``ev_id``); stable identifier
                         that lets re-ingestion of an updated NTSB record attach
                         to the original event rather than create a duplicate.
* ``idempotency_key``  - deterministic, content-addressed; identical content
                         replays, changed content is a new submission.

Design notes
------------
* **No I/O here.**  Reading ``avall.mdb`` / CSV and calling the use case live in
  ``infrastructure`` and the CLI respectively, so this core is trivially unit
  testable and reusable for future bulk sources (ASN, etc.).
* **Epistemic framing.**  NTSB probable-cause text and coded findings are the
  Board's *official determinations*, not model inferences.  They are emitted as
  authoritative claims (reliability tier 1) and carry **no synthetic
  probability**.  Findings keep their Cause/Factor role exactly as the Board
  coded it; we never invent a causal weight.
* **Duplicate field names are illegal** in one submission (the use case raises
  ``DuplicateClaimFieldError``).  Repeating concepts - findings, occurrences -
  are therefore folded into a single structured-list claim rather than emitted
  as N same-named claims.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from atlas.application.dto import IngestionClaimDTO

# --------------------------------------------------------------------------- #
# Source identity
# --------------------------------------------------------------------------- #

#: Canonical registered-source name.  The CLI registers exactly one Source row
#: with this name and uses ``NTSB_FIELD_MAPPING`` as its ``field_mapping_json``.
NTSB_SOURCE_NAME = "NTSB eADMS (avall)"

#: NTSB final reports are the authoritative US investigative record.  Lower
#: reliability_tier = more trusted in ``WinnerPolicy``; tier 1 is the top.
NTSB_RELIABILITY_TIER = 1

#: Bumped only when the *shape* of the emitted claim vocabulary changes, so old
#: snapshots remain interpretable.
NTSB_SCHEMA_VERSION = 2

#: Stable prefix for the idempotency key namespace.
_IDEMPOTENCY_PREFIX = "ntsb-eadms"


# --------------------------------------------------------------------------- #
# Canonical field vocabulary
# --------------------------------------------------------------------------- #
# Atlas is deliberately field-name-agnostic below the source boundary, so the
# importer is responsible for picking a clean, source-neutral vocabulary.  These
# are the canonical Atlas field names the rest of the system (search, maps,
# causality, projections) will see.  The mapping is also stored verbatim on the
# Source row (``field_mapping_json``) so provenance/audit can explain every
# canonical field in terms of its raw eADMS column.

# raw eADMS column  ->  canonical Atlas field name
NTSB_FIELD_MAPPING: dict[str, str] = {
    # identity / classification
    "ntsb_no": "ntsb_number",
    "ev_type": "event_type",
    # when
    "ev_date": "occurred_on",
    "ev_time": "occurred_time_local",
    "ev_tmzn": "occurred_timezone",
    # where
    "ev_city": "location_city",
    "ev_state": "location_state",
    "ev_country": "location_country",
    "latitude": "latitude",
    "longitude": "longitude",
    "apt_name": "nearest_airport_name",
    "apt_dist": "nearest_airport_distance_nm",
    # environment
    "light_cond": "light_condition",
    "sky_cond_ceil": "ceiling_condition",
    "vis_sm": "visibility_statute_miles",
    "wx_temp": "temperature_c",
    "wind_vel_kts": "wind_speed_knots",
    "gust_kts": "wind_gust_knots",
    # outcome
    "ev_highest_injury": "highest_injury_level",
    "inj_tot_f": "fatalities_total",
    "inj_tot_s": "serious_injuries_total",
    "inj_tot_m": "minor_injuries_total",
    "inj_tot_n": "uninjured_total",
    # aircraft (primary aircraft on the event)
    "regis_no": "registration_number",
    "acft_make": "aircraft_make",
    "acft_model": "aircraft_model",
    "acft_series": "aircraft_series",
    "acft_category": "aircraft_category",
    "far_part": "far_part",
    "damage": "aircraft_damage",
    "homebuilt": "amateur_built",
    "num_eng": "engine_count",
    "total_seats": "total_seats",
    # narratives
    "narr_cause": "probable_cause_narrative",
    "narr_accf": "factual_narrative",
    "narr_accp": "analysis_narrative",
    # derived / structured
    "Findings": "causal_findings",
}

#: Which raw event/aircraft codes get decoded via the eADMS data dictionary.
#: (table, column) keys match the dictionary's own ``Table``/``Column`` columns.
_CODED_FIELDS: tuple[tuple[str, str], ...] = (
    ("events", "ev_type"),
    ("events", "ev_highest_injury"),
    ("events", "light_cond"),
    ("events", "sky_cond_ceil"),
    ("aircraft", "damage"),
    ("aircraft", "far_part"),
    ("aircraft", "acft_category"),
)


# --------------------------------------------------------------------------- #
# Code decoding
# --------------------------------------------------------------------------- #


class EadmsCodeDecoder:
    """Decode coded eADMS values (e.g. ``DEST`` -> ``Destroyed``).

    Built from the ``eADMSPUB_DataDictionary`` table that ships *inside* the
    same ``avall.mdb``, so the importer needs no external code list and stays
    correct as the NTSB revises codes.  Unknown codes are passed through
    unchanged rather than dropped - losing data silently would be worse than a
    rare un-prettified value, and the raw payload preserves the original code
    regardless.
    """

    def __init__(self, mapping: Mapping[tuple[str, str, str], str]) -> None:
        # (table_lower, column_lower, code_upper) -> human meaning
        self._mapping = dict(mapping)

    @classmethod
    def from_dictionary_rows(cls, rows: Iterable[Mapping[str, Any]]) -> EadmsCodeDecoder:
        mapping: dict[tuple[str, str, str], str] = {}
        for row in rows:
            table = (row.get("Table") or "").strip().lower()
            column = (row.get("Column") or "").strip().lower()
            code = (row.get("code_iaids") or "").strip()
            meaning = (row.get("meaning") or "").strip()
            if not (table and column and code and meaning):
                continue
            mapping[(table, column, code.upper())] = meaning
        return cls(mapping)

    def decode(self, table: str, column: str, code: str | None) -> str | None:
        if code is None:
            return None
        code = code.strip()
        if not code:
            return None
        return self._mapping.get((table.lower(), column.lower(), code.upper()), code)

    def __len__(self) -> int:  # pragma: no cover - trivial
        return len(self._mapping)


# --------------------------------------------------------------------------- #
# Result record
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class NtsbEventRecord:
    """One NTSB accident, shaped exactly for ``IngestSourceData.execute``."""

    source_record_id: str
    captured_at: datetime
    raw_payload: dict[str, Any]
    claims: list[IngestionClaimDTO]
    idempotency_key: str
    content_hash: str


# --------------------------------------------------------------------------- #
# Coercion helpers (defensive: eADMS CSV gives everything as strings)
# --------------------------------------------------------------------------- #


def _clean_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _to_int(value: Any) -> int | None:
    text = _clean_str(value)
    if text is None:
        return None
    try:
        return int(float(text))  # tolerate "3.0"
    except (TypeError, ValueError):
        return None


def _to_float(value: Any) -> float | None:
    text = _clean_str(value)
    if text is None:
        return None
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def _to_bool_yn(value: Any) -> bool | None:
    text = _clean_str(value)
    if text is None:
        return None
    upper = text.upper()
    if upper in {"Y", "YES", "T", "TRUE", "1"}:
        return True
    if upper in {"N", "NO", "F", "FALSE", "0"}:
        return False
    return None


# eADMS mdb-export renders dates like "09/25/2020 18:05:31" or "09/25/20 ...".
_DATE_FORMATS = ("%m/%d/%Y %H:%M:%S", "%m/%d/%y %H:%M:%S", "%m/%d/%Y", "%m/%d/%y")


def _parse_event_date(value: Any) -> date | None:
    text = _clean_str(value)
    if text is None:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    # last resort: leading ISO date
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _parse_local_time(value: Any) -> str | None:
    """``ev_time`` is an HHMM integer (e.g. 1805 -> '18:05'); 0/blank -> None."""
    minutes = _to_int(value)
    if minutes is None or minutes <= 0:
        return None
    hh, mm = divmod(minutes, 100)
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        return None
    return f"{hh:02d}:{mm:02d}"


# --------------------------------------------------------------------------- #
# Findings -> structured causal-evidence claim
# --------------------------------------------------------------------------- #

# Cause_Factor codes in eADMS: "C" = cause, "F" = factor, blank = unspecified.
_CAUSE_FACTOR_ROLE = {"C": "CAUSE", "F": "FACTOR"}


def build_finding_items(finding_rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Fold raw ``Findings`` rows into ordered, structured evidence items.

    Each item records the Board's coded finding and its Cause/Factor *role* as
    recorded - never a fabricated probability or weight.
    """
    items: list[dict[str, Any]] = []
    for row in finding_rows:
        code = _clean_str(row.get("finding_code"))
        description = _clean_str(row.get("finding_description"))
        if code is None and description is None:
            continue
        role_code = (_clean_str(row.get("Cause_Factor")) or "").upper()
        items.append(
            {
                "finding_code": code,
                "description": description,
                "category_no": _clean_str(row.get("category_no")),
                "subcategory_no": _clean_str(row.get("subcategory_no")),
                # The Board's recorded role; "UNSPECIFIED" when not coded.
                "role": _CAUSE_FACTOR_ROLE.get(role_code, "UNSPECIFIED"),
            }
        )
    # Deterministic order: causes first, then factors, then unspecified; stable
    # within a role by (finding_no order preserved) -> already iteration order.
    role_rank = {"CAUSE": 0, "FACTOR": 1, "UNSPECIFIED": 2}
    items.sort(key=lambda it: role_rank.get(it["role"], 3))
    return items


# --------------------------------------------------------------------------- #
# Claim assembly
# --------------------------------------------------------------------------- #


def _aircraft_sort_key(row: Mapping[str, Any]) -> int:
    return _to_int(row.get("Aircraft_Key")) or 0


def _registration_dedupe_key(value: str) -> str:
    return re.sub(r"[-/\s]", "", value.lower())


def _collect_aircraft_registration_numbers(
    aircraft_rows: list[Mapping[str, Any]],
) -> list[str]:
    registrations: list[str] = []
    seen: set[str] = set()

    for row in sorted(aircraft_rows, key=_aircraft_sort_key):
        registration = _clean_str(row.get("regis_no"))
        if registration is None:
            continue

        key = _registration_dedupe_key(registration)
        if not key or key in seen:
            continue

        seen.add(key)
        registrations.append(registration)

    return registrations


def _select_primary_aircraft(
    aircraft_rows: list[Mapping[str, Any]],
) -> Mapping[str, Any] | None:
    """Pick the primary aircraft (lowest ``Aircraft_Key``) for event-level claims.

    Primary-aircraft scalar claims still come from the lowest ``Aircraft_Key``.
    All aircraft registrations are now emitted as the derived ``aircraft_registration_numbers`` claim.
    Full per-aircraft claim fan-out remains out of scope.
    Multi-aircraft events (mid-airs, ground collisions) keep *all* aircraft in
    the raw payload.
    """
    if not aircraft_rows:
        return None
    return min(aircraft_rows, key=_aircraft_sort_key)


def build_event_record(
    *,
    event_row: Mapping[str, Any],
    aircraft_rows: list[Mapping[str, Any]],
    narrative_rows: list[Mapping[str, Any]],
    finding_rows: list[Mapping[str, Any]],
    decoder: EadmsCodeDecoder,
    captured_at: datetime | None = None,
) -> NtsbEventRecord | None:
    """Map one joined NTSB accident into an Atlas ingestion submission.

    Returns ``None`` when the record has no usable ``ev_id`` (cannot be a stable
    source_record_id), which the caller should count and skip.
    """
    ev_id = _clean_str(event_row.get("ev_id"))
    if ev_id is None:
        return None

    captured_at = captured_at or datetime.now(UTC)
    primary_acft = _select_primary_aircraft(aircraft_rows)
    all_aircraft_registrations = _collect_aircraft_registration_numbers(aircraft_rows)
    narrative = narrative_rows[0] if narrative_rows else {}

    # Ordered (canonical_name, value) pairs.  Order is fixed for determinism;
    # None values are dropped so projections stay clean.
    pairs: list[tuple[str, Any]] = []

    def put(name: str, value: Any) -> None:
        if value is None:
            return
        if isinstance(value, str) and not value.strip():
            return
        pairs.append((name, value))

    # ---- classification / time -------------------------------------------- #
    put("ntsb_number", _clean_str(event_row.get("ntsb_no")))
    put("event_type", decoder.decode("events", "ev_type", _clean_str(event_row.get("ev_type"))))
    ev_date = _parse_event_date(event_row.get("ev_date"))
    put("occurred_on", ev_date.isoformat() if ev_date else None)
    put("occurred_time_local", _parse_local_time(event_row.get("ev_time")))
    put("occurred_timezone", _clean_str(event_row.get("ev_tmzn")))

    # ---- location --------------------------------------------------------- #
    put("location_city", _clean_str(event_row.get("ev_city")))
    put("location_state", _clean_str(event_row.get("ev_state")))
    put("location_country", _clean_str(event_row.get("ev_country")))
    put("latitude", _to_float(event_row.get("latitude")))
    put("longitude", _to_float(event_row.get("longitude")))
    put("nearest_airport_name", _clean_str(event_row.get("apt_name")))
    put("nearest_airport_distance_nm", _to_float(event_row.get("apt_dist")))

    # ---- environment ------------------------------------------------------ #
    put(
        "light_condition",
        decoder.decode("events", "light_cond", _clean_str(event_row.get("light_cond"))),
    )
    put(
        "ceiling_condition",
        decoder.decode("events", "sky_cond_ceil", _clean_str(event_row.get("sky_cond_ceil"))),
    )
    put("visibility_statute_miles", _to_float(event_row.get("vis_sm")))
    put("temperature_c", _to_float(event_row.get("wx_temp")))
    put("wind_speed_knots", _to_int(event_row.get("wind_vel_kts")))
    put("wind_gust_knots", _to_int(event_row.get("gust_kts")))

    # ---- outcome ---------------------------------------------------------- #
    put(
        "highest_injury_level",
        decoder.decode(
            "events", "ev_highest_injury", _clean_str(event_row.get("ev_highest_injury"))
        ),
    )
    put("fatalities_total", _to_int(event_row.get("inj_tot_f")))
    put("serious_injuries_total", _to_int(event_row.get("inj_tot_s")))
    put("minor_injuries_total", _to_int(event_row.get("inj_tot_m")))
    put("uninjured_total", _to_int(event_row.get("inj_tot_n")))

    # ---- aircraft (primary) ----------------------------------------------- #
    if primary_acft is not None:
        put("registration_number", _clean_str(primary_acft.get("regis_no")))
        if all_aircraft_registrations:
            put("aircraft_registration_numbers", all_aircraft_registrations)
        put("aircraft_make", _clean_str(primary_acft.get("acft_make")))
        put("aircraft_model", _clean_str(primary_acft.get("acft_model")))
        put("aircraft_series", _clean_str(primary_acft.get("acft_series")))
        put(
            "aircraft_category",
            decoder.decode(
                "aircraft", "acft_category", _clean_str(primary_acft.get("acft_category"))
            ),
        )
        put(
            "far_part",
            decoder.decode("aircraft", "far_part", _clean_str(primary_acft.get("far_part"))),
        )
        put(
            "aircraft_damage",
            decoder.decode("aircraft", "damage", _clean_str(primary_acft.get("damage"))),
        )
        put("amateur_built", _to_bool_yn(primary_acft.get("homebuilt")))
        put("engine_count", _to_int(primary_acft.get("num_eng")))
        put("total_seats", _to_int(primary_acft.get("total_seats")))

    # ---- narratives ------------------------------------------------------- #
    put("probable_cause_narrative", _clean_str(narrative.get("narr_cause")))
    put("factual_narrative", _clean_str(narrative.get("narr_accf")))
    put("analysis_narrative", _clean_str(narrative.get("narr_accp")))

    # ---- findings (single structured claim; never duplicate field names) -- #
    finding_items = build_finding_items(finding_rows)
    if finding_items:
        pairs.append(("causal_findings", finding_items))

    claims = [IngestionClaimDTO(field_name=name, field_value=value) for name, value in pairs]

    raw_payload: dict[str, Any] = {
        "schema": "ntsb-eadms",
        "schema_version": NTSB_SCHEMA_VERSION,
        "ev_id": ev_id,
        "event": dict(event_row),
        "aircraft": [dict(r) for r in aircraft_rows],
        "narratives": [dict(r) for r in narrative_rows],
        "findings": [dict(r) for r in finding_rows],
    }

    content_hash = _content_hash(source_record_id=ev_id, claims=claims, raw_payload=raw_payload)
    idempotency_key = f"{_IDEMPOTENCY_PREFIX}:{ev_id}:{content_hash[:16]}"

    return NtsbEventRecord(
        source_record_id=ev_id,
        captured_at=captured_at,
        raw_payload=raw_payload,
        claims=claims,
        idempotency_key=idempotency_key,
        content_hash=content_hash,
    )


def _content_hash(
    *,
    source_record_id: str,
    claims: list[IngestionClaimDTO],
    raw_payload: Mapping[str, Any],
) -> str:
    """Stable content fingerprint.

    Same NTSB record content -> same key (the use case replays the stored
    result).  Changed content -> different key, which the use case treats as a
    *new* submission that still attaches to the original event via the shared
    ``source_record_id``.  So re-running the importer after an NTSB refresh
    cleanly layers updated claims without creating duplicate events.
    """
    material = {
        "source_record_id": source_record_id,
        "claims": [c.model_dump(mode="json") for c in claims],
        # Hash the raw event/aircraft/narrative/finding bodies, not capture time.
        "ev_id": raw_payload.get("ev_id"),
        "event": raw_payload.get("event"),
        "aircraft": raw_payload.get("aircraft"),
        "narratives": raw_payload.get("narratives"),
        "findings": raw_payload.get("findings"),
    }
    blob = json.dumps(material, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(blob).hexdigest()
