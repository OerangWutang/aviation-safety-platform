"""Unit tests for the Echo corpus cache (CachedCorpusLoader).

Verifies cache hit/miss/expiry/invalidation without a database or real corpus.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest

from atlas.application.use_cases.echo_crossref import (
    CachedCorpusLoader,
    InMemoryCorpusLoader,
    _CorpusCacheEntry,
)
from atlas.domain.crossref.entities import PrecedentRecord


def _make_records(n: int = 3) -> list[PrecedentRecord]:
    return [PrecedentRecord(event_id=f"EVT{i:03d}") for i in range(n)]


def _fake_uow():
    return object()  # corpus loader protocol only uses uow as a passthrough


@pytest.fixture(autouse=True)
def reset_cache():
    """Clear the class-level cache and lock before each test."""
    CachedCorpusLoader._cache = None
    CachedCorpusLoader._lock = None
    yield
    CachedCorpusLoader._cache = None
    CachedCorpusLoader._lock = None


@pytest.mark.asyncio
async def test_first_call_loads_corpus():
    records = _make_records(5)
    with patch.object(InMemoryCorpusLoader, "load", new=AsyncMock(return_value=records)):
        loader = CachedCorpusLoader(ttl_seconds=3600)
        result = await loader.load(uow=_fake_uow())
    assert result == records
    assert CachedCorpusLoader._cache is not None
    assert CachedCorpusLoader._cache.size == 5


@pytest.mark.asyncio
async def test_second_call_within_ttl_uses_cache():
    records = _make_records(4)
    mock_load = AsyncMock(return_value=records)
    with patch.object(InMemoryCorpusLoader, "load", new=mock_load):
        loader = CachedCorpusLoader(ttl_seconds=3600)
        await loader.load(uow=_fake_uow())
        await loader.load(uow=_fake_uow())
    # InMemoryCorpusLoader.load should only be called once
    assert mock_load.call_count == 1


@pytest.mark.asyncio
async def test_expired_cache_triggers_reload():
    records = _make_records(2)
    mock_load = AsyncMock(return_value=records)
    with patch.object(InMemoryCorpusLoader, "load", new=mock_load):
        loader = CachedCorpusLoader(ttl_seconds=1)
        await loader.load(uow=_fake_uow())
        # Artificially age the cache entry past the TTL
        CachedCorpusLoader._cache = _CorpusCacheEntry(
            records=records,
            built_at=datetime.now(UTC) - timedelta(seconds=10),
            size=len(records),
        )
        await loader.load(uow=_fake_uow())
    assert mock_load.call_count == 2


@pytest.mark.asyncio
async def test_ttl_zero_always_reloads():
    records = _make_records(1)
    mock_load = AsyncMock(return_value=records)
    with patch.object(InMemoryCorpusLoader, "load", new=mock_load):
        loader = CachedCorpusLoader(ttl_seconds=0)
        await loader.load(uow=_fake_uow())
        await loader.load(uow=_fake_uow())
    assert mock_load.call_count == 2


@pytest.mark.asyncio
async def test_invalidate_forces_reload():
    records = _make_records(3)
    mock_load = AsyncMock(return_value=records)
    with patch.object(InMemoryCorpusLoader, "load", new=mock_load):
        loader = CachedCorpusLoader(ttl_seconds=3600)
        await loader.load(uow=_fake_uow())
        CachedCorpusLoader.invalidate()
        assert CachedCorpusLoader._cache is None
        await loader.load(uow=_fake_uow())
    assert mock_load.call_count == 2


@pytest.mark.asyncio
async def test_concurrent_calls_load_once():
    """Two concurrent coroutines should trigger only one DB load (lock guards)."""
    records = _make_records(10)
    call_count = 0

    async def slow_load(self, *, uow):
        nonlocal call_count
        call_count += 1
        await asyncio.sleep(0.01)
        return records

    with patch.object(InMemoryCorpusLoader, "load", new=slow_load):
        loader = CachedCorpusLoader(ttl_seconds=3600)
        await asyncio.gather(
            loader.load(uow=_fake_uow()),
            loader.load(uow=_fake_uow()),
            loader.load(uow=_fake_uow()),
        )
    assert call_count == 1
