from __future__ import annotations

import pytest

from atlas.config import Settings


def test_null_pool_warning_checks_each_effective_database_url() -> None:
    settings = Settings(
        database_url="postgresql+asyncpg://app@pgbouncer/atlas",
        system_database_url="postgresql+asyncpg://system@pgbouncer/atlas",
        tenant_database_url="postgresql+asyncpg://tenant@postgres/atlas",
        public_database_url="postgresql+asyncpg://public@postgres/atlas",
        environment="production",
        api_key_hash_secret="a" * 64,
        db_use_null_pool=True,
    )

    with pytest.warns(UserWarning) as warnings_seen:
        settings.validate_common_runtime_settings()

    messages = [str(w.message) for w in warnings_seen]
    assert any("TENANT_DATABASE_URL" in message for message in messages)
    assert any("PUBLIC_DATABASE_URL" in message for message in messages)
