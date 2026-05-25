from __future__ import annotations

import pytest

from atlas.application.use_cases.create_hermes_crawl_target import (
    CreateHermesCrawlTarget,
    CreateHermesCrawlTargetInput,
)
from atlas.application.use_cases.enqueue_hermes_fetch_job import (
    EnqueueHermesFetchJob,
    EnqueueHermesFetchJobInput,
)
from atlas.application.use_cases.register_hermes_source import (
    RegisterHermesSource,
    RegisterHermesSourceInput,
)
from atlas.domain.enums import HermesSourceType, HermesTargetStatus
from atlas.domain.services.hermes_url_normalizer import normalize_url
from tests.domain._fake_uow import InMemoryUnitOfWork


def make_uow() -> InMemoryUnitOfWork:
    return InMemoryUnitOfWork()


@pytest.mark.asyncio
async def test_register_source_creates_source():
    uow = make_uow()
    uc = RegisterHermesSource(uow)
    source = await uc.execute(
        RegisterHermesSourceInput(name="BBC News", source_type=HermesSourceType.NEWS)
    )
    assert source.name == "BBC News"
    assert source.source_type == HermesSourceType.NEWS
    assert uow.commits == 1


@pytest.mark.asyncio
async def test_register_same_name_is_idempotent():
    uow = make_uow()
    uc = RegisterHermesSource(uow)
    s1 = await uc.execute(
        RegisterHermesSourceInput(name="BBC News", source_type=HermesSourceType.NEWS)
    )
    s2 = await uc.execute(
        RegisterHermesSourceInput(name="bbc news", source_type=HermesSourceType.NEWS)
    )
    assert s1.id == s2.id
    assert uow.commits == 1


@pytest.mark.asyncio
async def test_create_target_normalizes_url():
    uow = make_uow()
    source = await RegisterHermesSource(uow).execute(
        RegisterHermesSourceInput(name="S", source_type=HermesSourceType.OTHER)
    )
    target = await CreateHermesCrawlTarget(uow).execute(
        CreateHermesCrawlTargetInput(source_id=source.id, url="HTTP://EXAMPLE.COM/PATH/")
    )
    assert target.normalized_url == "http://example.com/PATH"


@pytest.mark.asyncio
async def test_duplicate_normalized_target_is_idempotent():
    uow = make_uow()
    source = await RegisterHermesSource(uow).execute(
        RegisterHermesSourceInput(name="S2", source_type=HermesSourceType.OTHER)
    )
    t1 = await CreateHermesCrawlTarget(uow).execute(
        CreateHermesCrawlTargetInput(source_id=source.id, url="https://example.com/page")
    )
    t2 = await CreateHermesCrawlTarget(uow).execute(
        CreateHermesCrawlTargetInput(source_id=source.id, url="https://example.com/page")
    )
    assert t1.id == t2.id


@pytest.mark.asyncio
async def test_create_target_for_missing_source_raises():
    uow = make_uow()
    from uuid import uuid4

    with pytest.raises(ValueError, match="not found"):
        await CreateHermesCrawlTarget(uow).execute(
            CreateHermesCrawlTargetInput(source_id=uuid4(), url="https://example.com")
        )


@pytest.mark.asyncio
async def test_enqueue_job_creates_queued_job():
    uow = make_uow()
    source = await RegisterHermesSource(uow).execute(
        RegisterHermesSourceInput(name="S3", source_type=HermesSourceType.OTHER)
    )
    target = await CreateHermesCrawlTarget(uow).execute(
        CreateHermesCrawlTargetInput(source_id=source.id, url="https://example.com")
    )
    job = await EnqueueHermesFetchJob(uow).execute(EnqueueHermesFetchJobInput(target_id=target.id))
    from atlas.domain.enums import HermesFetchJobStatus

    assert job.status == HermesFetchJobStatus.QUEUED
    assert job.target_id == target.id


@pytest.mark.asyncio
async def test_cannot_enqueue_paused_target():
    uow = make_uow()
    source = await RegisterHermesSource(uow).execute(
        RegisterHermesSourceInput(name="S4", source_type=HermesSourceType.OTHER)
    )
    target = await CreateHermesCrawlTarget(uow).execute(
        CreateHermesCrawlTargetInput(source_id=source.id, url="https://paused.example.com")
    )
    target.status = HermesTargetStatus.PAUSED
    await uow.hermes_crawl_targets.save(target)
    with pytest.raises(ValueError, match="not ACTIVE"):
        await EnqueueHermesFetchJob(uow).execute(EnqueueHermesFetchJobInput(target_id=target.id))


@pytest.mark.asyncio
async def test_normalize_url_strips_fragment_and_default_port():
    assert normalize_url("http://example.com:80/page#section") == "http://example.com/page"
    assert normalize_url("https://example.com:443/page") == "https://example.com/page"
    assert normalize_url("https://example.com/path/?q=1#frag") == "https://example.com/path?q=1"


def test_normalize_url_rejects_missing_host_and_bad_port():
    with pytest.raises(ValueError):
        normalize_url("http:///path")
    with pytest.raises(ValueError):
        normalize_url("http://example.com:bad/path")
