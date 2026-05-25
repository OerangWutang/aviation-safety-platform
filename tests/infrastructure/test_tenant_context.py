"""Unit test for the tenant-context helper - no database required.

Verifies the *shape* of what ``set_tenant_context`` sends: a transaction-local
``set_config`` (``true`` third argument) for the ``app.current_tenant_id`` GUC,
with the tenant id passed as a bound parameter (never string-interpolated).
The live isolation behaviour is covered by ``tests/integration/test_tenant_rls``.
"""

from __future__ import annotations

import uuid

import pytest

from atlas.infrastructure.db.unit_of_work import TENANT_GUC, set_tenant_context


class _FakeSession:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def execute(self, clause, params=None):
        self.calls.append((str(clause), params or {}))
        return None


@pytest.mark.asyncio
async def test_set_tenant_context_uses_transaction_local_set_config():
    session = _FakeSession()
    tid = uuid.uuid4()

    await set_tenant_context(session, tid)

    assert len(session.calls) == 1
    sql, params = session.calls[0]
    assert "set_config" in sql.lower()
    assert ", true)" in sql.replace(" ", " ")  # third arg true -> transaction-local
    assert params == {"k": TENANT_GUC, "v": str(tid)}
    assert TENANT_GUC == "app.current_tenant_id"
    # The tenant id must travel as a bound parameter, not be baked into the SQL.
    assert str(tid) not in sql
