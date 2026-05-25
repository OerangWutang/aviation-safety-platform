from collections.abc import AsyncGenerator
from functools import lru_cache
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from atlas.config import get_settings


def _engine_kwargs() -> dict[str, Any]:
    settings = get_settings()
    engine_kwargs: dict[str, Any] = {
        "echo": False,
        "future": True,
        "pool_pre_ping": True,
        "pool_recycle": settings.db_pool_recycle_seconds,
    }
    if settings.db_use_null_pool:
        # PgBouncer transaction-pooling mode owns connection reuse. A local
        # SQLAlchemy pool can otherwise pin server connections across requests
        # and defeat the external pooler's multiplexing.
        engine_kwargs["poolclass"] = NullPool
    else:
        engine_kwargs.update(
            pool_size=settings.db_pool_size,
            max_overflow=settings.db_max_overflow,
        )
    return engine_kwargs


@lru_cache(maxsize=1)
def get_engine():
    """System/default engine.

    Uses SYSTEM_DATABASE_URL when present, otherwise DATABASE_URL.  This keeps
    development backwards-compatible while allowing production to separate the
    system/worker role from the tenant HTTP role.
    """
    return create_async_engine(get_settings().effective_system_database_url, **_engine_kwargs())


@lru_cache(maxsize=1)
def get_tenant_engine():
    """Least-privilege tenant engine.

    In production this should use a NOBYPASSRLS role so tenant RLS policies are
    enforced by PostgreSQL rather than by application discipline alone.
    """
    return create_async_engine(get_settings().effective_tenant_database_url, **_engine_kwargs())


@lru_cache(maxsize=1)
def get_public_engine():
    """Engine for the public Atlas database (read-only corpus / projections).

    When ``PUBLIC_DATABASE_URL`` is set, connects to the separate public DB
    that holds canonical event projections (the "fully separate" topology).
    Falls back to the system/default URL when unset.
    """
    return create_async_engine(get_settings().effective_public_database_url, **_engine_kwargs())


@lru_cache(maxsize=1)
def get_session_factory():
    return async_sessionmaker(get_engine(), class_=AsyncSession, expire_on_commit=False)


@lru_cache(maxsize=1)
def get_tenant_session_factory():
    return async_sessionmaker(get_tenant_engine(), class_=AsyncSession, expire_on_commit=False)


@lru_cache(maxsize=1)
def get_public_session_factory():
    """Session factory for the public Atlas database.

    Used by ``create_public_uow()`` to load the Echo precedent corpus and
    serve public projection reads in the split-topology deployment.
    """
    return async_sessionmaker(get_public_engine(), class_=AsyncSession, expire_on_commit=False)


def async_session_factory() -> AsyncSession:
    return get_session_factory()()


def async_tenant_session_factory() -> AsyncSession:
    return get_tenant_session_factory()()


def async_public_session_factory() -> AsyncSession:
    """Return a session on the public DB (or the system DB if topology is single)."""
    return get_public_session_factory()()


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    async with async_session_factory() as session:
        yield session
