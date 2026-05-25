"""Orion v0.1 deterministic field normalizers."""

from __future__ import annotations

import re


def normalize_registration(value: str) -> str:
    """Lowercase and strip spaces, hyphens, and slashes."""
    return re.sub(r"[-/\s]", "", str(value).strip().lower())


def normalize_name(value: str) -> str:
    """Lowercase, trim, and collapse internal whitespace."""
    return re.sub(r"\s+", " ", str(value).strip().lower())


def normalize_airport_code(value: str) -> str:
    """Uppercase and trim an airport code."""
    return str(value).strip().upper()


def normalize_country(value: str) -> str:
    """Lowercase and trim a country name."""
    return str(value).strip().lower()


def is_blank(value: object) -> bool:
    """Return True if value is None, empty, or whitespace-only."""
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    return False
