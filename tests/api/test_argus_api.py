"""API tests for Argus v0.1 endpoints."""

from __future__ import annotations

from uuid import uuid4

import pytest
from httpx import AsyncClient

from atlas.domain.entities import ArgusSignal, ChronosSequenceReview
from atlas.domain.enums import (
    ArgusSeverity,
    ArgusSignalStatus,
    ArgusSignalType,
    ChronosSequenceReviewStatus,
)


def _make_signal(**kwargs) -> ArgusSignal:
    defaults = dict(
        signal_type=ArgusSignalType.TIMELINE_SEQUENCE_CONFLICT,
        severity=ArgusSeverity.MEDIUM,
        confidence=0.9,
        title="Test signal",
        source_engine="chronos",
        dedupe_key=f"ARGUS::TEST::{uuid4()}",
    )
    defaults.update(kwargs)
    return ArgusSignal(**defaults)


def _make_sequence_review(**kwargs) -> ChronosSequenceReview:
    defaults = dict(
        id=uuid4(),
        accident_event_id=uuid4(),
        timeline_event_id_a=uuid4(),
        timeline_event_id_b=uuid4(),
        reason="test",
        status=ChronosSequenceReviewStatus.PENDING,
    )
    defaults.update(kwargs)
    return ChronosSequenceReview(**defaults)


@pytest.mark.asyncio
async def test_reviewer_can_post_run(async_client_reviewer: AsyncClient, in_memory_uow):
    in_memory_uow.store.chronos.sequence_reviews.append(_make_sequence_review())
    resp = await async_client_reviewer.post("/api/v1/argus/run", json={})
    assert resp.status_code == 200, resp.text
    assert resp.json()["signals_created_count"] >= 1


@pytest.mark.asyncio
async def test_analyst_cannot_post_run(async_client_analyst: AsyncClient):
    resp = await async_client_analyst.post("/api/v1/argus/run", json={})
    assert resp.status_code in (401, 403)


@pytest.mark.asyncio
async def test_analyst_can_get_signals(async_client_analyst: AsyncClient, in_memory_uow):
    signal = _make_signal()
    await in_memory_uow.argus_signals.add(signal)
    resp = await async_client_analyst.get("/api/v1/argus/signals")
    assert resp.status_code == 200, resp.text
    assert isinstance(resp.json(), list)


@pytest.mark.asyncio
async def test_reviewer_can_get_signal_detail(async_client_reviewer: AsyncClient, in_memory_uow):
    signal = _make_signal()
    await in_memory_uow.argus_signals.add(signal)
    resp = await async_client_reviewer.get(f"/api/v1/argus/signals/{signal.id}")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["signal"]["id"] == str(signal.id)
    assert "evidence" in data
    assert "reviews" in data


@pytest.mark.asyncio
async def test_reviewer_can_post_review(async_client_reviewer: AsyncClient, in_memory_uow):
    signal = _make_signal()
    await in_memory_uow.argus_signals.add(signal)
    resp = await async_client_reviewer.post(
        f"/api/v1/argus/signals/{signal.id}/review",
        json={
            "decision": "CONFIRMED",
            "note": "Verified",
            "expected_version": signal.version,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == ArgusSignalStatus.CONFIRMED
    # Successful review bumps the version so the next reviewer sees fresh state.
    assert body["version"] == signal.version + 1


@pytest.mark.asyncio
async def test_analyst_cannot_post_review(async_client_analyst: AsyncClient, in_memory_uow):
    signal = _make_signal()
    await in_memory_uow.argus_signals.add(signal)
    resp = await async_client_analyst.post(
        f"/api/v1/argus/signals/{signal.id}/review",
        json={"decision": "CONFIRMED", "expected_version": 1},
    )
    assert resp.status_code in (401, 403)


# ── Optimistic concurrency: 409 / 422 / 404 surface ─────────────────────────


@pytest.mark.asyncio
async def test_review_rejects_missing_expected_version(
    async_client_reviewer: AsyncClient, in_memory_uow
):
    """Without ``expected_version`` the request is invalid at the framework
    boundary.  Belt-and-braces: even if the use case were called directly,
    the schema rejects the request first."""
    signal = _make_signal()
    await in_memory_uow.argus_signals.add(signal)
    resp = await async_client_reviewer.post(
        f"/api/v1/argus/signals/{signal.id}/review",
        json={"decision": "CONFIRMED"},
    )
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_review_returns_409_on_stale_expected_version(
    async_client_reviewer: AsyncClient, in_memory_uow
):
    """When the reviewer's ``expected_version`` is stale, the API returns 409
    with the ``ARGUS_SIGNAL_MODIFIED`` error code and the current state so
    the client can re-render and retry."""
    from atlas.domain.enums import ArgusSignalStatus as Status

    signal = _make_signal()
    await in_memory_uow.argus_signals.add(signal)
    # Simulate a previous reviewer's confirm landing first.
    await in_memory_uow.argus_signals.update_with_version_check(
        signal_id=signal.id,
        expected_version=signal.version,
        updates={"status": Status.CONFIRMED.value},
    )

    resp = await async_client_reviewer.post(
        f"/api/v1/argus/signals/{signal.id}/review",
        json={
            "decision": "DISMISSED",
            "expected_version": signal.version,  # now stale
        },
    )
    assert resp.status_code == 409, resp.text
    body = resp.json()
    assert body["error"]["code"] == "ARGUS_SIGNAL_MODIFIED"
    assert body["error"]["details"]["signal_id"] == str(signal.id)
    assert body["error"]["details"]["current_version"] == signal.version + 1
    assert body["error"]["details"]["current_signal"]["status"] == Status.CONFIRMED.value


@pytest.mark.asyncio
async def test_review_returns_404_on_missing_signal(
    async_client_reviewer: AsyncClient,
):
    """Previously a 500 (use case raised bare ValueError → caught as
    AtlasError by the global handler → 500).  Now a typed
    ``ArgusSignalNotFoundError`` that maps to 404."""
    resp = await async_client_reviewer.post(
        f"/api/v1/argus/signals/{uuid4()}/review",
        json={"decision": "CONFIRMED", "expected_version": 1},
    )
    assert resp.status_code == 404, resp.text
    body = resp.json()
    assert body["error"]["code"] == "ARGUS_SIGNAL_NOT_FOUND"


# ── high_conflict_threshold request-validation surface ───────────────────────


@pytest.mark.asyncio
async def test_run_rejects_high_conflict_threshold_below_two(
    async_client_reviewer: AsyncClient,
):
    """The request schema constrains ``high_conflict_threshold`` to ``ge=2``.
    A value of 1 is rejected at the framework boundary (422), not silently
    accepted and then ignored by the detector."""
    resp = await async_client_reviewer.post(
        "/api/v1/argus/run", json={"high_conflict_threshold": 1}
    )
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_run_accepts_explicit_high_conflict_threshold(
    async_client_reviewer: AsyncClient, in_memory_uow
):
    """Smoke check: the new request field is honoured end-to-end."""
    from atlas.domain.entities import ClaimConflict

    event_id = uuid4()
    for i in range(5):
        await in_memory_uow.conflicts.add(ClaimConflict(event_id=event_id, field_name=f"f{i}"))

    resp = await async_client_reviewer.post(
        "/api/v1/argus/run",
        json={
            "include_chronos": False,
            "include_hermes": False,
            "include_atlas": True,
            "include_orion": False,
            "high_conflict_threshold": 5,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["signals_created_count"] == 1
    assert body["engines_errored"] == []


# ── Router → metric recorder integration ────────────────────────────────────


@pytest.mark.asyncio
async def test_run_endpoint_records_per_signal_type_counter(
    async_client_reviewer: AsyncClient, in_memory_uow, monkeypatch
):
    """The router must invoke ``record_signal_created`` once per newly-created
    signal, with the right ``signal_type`` label.  We patch the recorder so
    tests don't depend on the global Prometheus registry state.
    """
    from atlas.domain.entities import ClaimConflict
    from atlas.presentation.api.routers import argus as argus_router

    calls: list[ArgusSignalType] = []

    def _spy(signal_type):
        calls.append(signal_type)

    monkeypatch.setattr(argus_router, "record_signal_created", _spy)

    # Seed three high-conflict events so the Atlas detector creates three
    # signals — one per event.
    for _ in range(3):
        event_id = uuid4()
        for j in range(5):
            await in_memory_uow.conflicts.add(ClaimConflict(event_id=event_id, field_name=f"f{j}"))

    resp = await async_client_reviewer.post(
        "/api/v1/argus/run",
        json={
            "include_chronos": False,
            "include_hermes": False,
            "include_atlas": True,
            "include_orion": False,
            "high_conflict_threshold": 5,
        },
    )
    assert resp.status_code == 200, resp.text
    assert len(calls) == 3
    assert all(c == ArgusSignalType.HIGH_CONFLICT_ACCIDENT_RECORD for c in calls)


@pytest.mark.asyncio
async def test_run_endpoint_records_engine_errors(
    async_client_reviewer: AsyncClient, in_memory_uow, monkeypatch
):
    """A simulated chronos failure must surface both in ``engines_errored``
    and in the ``argus_engine_errors_total`` counter."""
    from atlas.presentation.api.routers import argus as argus_router

    calls: list[str] = []
    monkeypatch.setattr(argus_router, "record_engine_error", lambda engine: calls.append(engine))

    async def _boom(*_args, **_kwargs):
        raise RuntimeError("simulated chronos failure")

    in_memory_uow.chronos_sequence_reviews.list_pending = _boom  # type: ignore[method-assign]

    resp = await async_client_reviewer.post(
        "/api/v1/argus/run",
        json={"include_hermes": False, "include_atlas": False, "include_orion": False},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["engines_errored"] == ["chronos"]
    assert calls == ["chronos"]


@pytest.mark.asyncio
async def test_run_endpoint_observes_duration_histogram(
    async_client_reviewer: AsyncClient, monkeypatch
):
    """The router must observe one duration per detection run regardless of
    whether the run produced output."""
    from atlas.presentation.api.routers import argus as argus_router

    observations: list[float] = []
    monkeypatch.setattr(argus_router, "observe_detection_duration", observations.append)

    resp = await async_client_reviewer.post("/api/v1/argus/run", json={})
    assert resp.status_code == 200
    assert len(observations) == 1
    assert observations[0] >= 0


@pytest.mark.asyncio
async def test_run_endpoint_observes_duration_on_use_case_failure(
    async_client_reviewer: AsyncClient, monkeypatch
):
    """When the use case raises, the histogram must still record the elapsed
    time — that's the operational signal you want during incidents."""
    from atlas.presentation.api.routers import argus as argus_router

    observations: list[float] = []
    monkeypatch.setattr(argus_router, "observe_detection_duration", observations.append)

    class _Boom:
        def __init__(self, _uow):
            pass

        async def execute(self, _input):
            raise RuntimeError("kapow")

    monkeypatch.setattr(argus_router, "RunArgusSignalDetection", _Boom)

    resp = await async_client_reviewer.post("/api/v1/argus/run", json={})
    assert resp.status_code == 500
    # Even on a 500, the duration histogram observed exactly once.
    assert len(observations) == 1


@pytest.mark.asyncio
async def test_review_endpoint_records_decision_counter(
    async_client_reviewer: AsyncClient, in_memory_uow, monkeypatch
):
    """``record_signal_review`` must be invoked exactly once on success, with
    the reviewer's decision as the label."""
    from atlas.domain.enums import ArgusReviewDecision
    from atlas.presentation.api.routers import argus as argus_router

    calls: list[ArgusReviewDecision] = []
    monkeypatch.setattr(
        argus_router, "record_signal_review", lambda decision: calls.append(decision)
    )

    signal = _make_signal()
    await in_memory_uow.argus_signals.add(signal)
    resp = await async_client_reviewer.post(
        f"/api/v1/argus/signals/{signal.id}/review",
        json={"decision": "DISMISSED", "expected_version": signal.version},
    )
    assert resp.status_code == 200, resp.text
    assert calls == [ArgusReviewDecision.DISMISSED]


@pytest.mark.asyncio
async def test_review_endpoint_does_not_record_on_stale_version(
    async_client_reviewer: AsyncClient, in_memory_uow, monkeypatch
):
    """A 409 means the reviewer's decision was *not* persisted — the metric
    must not count it.  Otherwise dashboards would over-count reviews
    relative to actual state changes."""
    from atlas.domain.enums import ArgusSignalStatus as Status
    from atlas.presentation.api.routers import argus as argus_router

    calls: list[object] = []
    monkeypatch.setattr(
        argus_router, "record_signal_review", lambda decision: calls.append(decision)
    )

    signal = _make_signal()
    await in_memory_uow.argus_signals.add(signal)
    # Bump the version so the next request is stale.
    await in_memory_uow.argus_signals.update_with_version_check(
        signal_id=signal.id,
        expected_version=signal.version,
        updates={"status": Status.CONFIRMED.value},
    )

    resp = await async_client_reviewer.post(
        f"/api/v1/argus/signals/{signal.id}/review",
        json={"decision": "DISMISSED", "expected_version": signal.version},
    )
    assert resp.status_code == 409
    assert calls == []


# ── Keyset pagination: GET /argus/signals/page ──────────────────────────────


def _make_signal_at(last_detected_at, **kwargs):
    """Build a signal at a specific timestamp (helper for pagination tests)."""
    defaults = dict(
        signal_type=ArgusSignalType.NEW_SOURCE_CHANGE,
        severity=ArgusSeverity.MEDIUM,
        confidence=0.9,
        title="Page test",
        source_engine="hermes",
        dedupe_key=f"ARGUS::TEST::{uuid4()}",
        first_detected_at=last_detected_at,
        last_detected_at=last_detected_at,
    )
    defaults.update(kwargs)
    return ArgusSignal(**defaults)


@pytest.mark.asyncio
async def test_signals_page_endpoint_returns_envelope(
    async_client_reviewer: AsyncClient, in_memory_uow
):
    """The paginated endpoint returns ``{items, pagination}`` — distinct from
    the legacy ``GET /argus/signals`` which returns a bare list."""
    from datetime import UTC, datetime, timedelta

    base = datetime.now(UTC)
    for i in range(7):
        await in_memory_uow.argus_signals.add(_make_signal_at(base - timedelta(minutes=i)))

    resp = await async_client_reviewer.get("/api/v1/argus/signals/page?limit=3")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "items" in body
    assert "pagination" in body
    assert len(body["items"]) == 3
    assert body["pagination"]["limit"] == 3
    assert body["pagination"]["next_cursor"] is not None


@pytest.mark.asyncio
async def test_signals_page_endpoint_walks_pages_without_overlap(
    async_client_reviewer: AsyncClient, in_memory_uow
):
    """Two consecutive pages return disjoint sets that together cover the
    seeded data with no duplicates and no skips.  This is the regression
    case that motivated keyset pagination in the first place."""
    from datetime import UTC, datetime, timedelta

    base = datetime.now(UTC)
    seeded_ids = []
    for i in range(7):
        s = _make_signal_at(base - timedelta(minutes=i))
        seeded_ids.append(s.id)
        await in_memory_uow.argus_signals.add(s)

    # Page 1
    r1 = await async_client_reviewer.get("/api/v1/argus/signals/page?limit=3")
    assert r1.status_code == 200
    p1 = r1.json()
    page1_ids = [item["id"] for item in p1["items"]]
    assert len(page1_ids) == 3

    # Page 2
    r2 = await async_client_reviewer.get(
        f"/api/v1/argus/signals/page?limit=3&cursor={p1['pagination']['next_cursor']}"
    )
    assert r2.status_code == 200
    p2 = r2.json()
    page2_ids = [item["id"] for item in p2["items"]]
    assert len(page2_ids) == 3

    # Page 3 (final, partial)
    r3 = await async_client_reviewer.get(
        f"/api/v1/argus/signals/page?limit=3&cursor={p2['pagination']['next_cursor']}"
    )
    assert r3.status_code == 200
    p3 = r3.json()
    page3_ids = [item["id"] for item in p3["items"]]
    assert len(page3_ids) == 1
    assert p3["pagination"]["next_cursor"] is None

    # No overlap; union == seeded set.
    all_returned = page1_ids + page2_ids + page3_ids
    assert len(all_returned) == 7
    assert len(set(all_returned)) == 7
    assert set(all_returned) == {str(sid) for sid in seeded_ids}


@pytest.mark.asyncio
async def test_signals_page_endpoint_filters_apply_to_pagination(
    async_client_reviewer: AsyncClient, in_memory_uow
):
    """``status`` query filter narrows results before pagination kicks in."""
    from datetime import UTC, datetime, timedelta

    base = datetime.now(UTC)
    # 3 OPEN + 4 CONFIRMED, mixed timestamps.
    for i in range(3):
        await in_memory_uow.argus_signals.add(
            _make_signal_at(base - timedelta(minutes=i), status=ArgusSignalStatus.OPEN)
        )
    for i in range(4):
        await in_memory_uow.argus_signals.add(
            _make_signal_at(
                base - timedelta(minutes=10 + i),
                status=ArgusSignalStatus.CONFIRMED,
            )
        )

    resp = await async_client_reviewer.get("/api/v1/argus/signals/page?status=OPEN&limit=10")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["items"]) == 3
    assert all(item["status"] == "OPEN" for item in body["items"])


@pytest.mark.asyncio
async def test_signals_page_endpoint_rejects_oversized_limit(
    async_client_reviewer: AsyncClient,
):
    """The router caps ``limit`` at 500.  Anything larger is a 422 at the
    framework boundary."""
    resp = await async_client_reviewer.get("/api/v1/argus/signals/page?limit=10000")
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_signals_page_endpoint_requires_analyst_role(
    async_client_analyst: AsyncClient,
):
    """The endpoint is reader-level: analyst can read it."""
    resp = await async_client_analyst.get("/api/v1/argus/signals/page?limit=5")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_legacy_signals_endpoint_still_works(
    async_client_analyst: AsyncClient, in_memory_uow
):
    """The legacy bare-list endpoint stays in place; round 5 does not change
    its response shape.  Existing consumers keep working unchanged."""
    signal = _make_signal()
    await in_memory_uow.argus_signals.add(signal)
    resp = await async_client_analyst.get("/api/v1/argus/signals")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # Bare list, NOT an envelope.
    assert isinstance(body, list)
    assert any(item["id"] == str(signal.id) for item in body)
