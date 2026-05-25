"""Integration proof that the migration-045 RLS policy isolates tenants.

Marked ``integration``: needs a real PostgreSQL (``TEST_DATABASE_URL``) and is
skipped by default.  It creates its **own** throwaway probe table (so it never
touches the application schema and needs none of the TRUNCATE gating the rest of
the integration suite uses), applies the *exact* DDL pattern migration 045
emits, and asserts the guarantees that matter:

  * a tenant sees only its own rows,
  * an unset context is fail-closed (zero rows, never all rows),
  * a cross-tenant write is rejected by ``WITH CHECK``.

Critically, RLS is invisible to superusers and ``BYPASSRLS`` roles, so the test
**skips with a clear reason** if the connecting role would bypass the policy -
a false green here would be worse than no test.
"""

from __future__ import annotations

import os
import uuid

import pytest

pytest_asyncio = pytest.importorskip("pytest_asyncio")
sqlalchemy = pytest.importorskip("sqlalchemy")

from sqlalchemy import text  # noqa: E402
from sqlalchemy.exc import DBAPIError, ProgrammingError  # noqa: E402
from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402

pytestmark = pytest.mark.integration

_DSN = os.environ.get("TEST_DATABASE_URL")

_GUC = "app.current_tenant_id"
_PROBE = "rls_probe_tmp"


def _async_dsn(dsn: str) -> str:
    # Accept a psycopg/plain URL and coerce to the asyncpg driver.
    if dsn.startswith("postgresql+asyncpg://"):
        return dsn
    if dsn.startswith("postgresql://"):
        return dsn.replace("postgresql://", "postgresql+asyncpg://", 1)
    if dsn.startswith("postgres://"):
        return dsn.replace("postgres://", "postgresql+asyncpg://", 1)
    return dsn


@pytest_asyncio.fixture
async def engine():
    if not _DSN:
        pytest.skip("TEST_DATABASE_URL not set")
    eng = create_async_engine(_async_dsn(_DSN))
    # RLS cannot be observed as a superuser / BYPASSRLS role; skip rather than
    # report a misleading pass.
    async with eng.connect() as conn:
        row = (
            await conn.execute(
                text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")
            )
        ).first()
    if row and (row[0] or row[1]):
        await eng.dispose()
        if os.environ.get("ATLAS_RLS_TEST_MUST_RUN") == "1":
            pytest.fail("connecting role is superuser/BYPASSRLS; RLS is not observable")
        pytest.skip("connecting role is superuser/BYPASSRLS; RLS is not observable")
    try:
        yield eng
    finally:
        await eng.dispose()


@pytest_asyncio.fixture
async def probe(engine):
    async with engine.begin() as conn:
        await conn.execute(text(f"DROP TABLE IF EXISTS {_PROBE}"))
        await conn.execute(
            text(
                f"CREATE TABLE {_PROBE} ("
                "  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),"
                "  tenant_id uuid NOT NULL,"
                "  val text NOT NULL)"
            )
        )
        # Same DDL pattern as migration 045.
        await conn.execute(text(f"ALTER TABLE {_PROBE} ENABLE ROW LEVEL SECURITY"))
        await conn.execute(text(f"ALTER TABLE {_PROBE} FORCE ROW LEVEL SECURITY"))
        await conn.execute(
            text(
                f"CREATE POLICY tenant_isolation ON {_PROBE} "
                f"USING (tenant_id::text = current_setting('{_GUC}', true)) "
                f"WITH CHECK (tenant_id::text = current_setting('{_GUC}', true))"
            )
        )
    try:
        yield
    finally:
        async with engine.begin() as conn:
            await conn.execute(text(f"DROP TABLE IF EXISTS {_PROBE}"))


async def _seed(engine, tenant_id: uuid.UUID, val: str) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text("SELECT set_config(:k, :v, true)"), {"k": _GUC, "v": str(tenant_id)}
        )
        await conn.execute(
            text(f"INSERT INTO {_PROBE} (tenant_id, val) VALUES (:t, :v)"),
            {"t": str(tenant_id), "v": val},
        )


@pytest.mark.asyncio
async def test_tenant_sees_only_its_own_rows(engine, probe):
    a, b = uuid.uuid4(), uuid.uuid4()
    await _seed(engine, a, "a-secret")
    await _seed(engine, b, "b-secret")

    async with engine.begin() as conn:
        await conn.execute(text("SELECT set_config(:k, :v, true)"), {"k": _GUC, "v": str(a)})
        rows = (await conn.execute(text(f"SELECT val FROM {_PROBE}"))).scalars().all()
    assert rows == ["a-secret"]


@pytest.mark.asyncio
async def test_unset_context_is_fail_closed(engine, probe):
    a = uuid.uuid4()
    await _seed(engine, a, "a-secret")
    async with engine.begin() as conn:
        # No set_config at all -> current_setting(..., true) is NULL -> no rows.
        count = (await conn.execute(text(f"SELECT count(*) FROM {_PROBE}"))).scalar_one()
    assert count == 0


@pytest.mark.asyncio
async def test_cross_tenant_write_is_rejected(engine, probe):
    a, b = uuid.uuid4(), uuid.uuid4()
    with pytest.raises((DBAPIError, ProgrammingError)):
        async with engine.begin() as conn:
            await conn.execute(text("SELECT set_config(:k, :v, true)"), {"k": _GUC, "v": str(a)})
            # Attempt to write a row belonging to tenant B while in A's context.
            await conn.execute(
                text(f"INSERT INTO {_PROBE} (tenant_id, val) VALUES (:t, :v)"),
                {"t": str(b), "v": "leak"},
            )
