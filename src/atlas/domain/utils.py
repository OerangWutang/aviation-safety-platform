from __future__ import annotations

from datetime import UTC, datetime


def utc_now() -> datetime:
    """Return the current UTC time as a timezone-aware datetime."""
    return datetime.now(UTC)
