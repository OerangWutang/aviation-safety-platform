from __future__ import annotations

from uuid import uuid4

import pytest

from atlas.domain.entities import OutboxEvent
from atlas.domain.enums import OutboxStatus
from atlas.domain.tenancy.entities import CrossrefResultStatus, TenantCrossrefResult
from atlas.infrastructure.event_bus import outbox_worker as worker_module
from atlas.infrastructure.event_bus.outbox_worker import OutboxWorker
from tests.domain._fake_uow import InMemoryUnitOfWork


class _AsyncUowContext:
    def __init__(self, uow: InMemoryUnitOfWork) -> None:
        self.uow = uow

    async def __aenter__(self) -> InMemoryUnitOfWork:
        return self.uow

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None


@pytest.mark.asyncio
async def test_echo_outbox_acknowledges_completed_result_without_rerun(monkeypatch):
    tenant_id = uuid4()
    result_id = uuid4()

    tenant_uow = InMemoryUnitOfWork()
    tenant_uow.store.tenancy.crossref_results[result_id] = TenantCrossrefResult(
        id=result_id,
        tenant_id=tenant_id,
        safety_report_id=uuid4(),
        status=CrossrefResultStatus.COMPLETE,
        match_count=3,
    )

    system_uow = InMemoryUnitOfWork()
    event = OutboxEvent(
        event_type="ECHO_CROSSREF_REQUESTED",
        aggregate_id=result_id,
        payload={"tenant_id": str(tenant_id), "crossref_result_id": str(result_id)},
        status=OutboxStatus.PROCESSING,
        attempt_count=1,
        locked_by="worker-test",
    )
    await system_uow.outbox.add(event)

    monkeypatch.setattr(
        worker_module,
        "create_tenant_uow",
        lambda seen_tenant_id: _AsyncUowContext(tenant_uow),
    )

    def fail_if_rerun(*args, **kwargs):
        raise AssertionError("Completed Echo results must not be re-run")

    monkeypatch.setattr(worker_module, "RunEchoCrossReference", fail_if_rerun)

    processed = await OutboxWorker(worker_id="worker-test")._process_echo_crossref_event(
        system_uow,
        event,
    )

    assert processed is True
    stored_event = system_uow.store.outbox[0]
    assert stored_event.status == OutboxStatus.PROCESSED
    assert stored_event.processed_at is not None
