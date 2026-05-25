"""Chronos v0.1 deterministic field normalizers."""

from __future__ import annotations

from atlas.domain.constants import DISPUTED_MARKER


def normalize_timeline_raw_value(value: str) -> str:
    """Strip and normalize a raw timeline field value."""
    return value.strip()


def is_blank(value: object) -> bool:
    """Return True if value is None, empty, or whitespace-only."""
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    return False


def safe_str(value: object) -> str | None:
    """Return value as a clean string suitable for timeline use."""
    if value is None:
        return None
    if isinstance(value, (list, dict)):
        return None
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        if stripped == DISPUTED_MARKER or stripped.startswith(DISPUTED_MARKER):
            return None
        return stripped
    if isinstance(value, (int, float)):
        return str(value)
    return None
