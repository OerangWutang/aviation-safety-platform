from __future__ import annotations

import logging
import uuid
from collections import OrderedDict
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from fastapi import BackgroundTasks, Depends, HTTPException, Request, Security
from fastapi.security import APIKeyHeader
from sqlalchemy import select

from atlas.application.dto import CurrentTenantUser, CurrentUser
from atlas.config import get_settings
from atlas.domain.enums import Role
from atlas.infrastructure.db.orm_models import ApiKeyModel
from atlas.infrastructure.db.session import (
    async_public_session_factory,
    async_session_factory,
    async_tenant_session_factory,
)
from atlas.infrastructure.db.unit_of_work import (
    SqlAlchemyTenantUnitOfWork,
    SqlAlchemyUnitOfWork,
    set_tenant_context,
)
from atlas.security import hash_api_key_candidates

logger = logging.getLogger(__name__)
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)

# Only refresh ``last_used_at`` if the previous value is older than this. On a
# busy API the audit signal does not benefit from second-by-second resolution,
# and per-request writes add load (one extra round-trip + one extra commit).
_LAST_USED_REFRESH_INTERVAL = timedelta(minutes=5)


@dataclass
class _CachedApiKey:
    key_id: uuid.UUID
    user_id: uuid.UUID
    role: str
    last_used_at: datetime | None
    expires_at: datetime
    # Phase 5: optional tenant binding.  Both None on system-only
    # keys; both set on tenant keys (CHECK constraint enforces "both
    # or neither" at the DB).
    tenant_id: uuid.UUID | None = None
    tenant_role: str | None = None


# Valid key cache only. We intentionally do not cache failed lookups: a spray of
# random keys should not be able to fill RAM with negative-cache entries.
#
# Asyncio-safety note: dict/OrderedDict operations are atomic within a single
# CPython thread because the GIL holds across pure-Python dict ops and asyncio
# yields only at explicit ``await`` points.  Do NOT access this cache from a
# thread pool (e.g. ``asyncio.to_thread``) without adding a lock first.
_AUTH_CACHE: OrderedDict[str, _CachedApiKey] = OrderedDict()


def clear_auth_cache() -> None:
    """Clear the in-process auth cache.

    Useful from tests and operational hooks after key rotation/revocation. The
    cache TTL bounds production staleness, but explicit clearing gives admins a
    zero-wait invalidation path inside a single process.
    """
    _AUTH_CACHE.clear()


def _get_cached_api_key(key_hash: str, now: datetime) -> _CachedApiKey | None:
    entry = _AUTH_CACHE.get(key_hash)
    if entry is None:
        return None
    if entry.expires_at <= now:
        _AUTH_CACHE.pop(key_hash, None)
        return None
    # LRU touch keeps hot keys resident without scanning the whole mapping.
    _AUTH_CACHE.move_to_end(key_hash)
    return entry


def _cache_api_key(key_hash: str, row: ApiKeyModel, now: datetime) -> _CachedApiKey | None:
    settings = get_settings()
    if settings.api_key_cache_ttl_seconds <= 0:
        return None
    entry = _CachedApiKey(
        key_id=row.id,
        user_id=row.user_id,
        role=row.role,
        last_used_at=row.last_used_at,
        expires_at=now + timedelta(seconds=settings.api_key_cache_ttl_seconds),
        tenant_id=row.tenant_id,
        tenant_role=row.tenant_role,
    )
    _AUTH_CACHE[key_hash] = entry
    _AUTH_CACHE.move_to_end(key_hash)
    while len(_AUTH_CACHE) > settings.api_key_cache_max_entries:
        _AUTH_CACHE.popitem(last=False)
    return entry


async def get_uow() -> AsyncGenerator[SqlAlchemyUnitOfWork, None]:
    """Yield a UnitOfWork for the duration of the path operation function.

    Use cases own the authoritative commit point via ``await uow.commit()``.
    On unhandled exceptions, rollback prevents partial writes from leaking.
    Successful read-only endpoints get a defensive rollback so any implicit
    read transaction releases its connection before response serialization.
    """
    async with async_session_factory() as session:
        uow = SqlAlchemyUnitOfWork(session)
        try:
            yield uow
        except Exception:
            # Request is exiting; roll back without re-establishing a fresh
            # tenant GUC transaction.
            await session.rollback()
            raise
        finally:
            if session.in_transaction():
                await session.rollback()


async def get_public_uow() -> AsyncGenerator[SqlAlchemyUnitOfWork, None]:
    """Yield a read-only UnitOfWork connected to the public Atlas database.

    In the split-topology deployment (``PUBLIC_DATABASE_URL`` set), this
    uses the separate public DB that holds canonical event projections and
    the public search/map indexes.  In single-database mode it falls back to
    the same DB as ``get_uow()`` — behaviour is identical in development.

    Use this for all public-read endpoints: ``/public/events``,
    ``/search/events``, ``/maps/events``.  Never use it for tenant payload
    reads (no RLS context is set) or writes (the public DB may be read-only
    in production).

    This is the FastAPI dependency counterpart to the ``create_public_uow``
    context manager used by the CLI corpus commands.
    """
    async with async_public_session_factory() as session:
        uow = SqlAlchemyUnitOfWork(session)
        try:
            yield uow
        except Exception:
            await uow.rollback()
            raise
        finally:
            if session.in_transaction():
                await session.rollback()


async def get_tenant_uow(
    tenant_id: uuid.UUID,
) -> AsyncGenerator[SqlAlchemyUnitOfWork, None]:
    """Yield a tenant-scoped UnitOfWork with PostgreSQL RLS context established.

    Sets ``app.current_tenant_id`` on the session before yielding so the
    migration-045 row-level-security policy filters every read and rejects
    every cross-tenant write at the database level.  This is defence-in-depth
    behind the application's own tenant checks in ``require_tenant_membership``.

    FastAPI injects ``tenant_id`` from the URL path parameter automatically.
    Because all tenant routes are mounted under
    ``/enterprise/tenants/{tenant_id}/...``, every dependent will receive the
    correct tenant UUID without any extra wiring in route handlers.

    FastAPI deduplicates identical ``Depends`` instances within a single
    request, so when both ``require_tenant_membership`` and the route handler
    declare ``Depends(get_tenant_uow)``, they share the same UoW object and
    the same underlying database session.
    """
    async with async_tenant_session_factory() as session:
        uow = SqlAlchemyTenantUnitOfWork(session, tenant_id)
        try:
            await set_tenant_context(session, tenant_id)
            yield uow
        except Exception:
            # Request is exiting; roll back without re-establishing a fresh
            # tenant GUC transaction.
            await session.rollback()
            raise
        finally:
            if session.in_transaction():
                await session.rollback()


async def _refresh_last_used_at(key_id: uuid.UUID, user_id: uuid.UUID) -> None:
    """Write ``last_used_at`` in a short-lived independent session.

    Runs as a FastAPI ``BackgroundTask`` after the response has been sent, so:
      - auth latency is unaffected by the round-trip,
      - failures here cannot fail the user-facing request (we log and move on),
      - the task lifetime is owned by the framework rather than the event loop
        (the previous ``asyncio.ensure_future`` form leaked a task reference
        that the loop could garbage-collect mid-flight).

    A separate session is required because:
      1. read-only endpoints never commit the request session, so the update
         would be silently discarded,
      2. committing the request session here risks flushing use-case writes
         that haven't finished yet (early commit hazard).
    """
    async with async_session_factory() as audit_session:
        try:
            key_obj = await audit_session.get(ApiKeyModel, key_id)
            if key_obj:
                key_obj.last_used_at = datetime.now(UTC)
                await audit_session.commit()
        except Exception:
            logger.warning(
                "Failed to update last_used_at for user %s",
                user_id,
                exc_info=True,
                extra={"user_id": str(user_id), "api_key_id": str(key_id)},
            )
            await audit_session.rollback()


def _schedule_last_used_refresh(
    background_tasks: BackgroundTasks,
    entry: _CachedApiKey,
    now: datetime,
) -> None:
    needs_refresh = (
        entry.last_used_at is None or entry.last_used_at < now - _LAST_USED_REFRESH_INTERVAL
    )
    if needs_refresh:
        # Mutate the cached timestamp before enqueueing to stop a thundering herd
        # of concurrent requests from scheduling identical background writes.
        entry.last_used_at = now
        background_tasks.add_task(_refresh_last_used_at, entry.key_id, entry.user_id)


async def _resolve_api_key(
    request: Request,
    background_tasks: BackgroundTasks,
    api_key: str,
) -> _CachedApiKey:
    """Shared lookup-and-cache for any auth dependency.

    Returns the cached entry (with tenant fields populated) so both
    the system-side and tenant-side dependencies can decide how to
    project it into their domain DTO.
    """
    if not api_key:
        raise HTTPException(status_code=401, detail="API key required")

    key_hashes = hash_api_key_candidates(api_key)
    now = datetime.now(UTC)
    for key_hash in key_hashes:
        cached = _get_cached_api_key(key_hash, now)
        if cached is not None:
            _schedule_last_used_refresh(background_tasks, cached, now)
            return cached

    stmt = select(ApiKeyModel).where(
        ApiKeyModel.key_hash.in_(key_hashes),
        ApiKeyModel.is_active.is_(True),
    )
    async with async_session_factory() as auth_session:
        result = await auth_session.execute(stmt)
        rows = list(result.scalars().all())

    if not rows:
        for key_hash in key_hashes:
            _AUTH_CACHE.pop(key_hash, None)
        logger.warning(
            "Auth failed - invalid or inactive API key",
            extra={"path": request.url.path, "method": request.method},
        )
        raise HTTPException(status_code=403, detail="Invalid API key")

    row_by_hash = {row.key_hash: row for row in rows}
    row = next((row_by_hash[h] for h in key_hashes if h in row_by_hash), rows[0])
    matched_hash = row.key_hash

    if len(rows) > 1:
        logger.warning(
            "Multiple active API key rows matched candidate hashes for one presented key; "
            "preferring the first hash candidate order",
            extra={"path": request.url.path, "method": request.method},
        )

    if row.role not in Role.values():
        logger.warning(
            "Auth failed - API key has unknown role %r for user %s",
            row.role,
            row.user_id,
        )
        raise HTTPException(status_code=403, detail="API key has unrecognised role")

    cached_row = _cache_api_key(matched_hash, row, now)
    entry = cached_row or _CachedApiKey(
        key_id=row.id,
        user_id=row.user_id,
        role=row.role,
        last_used_at=row.last_used_at,
        expires_at=now,
        tenant_id=row.tenant_id,
        tenant_role=row.tenant_role,
    )
    _schedule_last_used_refresh(background_tasks, entry, now)
    return entry


async def get_current_user(
    request: Request,
    background_tasks: BackgroundTasks,
    api_key: str = Security(api_key_header),
) -> CurrentUser:
    entry = await _resolve_api_key(request, background_tasks, api_key)
    return CurrentUser(user_id=entry.user_id, role=entry.role)


async def get_current_tenant_user(
    request: Request,
    background_tasks: BackgroundTasks,
    api_key: str = Security(api_key_header),
) -> CurrentTenantUser:
    """Auth dependency for tenant-scoped routes.

    Rejects with HTTP 403 (``NOT_A_TENANT_API_KEY``) if the API key
    is not bound to a tenant.  The tenant_id check against the path
    is performed by :func:`require_tenant_membership` so this
    dependency stays a pure "is this a tenant key" gate.
    """
    entry = await _resolve_api_key(request, background_tasks, api_key)
    if entry.tenant_id is None or entry.tenant_role is None:
        # The CHECK constraint guarantees both-or-neither at the DB
        # level; this in-process check is defence in depth against a
        # post-migration row that somehow violated it.
        raise HTTPException(
            status_code=403,
            detail={
                "code": "NOT_A_TENANT_API_KEY",
                "message": "This API key is not bound to a tenant",
            },
        )
    return CurrentTenantUser(
        user_id=entry.user_id,
        role=entry.role,
        tenant_id=entry.tenant_id,
        tenant_role=entry.tenant_role,
    )


def require_role(*roles: Role):
    """Return a FastAPI dependency that enforces the given role(s).

    ``roles`` must be ``Role`` enum members; passing arbitrary strings is a
    type error.  The comparison is against the enum *value* (a string) so it
    works transparently against the ``CurrentUser.role`` string field.
    """
    role_values = {r.value for r in roles}

    async def dependency(user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
        if user.role not in role_values:
            raise HTTPException(status_code=403, detail="Insufficient role")
        return user

    return dependency


def require_tenant_membership(*, allowed_roles: tuple[str, ...] = ()):
    """Return a FastAPI dependency that enforces tenant membership.

    Reads ``tenant_id`` from the path (the dependency factory wires
    it as a path parameter), then verifies:

    1. The caller's API key is bound to the same tenant
       (:class:`CrossTenantAccessError` → HTTP 403 otherwise).
    2. The tenant is active (:class:`TenantInactiveError` → 403
       otherwise).
    3. If ``allowed_roles`` is given, the caller's tenant_role is in
       the allowed set (HTTP 403 otherwise).

    The dependency also performs a live ``TenantMembership`` lookup.  The API
    key's tenant columns bind the key to one tenant, but the membership row is
    authoritative for revocation and role changes.  This makes tenant access
    fail closed immediately even if an API-key cache entry is still warm.

    Three independent isolation layers:

    1. This dependency (the auth gate).
    2. The use case (re-checks the path tenant_id against the
       authenticated CurrentTenantUser.tenant_id — defence in
       depth).
    3. The repository (every method takes tenant_id as a required
       parameter, WHERE-clauses on it).
    """
    from atlas.domain.tenancy.exceptions import (
        CrossTenantAccessError,
        TenantInactiveError,
    )

    async def dependency(
        tenant_id: uuid.UUID,
        caller: CurrentTenantUser = Depends(get_current_tenant_user),
        uow: SqlAlchemyUnitOfWork = Depends(get_tenant_uow),
    ) -> CurrentTenantUser:
        if caller.tenant_id != tenant_id:
            raise CrossTenantAccessError(
                caller_tenant_id=caller.tenant_id,
                target_tenant_id=tenant_id,
            )
        # Check tenant is active.  This is a cheap PK lookup
        # against the small ``tenants`` table.
        tenant = await uow.tenants.get(tenant_id)
        if tenant is None or not tenant.is_active:
            raise TenantInactiveError(f"Tenant {tenant_id} is not active")

        membership = await uow.tenant_memberships.get_for_user_in_tenant(
            tenant_id=tenant_id,
            user_id=caller.user_id,
        )
        if membership is None:
            raise HTTPException(
                status_code=403,
                detail={
                    "code": "TENANT_MEMBERSHIP_REQUIRED",
                    "message": "The authenticated user is not an active member of this tenant",
                },
            )

        live_role = (
            membership.tenant_role.value
            if hasattr(membership.tenant_role, "value")
            else str(membership.tenant_role)
        )
        if allowed_roles and live_role not in allowed_roles:
            raise HTTPException(
                status_code=403,
                detail={
                    "code": "INSUFFICIENT_TENANT_ROLE",
                    "message": (f"Tenant role {live_role} cannot access this resource"),
                },
            )
        return CurrentTenantUser(
            user_id=caller.user_id,
            role=caller.role,
            tenant_id=caller.tenant_id,
            tenant_role=live_role,
        )

    return dependency
