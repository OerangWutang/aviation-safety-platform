"""Fake NL search query log and saved-query repositories."""

from __future__ import annotations

from uuid import UUID

from atlas.domain.interfaces.repositories import (
    NlQueryLogRepository,
    SavedNlQueryRepository,
)
from atlas.domain.nl_search.entities import NlQueryLog, SavedNlQuery
from tests.domain.fakes._store import (
    _NlSearchStore,
)


class FakeNlQueryLogRepository(NlQueryLogRepository):
    def __init__(self, s: _NlSearchStore) -> None:
        self._s = s

    async def add(self, entry: NlQueryLog) -> None:
        self._s.query_log.append(entry.model_copy(deep=True))


class FakeSavedNlQueryRepository(SavedNlQueryRepository):
    def __init__(self, s: _NlSearchStore) -> None:
        self._s = s

    async def add(self, saved: SavedNlQuery) -> None:
        self._s.saved_queries[saved.id] = saved.model_copy(deep=True)

    async def list_for_user(self, user_id: UUID, *, limit: int = 50) -> list[SavedNlQuery]:
        return sorted(
            (
                s.model_copy(deep=True)
                for s in self._s.saved_queries.values()
                if s.user_id == user_id
            ),
            key=lambda s: s.created_at,
            reverse=True,
        )[:limit]

    async def get(self, saved_id: UUID) -> SavedNlQuery | None:
        s = self._s.saved_queries.get(saved_id)
        return s.model_copy(deep=True) if s else None

    async def delete_for_user(self, *, saved_id: UUID, user_id: UUID) -> bool:
        saved = self._s.saved_queries.get(saved_id)
        if saved is None or saved.user_id != user_id:
            return False
        del self._s.saved_queries[saved_id]
        return True


# ── Phase 8 fakes ───────────────────────────────────────────────────────────
