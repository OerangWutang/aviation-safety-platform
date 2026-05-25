"""Regression tests for P1 bug fixes.

Bug 1 - ReviewDuplicate respects source_event_id direction
Bug 2 - Role enum replaces stray 'curator'; unknown roles are rejected
Bug 3 - Concurrent source_record_id corrections are serialised
Bug 4 - ConflictDetector enforces cross-source policy by default
"""

from __future__ import annotations

import importlib
from itertools import pairwise
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from atlas.application.dto import CurrentUser, IngestionClaimDTO
from atlas.application.use_cases.ingest_source_data import IngestSourceData
from atlas.application.use_cases.review_duplicate import ReviewDuplicate
from atlas.domain.entities import AccidentEvent, Claim, PendingDuplicateReview, Source
from atlas.domain.enums import ClaimType, DuplicateReviewStatus, Role, SourceKind
from atlas.domain.exceptions import DomainValidationError
from atlas.domain.services.conflict_detector import ConflictDetector
from tests.domain._fake_uow import InMemoryUnitOfWork, make_settings

# asyncio_mode = "auto" in pyproject.toml handles coroutine collection globally.

# ── Helpers ───────────────────────────────────────────────────────────────────

_REQUIRED_ENV = {
    "DATABASE_URL": "postgresql+asyncpg://user:pass@localhost:5432/atlas",
    "DATABASE_SYNC_URL": "postgresql://user:pass@localhost:5432/atlas",
    "POSTGRES_USER": "user",
    "POSTGRES_PASSWORD": "pass",
    "POSTGRES_DB": "atlas",
}


def _make_app(monkeypatch=None):
    """Return the session-shared domain app (monkeypatch accepted but unused)."""
    from tests.domain.conftest import _DOMAIN_SHARED_APP

    if _DOMAIN_SHARED_APP is None:
        mod = importlib.import_module("atlas.presentation.api.app")
        return mod.create_app()
    return _DOMAIN_SHARED_APP


def _authed(app, user: CurrentUser) -> AsyncClient:
    from atlas.presentation.api import dependencies

    async def _stamp(request):
        app.dependency_overrides[dependencies.get_current_user] = lambda: user

    return AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        event_hooks={"request": [_stamp]},
    )


def _authed_with_uow(app, user: CurrentUser):
    from atlas.presentation.api import dependencies

    uow = InMemoryUnitOfWork()
    app.dependency_overrides[dependencies.get_uow] = lambda: uow

    async def _stamp(request):
        app.dependency_overrides[dependencies.get_current_user] = lambda: user

    return (
        AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            event_hooks={"request": [_stamp]},
        ),
        uow,
    )


async def _setup_review(uow: InMemoryUnitOfWork):
    """Create two events and a PendingDuplicateReview for event_a/event_b."""
    src = Source(id=uuid4(), name="Src", kind=SourceKind.EXTERNAL, reliability_tier=1)
    await uow.sources.add(src)
    event_a = AccidentEvent(id=uuid4())
    event_b = AccidentEvent(id=uuid4())
    await uow.events.add(event_a)
    await uow.events.add(event_b)
    settings = make_settings()
    for event, val in [(event_a, "2024-01-01"), (event_b, "2024-01-02")]:
        await IngestSourceData(uow, settings).execute(
            source_id=src.id,
            raw_payload={"r": val},
            ingestion_run_id=uuid4(),
            claims_data=[IngestionClaimDTO(field_name="event_date", field_value=val)],
            event_id=event.id,
        )
    review = PendingDuplicateReview(
        id=uuid4(),
        event_id_a=event_a.id,
        event_id_b=event_b.id,
        status=DuplicateReviewStatus.PENDING,
        match_score=0.85,
        matched_fields=["event_date"],
    )
    await uow.duplicate_reviews.add(review)
    return event_a, event_b, review


# ─────────────────────────────────────────────────────────────────────────────
# Bug 1 - ReviewDuplicate merge direction
# ─────────────────────────────────────────────────────────────────────────────


async def test_review_confirm_default_absorbs_event_b_into_event_a():
    """Without source_event_id, the default is B->A (newcomer absorbed)."""
    uow = InMemoryUnitOfWork()
    event_a, event_b, review = await _setup_review(uow)

    await ReviewDuplicate(uow).execute(
        review_id=review.id,
        action="CONFIRM",
        resolved_by=uuid4(),
    )

    reloaded_a = await uow.events.get(event_a.id)
    reloaded_b = await uow.events.get(event_b.id)
    # Default direction: B absorbed into A; A survives.
    assert reloaded_b.is_merged, "event_b should be absorbed"
    assert not reloaded_a.is_merged, "event_a should survive"
    assert reloaded_b.merged_into_event_id == event_a.id


async def test_review_confirm_explicit_source_event_id_a_absorbs_a_into_b():
    """Specifying source_event_id=A reverses the direction: A is absorbed into B."""
    uow = InMemoryUnitOfWork()
    event_a, event_b, review = await _setup_review(uow)

    await ReviewDuplicate(uow).execute(
        review_id=review.id,
        action="CONFIRM",
        resolved_by=uuid4(),
        source_event_id=event_a.id,  # absorb A into B
    )

    reloaded_a = await uow.events.get(event_a.id)
    reloaded_b = await uow.events.get(event_b.id)
    assert reloaded_a.is_merged, "event_a should be absorbed"
    assert not reloaded_b.is_merged, "event_b should survive"
    assert reloaded_a.merged_into_event_id == event_b.id


async def test_review_confirm_explicit_source_event_id_b_keeps_default():
    """Explicitly specifying source_event_id=B gives the same result as the default."""
    uow = InMemoryUnitOfWork()
    event_a, event_b, review = await _setup_review(uow)

    await ReviewDuplicate(uow).execute(
        review_id=review.id,
        action="CONFIRM",
        resolved_by=uuid4(),
        source_event_id=event_b.id,
    )

    reloaded_b = await uow.events.get(event_b.id)
    assert reloaded_b.is_merged
    assert reloaded_b.merged_into_event_id == event_a.id


async def test_review_confirm_invalid_source_event_id_raises():
    """A source_event_id that is not part of the review raises DomainValidationError."""
    uow = InMemoryUnitOfWork()
    _, _, review = await _setup_review(uow)

    with pytest.raises(DomainValidationError, match="not part of review"):
        await ReviewDuplicate(uow).execute(
            review_id=review.id,
            action="CONFIRM",
            resolved_by=uuid4(),
            source_event_id=uuid4(),  # random UUID - not in the review
        )


async def test_review_reject_ignores_source_event_id():
    """REJECT always succeeds regardless of source_event_id."""
    uow = InMemoryUnitOfWork()
    event_a, event_b, review = await _setup_review(uow)

    await ReviewDuplicate(uow).execute(
        review_id=review.id,
        action="REJECT",
        resolved_by=uuid4(),
        source_event_id=event_a.id,  # should be ignored
    )

    reloaded_review = await uow.duplicate_reviews.get(review.id)
    assert reloaded_review.status == DuplicateReviewStatus.REJECTED
    assert not (await uow.events.get(event_a.id)).is_merged
    assert not (await uow.events.get(event_b.id)).is_merged


# ─────────────────────────────────────────────────────────────────────────────
# Bug 1 - admin router wires source_event_id through
# ─────────────────────────────────────────────────────────────────────────────


async def test_review_api_endpoint_passes_source_event_id(monkeypatch):
    """The admin endpoint passes source_event_id into ReviewDuplicate.execute()."""
    app = _make_app(monkeypatch)
    uow = InMemoryUnitOfWork()
    event_a, _event_b, review = await _setup_review(uow)

    admin = CurrentUser(user_id=uuid4(), role=Role.ADMIN)
    async with _authed_with_uow(app, admin)[0] as c:
        # Override uow after _authed_with_uow set it so we can use our seeded uow.
        from atlas.presentation.api import dependencies

        app.dependency_overrides[dependencies.get_uow] = lambda: uow
        r = await c.post(
            f"/api/v1/admin/reviews/{review.id}/resolve",
            json={
                "action": "confirm",
                "source_event_id": str(event_a.id),
            },
        )
    assert r.status_code == 200, r.text
    reloaded_a = await uow.events.get(event_a.id)
    assert reloaded_a.is_merged, "event_a should have been absorbed per source_event_id"


# ─────────────────────────────────────────────────────────────────────────────
# Bug 2 - Role enum / no 'curator' role
# ─────────────────────────────────────────────────────────────────────────────


def test_role_enum_has_expected_values():
    assert Role.ANALYST == "analyst"
    assert Role.REVIEWER == "reviewer"
    assert Role.ADMIN == "admin"


def test_role_enum_does_not_include_curator():
    assert "curator" not in Role.values()


async def test_reviewer_can_access_reviews_endpoint(monkeypatch):
    """reviewer (not curator) is the correct role for the reviews endpoint.

    list_pending_reviews accesses uow.session directly (raw SQLAlchemy), so we
    inject a minimal fake UoW that exposes a 'session' attribute to avoid the
    AttributeError.  We care only that auth passes (not 403), not that the
    query returns real data.
    """
    from unittest.mock import AsyncMock, MagicMock

    app = _make_app(monkeypatch)
    reviewer = CurrentUser(user_id=uuid4(), role=Role.REVIEWER)
    uow = InMemoryUnitOfWork()
    # list_pending_reviews reaches into uow.session directly; stub it out.
    mock_session = MagicMock()
    mock_scalars = MagicMock(return_value=[])
    mock_session.execute = AsyncMock(return_value=MagicMock(scalars=lambda: mock_scalars))
    uow.session = mock_session  # type: ignore[attr-defined]
    async with _authed_with_uow(app, reviewer)[0] as c:
        from atlas.presentation.api import dependencies

        app.dependency_overrides[dependencies.get_uow] = lambda: uow
        r = await c.get("/api/v1/admin/reviews")
    assert r.status_code not in (401, 403), f"reviewer should not be forbidden; got {r.status_code}"


async def test_analyst_cannot_access_reviews_endpoint(monkeypatch):
    """analyst must not be able to list or resolve duplicate reviews."""
    app = _make_app(monkeypatch)
    analyst = CurrentUser(user_id=uuid4(), role=Role.ANALYST)
    async with _authed(app, analyst) as c:
        r = await c.get("/api/v1/admin/reviews")
    assert r.status_code == 403


async def test_curator_role_rejected_by_auth(monkeypatch):
    """A key with role='curator' should be rejected because curator is not a known role.

    The auth layer validates the role against Role.values() and returns 403 for
    any unknown role, so a misconfigured seed entry can never silently grant
    or deny access.
    """
    app = _make_app(monkeypatch)
    # We bypass the DB auth entirely and inject a CurrentUser with curator role.
    # The require_role dependency will reject it because 'curator' ∉ Role.values().
    curator = CurrentUser(user_id=uuid4(), role="curator")
    async with _authed(app, curator) as c:
        r = await c.get("/api/v1/admin/reviews")
    assert r.status_code == 403


# ─────────────────────────────────────────────────────────────────────────────
# Bug 3 - Source-record advisory lock (unit-test coverage; SQL test is
#          integration-only but the interface and no-op stub are verified here)
# ─────────────────────────────────────────────────────────────────────────────


async def test_source_record_correction_lock_is_called(monkeypatch):
    """lock_for_source_record_correction is invoked before reading the prior snapshot."""
    uow = InMemoryUnitOfWork()
    lock_calls: list[tuple] = []

    original_lock = uow.snapshots.lock_for_source_record_correction

    async def spy_lock(source_id, source_record_id):
        lock_calls.append((source_id, source_record_id))
        return await original_lock(source_id, source_record_id)

    uow.snapshots.lock_for_source_record_correction = spy_lock  # type: ignore[method-assign]

    settings = make_settings()
    src = Source(id=uuid4(), name="S", kind=SourceKind.EXTERNAL, reliability_tier=1)
    await uow.sources.add(src)

    await IngestSourceData(uow, settings).execute(
        source_id=src.id,
        raw_payload={"x": 1},
        ingestion_run_id=uuid4(),
        claims_data=[IngestionClaimDTO(field_name="event_date", field_value="2024-06-01")],
        source_record_id="record-001",
    )

    # Lock must have been acquired exactly once for this source_record_id.
    assert len(lock_calls) == 1
    assert lock_calls[0] == (src.id, "record-001")


async def test_second_correction_for_same_source_record_supersedes_first():
    """A second correction for the same source_record_id leaves only new claims active."""
    uow = InMemoryUnitOfWork()
    settings = make_settings()
    src = Source(id=uuid4(), name="S", kind=SourceKind.EXTERNAL, reliability_tier=1)
    await uow.sources.add(src)

    # First ingestion.
    event_id = await IngestSourceData(uow, settings).execute(
        source_id=src.id,
        raw_payload={"fatalities": 3},
        ingestion_run_id=uuid4(),
        claims_data=[IngestionClaimDTO(field_name="fatalities_total", field_value=3)],
        source_record_id="rec-A",
    )

    # Second ingestion - correction with a different value.
    event_id2 = await IngestSourceData(uow, settings).execute(
        source_id=src.id,
        raw_payload={"fatalities": 5},
        ingestion_run_id=uuid4(),
        claims_data=[IngestionClaimDTO(field_name="fatalities_total", field_value=5)],
        source_record_id="rec-A",
    )

    assert event_id == event_id2, "Corrections must attach to the same event"

    active = [
        c
        for c in uow.store.claims.values()
        if c.event_id == event_id and c.field_name == "fatalities_total" and c.is_active
    ]
    assert len(active) == 1, "Exactly one active claim per field after correction"
    assert active[0].field_value == 5


# ─────────────────────────────────────────────────────────────────────────────
# Bug 4 - Cross-source conflict policy
# ─────────────────────────────────────────────────────────────────────────────


def _make_claim(event_id, source_id, field, value):
    return Claim(
        id=uuid4(),
        event_id=event_id,
        source_id=source_id,
        raw_snapshot_id=uuid4(),
        field_name=field,
        field_value=value,
        claim_type=ClaimType.RAW,
    )


def test_conflict_detector_requires_multiple_sources_by_default():
    """Two claims from the same source with different values -> NO conflict by default."""
    eid = uuid4()
    sid = uuid4()
    claims = [
        _make_claim(eid, sid, "fatalities_total", 3),
        _make_claim(eid, sid, "fatalities_total", 5),
    ]
    # Default policy: require_multiple_sources=True
    conflicts = ConflictDetector().detect(claims)
    assert conflicts == [], (
        "Same-source contradiction should not produce a conflict under cross-source policy"
    )


def test_conflict_detector_raises_conflict_across_two_sources():
    """Two claims from *different* sources with different values -> conflict."""
    eid = uuid4()
    claims = [
        _make_claim(eid, uuid4(), "fatalities_total", 3),
        _make_claim(eid, uuid4(), "fatalities_total", 5),
    ]
    conflicts = ConflictDetector().detect(claims)
    assert len(conflicts) == 1
    assert conflicts[0].field_name == "fatalities_total"


def test_conflict_detector_single_source_mode_raises_self_conflict():
    """Opt-in single-source mode still surfaces same-source contradictions."""
    eid = uuid4()
    sid = uuid4()
    claims = [
        _make_claim(eid, sid, "fatalities_total", 3),
        _make_claim(eid, sid, "fatalities_total", 5),
    ]
    conflicts = ConflictDetector(require_multiple_sources=False).detect(claims)
    assert len(conflicts) == 1


def test_conflict_detector_no_conflict_when_values_agree():
    """Same value from two different sources -> no conflict."""
    eid = uuid4()
    claims = [
        _make_claim(eid, uuid4(), "fatalities_total", 3),
        _make_claim(eid, uuid4(), "fatalities_total", 3),
    ]
    assert ConflictDetector().detect(claims) == []


async def test_ingestion_same_source_contradictory_claims_no_conflict():
    """Same-source replacement without source_record_id supersedes old evidence.

    The default conflict policy only surfaces cross-source disagreement. Repeated
    ingestion from the same source without a stable source_record_id should not
    leave two active contradictory claims behind; the older same-source claim is
    superseded by the newer one.
    """
    uow = InMemoryUnitOfWork()
    settings = make_settings()
    src = Source(id=uuid4(), name="S", kind=SourceKind.EXTERNAL, reliability_tier=1)
    await uow.sources.add(src)

    event_id = await IngestSourceData(uow, settings).execute(
        source_id=src.id,
        raw_payload={"f": 3},
        ingestion_run_id=uuid4(),
        claims_data=[IngestionClaimDTO(field_name="fatalities_total", field_value=3)],
    )
    original_claim = next(iter(uow.store.claims.values()))

    await IngestSourceData(uow, settings).execute(
        source_id=src.id,
        raw_payload={"f": 5},
        ingestion_run_id=uuid4(),
        claims_data=[IngestionClaimDTO(field_name="fatalities_total", field_value=5)],
        event_id=event_id,
    )

    old_claim = await uow.claims.get(original_claim.id)
    assert old_claim is not None
    assert old_claim.claim_type == ClaimType.SUPERSEDED
    assert old_claim.superseded_by_claim_id is not None

    active_claims = await uow.claims.find_active_by_event_field(event_id, "fatalities_total")
    assert len(active_claims) == 1
    assert active_claims[0].field_value == 5

    event_conflicts = [c for c in uow.store.conflicts.values() if c.event_id == event_id]
    assert event_conflicts == [], "Single-source replacement should not create a conflict"


async def test_ingestion_two_sources_contradictory_claims_creates_conflict():
    """Two different sources asserting different values DOES create a conflict."""
    uow = InMemoryUnitOfWork()
    settings = make_settings()
    src_a = Source(id=uuid4(), name="A", kind=SourceKind.EXTERNAL, reliability_tier=1)
    src_b = Source(id=uuid4(), name="B", kind=SourceKind.EXTERNAL, reliability_tier=2)
    await uow.sources.add(src_a)
    await uow.sources.add(src_b)

    event_id = await IngestSourceData(uow, settings).execute(
        source_id=src_a.id,
        raw_payload={"f": 3},
        ingestion_run_id=uuid4(),
        claims_data=[IngestionClaimDTO(field_name="fatalities_total", field_value=3)],
    )
    await IngestSourceData(uow, settings).execute(
        source_id=src_b.id,
        raw_payload={"f": 5},
        ingestion_run_id=uuid4(),
        claims_data=[IngestionClaimDTO(field_name="fatalities_total", field_value=5)],
        event_id=event_id,
    )

    event_conflicts = [c for c in uow.store.conflicts.values() if c.event_id == event_id]
    assert len(event_conflicts) == 1
    assert event_conflicts[0].field_name == "fatalities_total"


# ─────────────────────────────────────────────────────────────────────────────
# QueryProvenance follows multi-hop merge chains
# ─────────────────────────────────────────────────────────────────────────────


async def test_query_provenance_follows_multi_hop_merge_chain():
    from atlas.application.use_cases.query_provenance import QueryProvenance

    uow = InMemoryUnitOfWork()
    event_a = AccidentEvent(id=uuid4())
    event_b = AccidentEvent(id=uuid4())
    event_c = AccidentEvent(id=uuid4())
    event_a.merged_into_event_id = event_b.id
    event_b.merged_into_event_id = event_c.id
    await uow.events.add(event_a)
    await uow.events.add(event_b)
    await uow.events.add(event_c)

    result = await QueryProvenance(uow).execute(event_a.id, canonicalize=True)

    assert result["event_id"] == event_c.id
    assert result["absorbed_event_id"] == event_a.id
    assert result["canonicalized"] is True


async def test_query_provenance_can_skip_canonicalization_for_merged_event():
    from atlas.application.use_cases.query_provenance import QueryProvenance

    uow = InMemoryUnitOfWork()
    event_a = AccidentEvent(id=uuid4())
    event_b = AccidentEvent(id=uuid4())
    event_a.merged_into_event_id = event_b.id
    await uow.events.add(event_a)
    await uow.events.add(event_b)

    result = await QueryProvenance(uow).execute(event_a.id, canonicalize=False)

    assert result["event_id"] == event_a.id
    assert result["absorbed_event_id"] is None
    assert result["canonicalized"] is False


async def test_query_provenance_merge_chain_hop_limit_raises():
    from atlas.application.use_cases.query_provenance import QueryProvenance
    from atlas.domain.exceptions import EventAlreadyMergedError

    uow = InMemoryUnitOfWork()
    events = [AccidentEvent(id=uuid4()) for _ in range(17)]
    for source, target in pairwise(events):
        source.merged_into_event_id = target.id
    for event in events:
        await uow.events.add(event)

    with pytest.raises(EventAlreadyMergedError, match="16-hop safety limit"):
        await QueryProvenance(uow).execute(events[0].id, canonicalize=True)
