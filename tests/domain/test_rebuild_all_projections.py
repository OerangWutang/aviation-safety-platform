"""Tests for RebuildAllProjections - per-event commit and failure isolation.

After the fix, each event commits independently. A failure on event N cannot
roll back the already-committed projections for events 1..N-1.
"""

from __future__ import annotations

import logging
from uuid import uuid4

from atlas.application.dto import IngestionClaimDTO
from atlas.application.use_cases.ingest_source_data import IngestSourceData
from atlas.application.use_cases.rebuild_all_projections import RebuildAllProjections
from atlas.application.use_cases.reproject_event import ReProjectEvent
from atlas.domain.entities import Source
from atlas.domain.enums import SourceKind
from tests.domain._fake_uow import InMemoryUnitOfWork, make_settings


async def _setup_events(n: int) -> tuple[InMemoryUnitOfWork, list]:
    uow = InMemoryUnitOfWork()
    settings = make_settings()
    src = Source(id=uuid4(), name="S", kind=SourceKind.EXTERNAL, reliability_tier=1)
    await uow.sources.add(src)
    ids = []
    for i in range(n):
        eid = await IngestSourceData(uow, settings=settings).execute(
            source_id=src.id,
            raw_payload={"r": i},
            ingestion_run_id=uuid4(),
            claims_data=[
                IngestionClaimDTO(field_name="event_date", field_value=f"2024-01-{i + 1:02d}")
            ],
        )
        ids.append(eid)
    return uow, ids


async def test_rebuild_continues_after_one_event_fails(monkeypatch):
    """Failure on event 2 must not roll back event 1 or block event 3."""
    uow, (e1, e2, e3) = await _setup_events(3)
    bad = e2
    real = ReProjectEvent.execute

    async def flaky(self, event_id, **kwargs):
        if event_id == bad:
            raise RuntimeError("boom")
        return await real(self, event_id, **kwargs)

    monkeypatch.setattr(ReProjectEvent, "execute", flaky)
    result = await RebuildAllProjections(uow).execute(batch_size=10)

    assert result.processed == 2
    assert result.skipped == 1
    assert result.failed_event_ids == [bad]
    assert any("boom" in error for error in result.errors)
    assert e1 in uow.store.projections
    assert e3 in uow.store.projections
    assert e2 not in uow.store.projections


async def test_rebuild_counts_only_committed_events(monkeypatch):
    """The returned count reflects only committed projections."""
    uow, [e1] = await _setup_events(1)

    async def always_fail(self, event_id, **kwargs):
        raise RuntimeError("deliberate failure")

    monkeypatch.setattr(ReProjectEvent, "execute", always_fail)
    result = await RebuildAllProjections(uow).execute()
    assert result.processed == 0
    assert result.skipped == 1
    assert e1 not in uow.store.projections


async def test_rebuild_skips_reported_in_logs(monkeypatch, caplog):
    uow, [_e1] = await _setup_events(1)

    async def always_fail(self, event_id, **kwargs):
        raise RuntimeError("fail")

    monkeypatch.setattr(ReProjectEvent, "execute", always_fail)
    with caplog.at_level(logging.WARNING):
        await RebuildAllProjections(uow).execute()

    assert any("skipped" in r.message.lower() for r in caplog.records)


async def test_rebuild_can_be_safely_rerun():
    """Running rebuild twice is safe and does not create a duplicate version."""
    uow, [event_id] = await _setup_events(1)

    result1 = await RebuildAllProjections(uow).execute()
    v1 = uow.store.projections[event_id].projection_version

    result2 = await RebuildAllProjections(uow).execute()
    v2 = uow.store.projections[event_id].projection_version

    assert result1.processed == 1
    assert result2.processed == 1
    assert v2 == v1


async def test_rebuild_batch_size_respected(monkeypatch):
    """batch_size=1 means each event is in its own page."""
    uow, (e1, e2) = await _setup_events(2)
    result = await RebuildAllProjections(uow).execute(batch_size=1)
    assert result.processed == 2
    assert e1 in uow.store.projections
    assert e2 in uow.store.projections
