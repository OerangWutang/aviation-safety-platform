"""Natural-language search bounded context (Phase 7).

A deterministic NL parser routes free-text queries into structured
filters that compose with the existing Phase 2 (FTS), Phase 3
(spatial), and Phase 4 (HFACS/SHELO) infrastructure.

The parser is deliberately rule-based and stdlib-only so:

1. Production has no model dependency.
2. Results are reproducible and debuggable.
3. The output shape is what an LLM-routed pipeline would also
   produce, so a future swap to LLM-routed parsing doesn't change
   any downstream surface.
"""

from __future__ import annotations

from atlas.domain.nl_search.entities import (
    NlQueryLog,
    ParsedQuery,
    SavedNlQuery,
)
from atlas.domain.nl_search.exceptions import (
    SavedNlQueryNotFoundError,
)

__all__ = [
    "NlQueryLog",
    "ParsedQuery",
    "SavedNlQuery",
    "SavedNlQueryNotFoundError",
]
