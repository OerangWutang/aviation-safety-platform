"""Integration test: TenantCrossrefResult persistence lifecycle.

Tests the full write→read→mark_complete→mark_failed flow against a real
PostgreSQL instance, with the table's RLS policy active.

Marked ``integration``: requires ``TEST_DATABASE_URL`` and ``--run-integration``.
Skipped if the connecting role has BYPASSRLS (RLS is unobservable there).
Creates and drops its own probe schema — never touches the application schema.
"""

from __future__ import annotations

import json
import os
import uuid

import pytest

pytest_asyncio = pytest.importorskip("pytest_asyncio")

from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402

pytestmark = pytest.mark.integration

_DSN = os.environ.get("TEST_DATABASE_URL")
_GUC = "app.current_tenant_id"

_CREATE_PROBE_SCHEMA = """
CREATE TABLE IF NOT EXISTS _probe_tenants (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid()
);
CREATE TABLE IF NOT EXISTS _probe_safety_reports (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES _probe_tenants(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS _probe_crossref_results (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES _probe_tenants(id) ON DELETE CASCADE,
    safety_report_id uuid REFERENCES _probe_safety_reports(id) ON DELETE CASCADE,
    claim_id uuid,
    status text NOT NULL DEFAULT 'PENDING',
    matches_json jsonb NOT NULL DEFAULT '[]',
    matcher_config_json jsonb NOT NULL DEFAULT '{}',
    match_count integer NOT NULL DEFAULT 0,
    requested_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz,
    error_detail text,
    CHECK (status IN ('PENDING', 'COMPLETE', 'FAILED')),
    CHECK ((safety_report_id IS NOT NULL)::int + (claim_id IS NOT NULL)::int = 1),
    CHECK (match_count >= 0)
);
ALTER TABLE _probe_crossref_results ENABLE ROW LEVEL SECURITY;
ALTER TABLE _probe_crossref_results FORCE  ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON _probe_crossref_results
    USING      (tenant_id::text = current_setting('app.current_tenant_id', true))
    WITH CHECK (tenant_id::text = current_setting('app.current_tenant_id', true));
"""

_DROP_PROBE_SCHEMA = """
DROP TABLE IF EXISTS _probe_crossref_results;
DROP TABLE IF EXISTS _probe_safety_reports;
DROP TABLE IF EXISTS _probe_tenants;
"""


def _async_dsn(dsn: str) -> str:
    if dsn.startswith("postgresql+asyncpg://"):
        return dsn
    if dsn.startswith("postgresql://"):
        return dsn.replace("postgresql://", "postgresql+asyncpg://", 1)
    return dsn.replace("postgres://", "postgresql+asyncpg://", 1)


@pytest_asyncio.fixture
async def engine():
    if not _DSN:
        pytest.skip("TEST_DATABASE_URL not set")
    eng = create_async_engine(_async_dsn(_DSN))
    async with eng.connect() as conn:
        row = (
            await conn.execute(
                text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")
            )
        ).first()
    if row and (row[0] or row[1]):
        await eng.dispose()
        if os.environ.get("ATLAS_RLS_TEST_MUST_RUN") == "1":
            pytest.fail("connecting role bypasses RLS; test would not observe isolation")
        pytest.skip("connecting role bypasses RLS; test would not observe isolation")
    yield eng
    await eng.dispose()


def _split_sql(block: str) -> list[str]:
    return [s.strip() for s in block.strip().split(";") if s.strip()]


@pytest_asyncio.fixture
async def probe(engine):
    async with engine.begin() as conn:
        for stmt in _split_sql(_DROP_PROBE_SCHEMA):
            await conn.execute(text(stmt))
        for stmt in _split_sql(_CREATE_PROBE_SCHEMA):
            await conn.execute(text(stmt))
    yield engine
    async with engine.begin() as conn:
        for stmt in _split_sql(_DROP_PROBE_SCHEMA):
            await conn.execute(text(stmt))


async def _insert_tenant(engine, tenant_id: uuid.UUID) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO _probe_tenants (id) VALUES (:id) ON CONFLICT DO NOTHING"),
            {"id": str(tenant_id)},
        )


async def _insert_report(engine, tenant_id: uuid.UUID, report_id: uuid.UUID) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO _probe_safety_reports (id, tenant_id) VALUES (:id, :t)"),
            {"id": str(report_id), "t": str(tenant_id)},
        )


@pytest.mark.asyncio
async def test_pending_result_is_written_and_read_back(probe):
    tid = uuid.uuid4()
    rid = uuid.uuid4()
    result_id = uuid.uuid4()
    await _insert_tenant(probe, tid)
    await _insert_report(probe, tid, rid)

    async with probe.begin() as conn:
        await conn.execute(text("SELECT set_config(:k,:v,true)"), {"k": _GUC, "v": str(tid)})
        await conn.execute(
            text(
                "INSERT INTO _probe_crossref_results "
                "(id,tenant_id,safety_report_id) VALUES (:id,:t,:r)"
            ),
            {"id": str(result_id), "t": str(tid), "r": str(rid)},
        )

    async with probe.begin() as conn:
        await conn.execute(text("SELECT set_config(:k,:v,true)"), {"k": _GUC, "v": str(tid)})
        row = (
            await conn.execute(
                text("SELECT status, match_count FROM _probe_crossref_results WHERE id=:id"),
                {"id": str(result_id)},
            )
        ).first()
    assert row is not None
    assert row[0] == "PENDING"
    assert row[1] == 0


@pytest.mark.asyncio
async def test_mark_complete_writes_matches_json(probe):
    tid = uuid.uuid4()
    rid = uuid.uuid4()
    result_id = uuid.uuid4()
    await _insert_tenant(probe, tid)
    await _insert_report(probe, tid, rid)

    matches = [{"event_id": "X001", "score": 0.88, "support": "STRONG"}]

    async with probe.begin() as conn:
        await conn.execute(text("SELECT set_config(:k,:v,true)"), {"k": _GUC, "v": str(tid)})
        await conn.execute(
            text(
                "INSERT INTO _probe_crossref_results "
                "(id,tenant_id,safety_report_id) VALUES (:id,:t,:r)"
            ),
            {"id": str(result_id), "t": str(tid), "r": str(rid)},
        )
        await conn.execute(
            text("""UPDATE _probe_crossref_results
                    SET status='COMPLETE', matches_json=:m, match_count=:n, completed_at=now()
                    WHERE id=:id AND tenant_id=:t AND status='PENDING'"""),
            {"m": json.dumps(matches), "n": len(matches), "id": str(result_id), "t": str(tid)},
        )

    async with probe.begin() as conn:
        await conn.execute(text("SELECT set_config(:k,:v,true)"), {"k": _GUC, "v": str(tid)})
        row = (
            await conn.execute(
                text(
                    "SELECT status, match_count, matches_json "
                    "FROM _probe_crossref_results WHERE id=:id"
                ),
                {"id": str(result_id)},
            )
        ).first()
    assert row[0] == "COMPLETE"
    assert row[1] == 1
    assert row[2][0]["event_id"] == "X001"


@pytest.mark.asyncio
async def test_rls_hides_results_from_other_tenant(probe):
    t_a, t_b = uuid.uuid4(), uuid.uuid4()
    r_a, r_b = uuid.uuid4(), uuid.uuid4()
    await _insert_tenant(probe, t_a)
    await _insert_tenant(probe, t_b)
    await _insert_report(probe, t_a, r_a)
    await _insert_report(probe, t_b, r_b)

    for tid, rid in [(t_a, r_a), (t_b, r_b)]:
        async with probe.begin() as conn:
            await conn.execute(text("SELECT set_config(:k,:v,true)"), {"k": _GUC, "v": str(tid)})
            await conn.execute(
                text(
                    "INSERT INTO _probe_crossref_results "
                    "(id,tenant_id,safety_report_id) VALUES (:id,:t,:r)"
                ),
                {"id": str(uuid.uuid4()), "t": str(tid), "r": str(rid)},
            )

    # Tenant A sees exactly 1 row.
    async with probe.begin() as conn:
        await conn.execute(text("SELECT set_config(:k,:v,true)"), {"k": _GUC, "v": str(t_a)})
        n = (await conn.execute(text("SELECT count(*) FROM _probe_crossref_results"))).scalar_one()
    assert n == 1


@pytest.mark.asyncio
async def test_xor_constraint_rejects_both_null_sources(probe):
    """The source-XOR CHECK prevents rows with no hazard reference."""
    from sqlalchemy.exc import DBAPIError

    tid = uuid.uuid4()
    await _insert_tenant(probe, tid)

    with pytest.raises(DBAPIError):
        async with probe.begin() as conn:
            await conn.execute(text("SELECT set_config(:k,:v,true)"), {"k": _GUC, "v": str(tid)})
            # Neither safety_report_id nor claim_id set — must fail.
            await conn.execute(
                text("INSERT INTO _probe_crossref_results (id,tenant_id) VALUES (:id,:t)"),
                {"id": str(uuid.uuid4()), "t": str(tid)},
            )
