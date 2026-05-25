from __future__ import annotations

import hashlib
import hmac
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import BackgroundTasks, HTTPException

from atlas.config import get_settings


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows


class _FakeSession:
    def __init__(self, rows):
        self._rows = rows

    async def execute(self, _stmt):
        return _FakeResult(self._rows)


def _set_env(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@localhost/atlas")
    monkeypatch.setenv("DATABASE_SYNC_URL", "postgresql://u:p@localhost/atlas")
    monkeypatch.setenv("POSTGRES_USER", "u")
    monkeypatch.setenv("POSTGRES_PASSWORD", "p")
    monkeypatch.setenv("POSTGRES_DB", "atlas")
    monkeypatch.setenv("TENANT_DATABASE_URL", "postgresql+asyncpg://atlas_app:p@localhost/atlas")
    monkeypatch.setenv("SYSTEM_DATABASE_URL", "postgresql+asyncpg://atlas_system:p@localhost/atlas")


@pytest.mark.asyncio
async def test_auth_accepts_hash_with_previous_secret(monkeypatch):
    _set_env(monkeypatch)
    current = "a" * 64
    previous = "b" * 64
    monkeypatch.setenv("API_KEY_HASH_SECRET", current)
    monkeypatch.setenv("API_KEY_HASH_SECRET_PREVIOUS", previous)
    get_settings.cache_clear()

    import atlas.presentation.api.dependencies as dep_module

    dep_module.clear_auth_cache()

    plain_key = "legacy-client-key"
    previous_hash = hmac.digest(previous.encode(), plain_key.encode(), hashlib.sha256).hex()

    row = SimpleNamespace(
        id=uuid4(),
        key_hash=previous_hash,
        user_id=uuid4(),
        role="analyst",
        is_active=True,
        last_used_at=datetime.now(UTC),
        tenant_id=None,
        tenant_role=None,
    )

    @asynccontextmanager
    async def _fake_session_factory():
        yield _FakeSession([row])

    monkeypatch.setattr(dep_module, "async_session_factory", _fake_session_factory)

    request = SimpleNamespace(url=SimpleNamespace(path="/api/v1/accidents"), method="GET")
    background_tasks = BackgroundTasks()

    entry = await dep_module._resolve_api_key(request, background_tasks, plain_key)
    assert entry.user_id == row.user_id
    assert entry.role == "analyst"


@pytest.mark.asyncio
async def test_auth_rejects_previous_hash_when_previous_secret_not_set(monkeypatch):
    _set_env(monkeypatch)
    current = "a" * 64
    previous = "b" * 64
    monkeypatch.setenv("API_KEY_HASH_SECRET", current)
    monkeypatch.delenv("API_KEY_HASH_SECRET_PREVIOUS", raising=False)
    get_settings.cache_clear()

    import atlas.presentation.api.dependencies as dep_module

    dep_module.clear_auth_cache()

    plain_key = "legacy-client-key"
    @asynccontextmanager
    async def _fake_session_factory():
        # _resolve_api_key will query using only current hash, so this row should not match.
        yield _FakeSession([])

    monkeypatch.setattr(dep_module, "async_session_factory", _fake_session_factory)

    request = SimpleNamespace(url=SimpleNamespace(path="/api/v1/accidents"), method="GET")
    background_tasks = BackgroundTasks()

    with pytest.raises(HTTPException) as exc:
        await dep_module._resolve_api_key(request, background_tasks, plain_key)
    assert exc.value.status_code == 403
