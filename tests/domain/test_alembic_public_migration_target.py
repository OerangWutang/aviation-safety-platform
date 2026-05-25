from __future__ import annotations

from pathlib import Path


def test_alembic_can_target_public_database_sync_url() -> None:
    source = Path("alembic/env.py").read_text()

    assert "ATLAS_MIGRATION_TARGET" in source
    assert "PUBLIC_DATABASE_SYNC_URL" in source
    assert "DATABASE_SYNC_URL" in source
