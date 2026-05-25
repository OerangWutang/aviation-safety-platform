"""Chronos v0.1 deterministic timestamp parser."""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime

from atlas.domain.enums import ChronosTimestampPrecision

logger = logging.getLogger(__name__)

_ISO_DATETIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}")
_DATETIME_MINUTE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})$")
_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_DATE_HOUR_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})[T ](\d{2})$")
_TIME_ONLY_RE = re.compile(r"^\d{2}:\d{2}$")

_APPROXIMATE_PHRASES = (
    "approximately",
    "about",
    "around",
    "circa",
    "shortly after",
    "shortly before",
    "just after",
    "just before",
    "during approach",
    "during climb",
    "during descent",
    "during cruise",
    "during takeoff",
    "after takeoff",
    "before landing",
    "on approach",
)
_UNKNOWN_PHRASES = ("unknown", "n/a", "not known", "not available", "unavailable", "tbd", "")


def parse_chronos_timestamp(value: str) -> tuple[datetime | None, ChronosTimestampPrecision]:
    """Parse a raw string value into a (datetime | None, precision) tuple."""
    if not value or not value.strip():
        return None, ChronosTimestampPrecision.UNKNOWN

    stripped = value.strip()
    lower = stripped.lower()

    if lower in _UNKNOWN_PHRASES:
        return None, ChronosTimestampPrecision.UNKNOWN

    for phrase in _APPROXIMATE_PHRASES:
        if phrase in lower:
            return None, ChronosTimestampPrecision.APPROXIMATE

    if _TIME_ONLY_RE.match(stripped):
        return None, ChronosTimestampPrecision.RELATIVE

    if _ISO_DATETIME_RE.match(stripped):
        try:
            normalised = stripped.replace(" ", "T")
            if normalised.endswith("Z"):
                normalised = normalised[:-1] + "+00:00"
            dt = datetime.fromisoformat(normalised)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            if dt.second != 0 or dt.microsecond != 0:
                return dt, ChronosTimestampPrecision.EXACT
            return dt, ChronosTimestampPrecision.MINUTE
        except ValueError:
            pass

    m = _DATETIME_MINUTE_RE.match(stripped)
    if m:
        try:
            dt = datetime(
                int(m.group(1)),
                int(m.group(2)),
                int(m.group(3)),
                int(m.group(4)),
                int(m.group(5)),
                tzinfo=UTC,
            )
            return dt, ChronosTimestampPrecision.MINUTE
        except ValueError:
            pass

    m = _DATE_HOUR_RE.match(stripped)
    if m:
        try:
            dt = datetime(
                int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4)), 0, tzinfo=UTC
            )
            return dt, ChronosTimestampPrecision.HOUR
        except ValueError:
            pass

    m = _DATE_RE.match(stripped)
    if m:
        try:
            dt = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), tzinfo=UTC)
            return dt, ChronosTimestampPrecision.DAY
        except ValueError:
            pass

    logger.debug(
        "chronos_time_parser: could not parse %r as any known timestamp format; "
        "returning UNKNOWN precision",
        stripped,
    )
    return None, ChronosTimestampPrecision.UNKNOWN
