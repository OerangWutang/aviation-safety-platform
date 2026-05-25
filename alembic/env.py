from __future__ import annotations

import os
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

from atlas.infrastructure.db.orm_models import Base

config = context.config


def _read_env_file_value(name: str) -> str | None:
    env_file = Path(".env")
    if not env_file.exists():
        return None

    for line in env_file.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        if key.strip() == name:
            return value.strip().strip('"').strip("'")
    return None


def _read_required_setting(name: str) -> str | None:
    return os.environ.get(name) or _read_env_file_value(name)


def _read_database_sync_url() -> str:
    """Read only the one setting Alembic needs.

    Do not import ``atlas.config.Settings`` here: migrations should not require
    API-key, CORS, Redis, pool, or production-startup settings to be valid.

    Set ``ATLAS_MIGRATION_TARGET=public`` to run the same migration chain
    against the public Atlas database using ``PUBLIC_DATABASE_SYNC_URL``.
    The default target uses ``DATABASE_SYNC_URL``.
    """
    target = os.environ.get("ATLAS_MIGRATION_TARGET", "default").strip().lower()
    if target in {"", "default", "system", "sms", "private"}:
        setting_name = "DATABASE_SYNC_URL"
    elif target == "public":
        setting_name = "PUBLIC_DATABASE_SYNC_URL"
    else:
        raise RuntimeError(
            "ATLAS_MIGRATION_TARGET must be one of: default, system, sms, private, public."
        )

    setting_value = _read_required_setting(setting_name)
    if setting_value:
        return setting_value

    if target != "public":
        ini_value = config.get_main_option("sqlalchemy.url")
        if ini_value:
            return ini_value

    raise RuntimeError(
        f"{setting_name} is required to run Alembic migrations for "
        f"ATLAS_MIGRATION_TARGET={target!r}. Set it in the environment or .env."
    )


database_sync_url = _read_database_sync_url()
config.set_main_option("sqlalchemy.url", database_sync_url)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=database_sync_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
