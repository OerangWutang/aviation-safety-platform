"""Argus dedupe key service — stable, deterministic signal deduplication."""

from __future__ import annotations

from atlas.domain.enums import ArgusSignalType


def make_argus_dedupe_key(
    signal_type: ArgusSignalType, source_engine: str, parts: list[str]
) -> str:
    """Return a stable dedupe key for an Argus signal."""
    engine = source_engine.strip().lower()
    normalized_parts = [p.strip().lower() for p in parts]
    segments = ["ARGUS", signal_type.value, engine, *normalized_parts]
    return "::".join(segments)
