"""Regression tests for the targeted hardening pass.

Covers:
  1. ListPendingDuplicateReviews use case + repository (fake)
  2. Admin router /reviews endpoint uses use case, not raw SQL
  3. Merge claim provenance: claim_type and created_by preserved
  4. Bootstrap role validation rejects invalid roles
  5. DuplicateReviewStatus lifecycle documentation / CONFIRMED_DUPLICATE is legacy
  6. Duplicate field_name in single ingestion payload is rejected
  7. ReviewActionResponse.merge_result is populated on confirm
  8. ReviewActionRequest.action is validated by Pydantic (Literal)
"""

from __future__ import annotations

import importlib
from uuid import uuid4

import pytest

from atlas.application.dto import CurrentUser, IngestionClaimDTO
from atlas.application.use_cases.ingest_source_data import IngestSourceData
from atlas.application.use_cases.list_pending_reviews import ListPendingDuplicateReviews
from atlas.application.use_cases.merge_duplicate_events import MergeDuplicateEvents
from atlas.application.use_cases.review_duplicate import ReviewDuplicate
from atlas.domain.entities import AccidentEvent, PendingDuplicateReview, Source
from atlas.domain.enums import ClaimType, DuplicateReviewStatus, Role, SourceKind
from atlas.domain.exceptions import DomainValidationError, DuplicateClaimFieldError
from tests.domain._fake_uow import InMemoryUnitOfWork, make_settings

pytestmark = pytest.mark.asyncio

# ── Helpers ────────────────────────────────────────────────────────────────────

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


def _authed_with_uow(app, user: CurrentUser):
    from httpx import ASGITransport, AsyncClient

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


@pytest.fixture
def uow() -> InMemoryUnitOfWork:
    return InMemoryUnitOfWork()


async def _ingest(uow, src_id, event_id, field_name, field_value):
    await IngestSourceData(uow, make_settings()).execute(
        source_id=src_id,
        raw_payload={"r": field_value},
        ingestion_run_id=uuid4(),
        claims_data=[IngestionClaimDTO(field_name=field_name, field_value=field_value)],
        event_id=event_id,
    )


async def _setup_two_events(uow):
    src = Source(id=uuid4(), name="Src", kind=SourceKind.EXTERNAL, reliability_tier=1)
    await uow.sources.add(src)
    event_a = AccidentEvent(id=uuid4())
    event_b = AccidentEvent(id=uuid4())
    await uow.events.add(event_a)
    await uow.events.add(event_b)
    await _ingest(uow, src.id, event_a.id, "event_date", "2024-06-01")
    await _ingest(uow, src.id, event_b.id, "event_date", "2024-06-02")
    return src, event_a, event_b


async def _setup_review(uow):
    _src, event_a, event_b = await _setup_two_events(uow)
    review = PendingDuplicateReview(
        id=uuid4(),
        event_id_a=event_a.id,
        event_id_b=event_b.id,
        status=DuplicateReviewStatus.PENDING,
        match_score=0.6,
        matched_fields=["event_date"],
    )
    await uow.duplicate_reviews.add(review)
    return event_a, event_b, review


# ── 1. ListPendingDuplicateReviews ────────────────────────────────────────────


async def test_list_pending_returns_only_pending_reviews(uow):
    """list_pending filters to PENDING reviews only."""
    _event_a, _event_b, review = await _setup_review(uow)

    # Add a resolved review - must not appear.
    src2 = Source(id=uuid4(), name="S2", kind=SourceKind.EXTERNAL, reliability_tier=1)
    await uow.sources.add(src2)
    e_x = AccidentEvent(id=uuid4())
    e_y = AccidentEvent(id=uuid4())
    await uow.events.add(e_x)
    await uow.events.add(e_y)
    resolved = PendingDuplicateReview(
        id=uuid4(),
        event_id_a=e_x.id,
        event_id_b=e_y.id,
        status=DuplicateReviewStatus.MERGED,
        match_score=0.9,
        matched_fields=[],
    )
    await uow.duplicate_reviews.add(resolved)

    result = await ListPendingDuplicateReviews(uow).execute(limit=50)
    assert len(result) == 1
    assert result[0].id == review.id
    assert result[0].status == DuplicateReviewStatus.PENDING


async def test_list_pending_respects_limit(uow):
    """list_pending honours the limit parameter."""
    src = Source(id=uuid4(), name="S", kind=SourceKind.EXTERNAL, reliability_tier=1)
    await uow.sources.add(src)
    for _ in range(5):
        ea = AccidentEvent(id=uuid4())
        eb = AccidentEvent(id=uuid4())
        await uow.events.add(ea)
        await uow.events.add(eb)
        await uow.duplicate_reviews.add(
            PendingDuplicateReview(
                id=uuid4(),
                event_id_a=ea.id,
                event_id_b=eb.id,
                status=DuplicateReviewStatus.PENDING,
                match_score=0.6,
                matched_fields=[],
            )
        )

    result = await ListPendingDuplicateReviews(uow).execute(limit=3)
    assert len(result) == 3


async def test_list_pending_invalid_limit_raises(uow):
    with pytest.raises(DomainValidationError, match="limit"):
        await ListPendingDuplicateReviews(uow).execute(limit=0)


# ── 2. Admin /reviews endpoint uses use-case ──────────────────────────────────


async def test_admin_reviews_endpoint_uses_use_case(monkeypatch):
    """GET /api/v1/admin/reviews must work without accessing uow.session or SQLAlchemy models."""
    app = _make_app(monkeypatch)
    admin = CurrentUser(user_id=uuid4(), role=Role.ADMIN)
    async with _authed_with_uow(app, admin)[0] as client:
        resp = await client.get("/api/v1/admin/reviews")
        assert resp.status_code == 200
        body = resp.json()
        assert body["items"] == []
        # Pagination envelope shape is part of the API contract:
        # clients must read `items` and `pagination.next_cursor`.
        assert body["pagination"]["next_cursor"] is None
        assert body["pagination"]["limit"] >= 1


async def test_admin_reviews_endpoint_returns_pending(monkeypatch):
    """GET /api/v1/admin/reviews returns PENDING reviews via the use case."""
    app = _make_app(monkeypatch)
    admin = CurrentUser(user_id=uuid4(), role=Role.ADMIN)
    client, uow = _authed_with_uow(app, admin)
    _event_a, _event_b, review = await _setup_review(uow)

    async with client as c:
        resp = await c.get("/api/v1/admin/reviews", params={"limit": 10})
    assert resp.status_code == 200
    body = resp.json()
    items = body["items"]
    assert len(items) == 1
    assert items[0]["id"] == str(review.id)
    assert items[0]["status"] == "PENDING"
    assert body["pagination"]["limit"] == 10


# ── 3. Merge claim provenance ──────────────────────────────────────────────────


async def test_merge_preserves_raw_claim_type(uow):
    """Merging RAW claims keeps claim_type=RAW on the transferred claim."""
    _src, event_a, event_b = await _setup_two_events(uow)
    await MergeDuplicateEvents(uow).execute(
        source_event_id=event_b.id,
        target_event_id=event_a.id,
        resolved_by=uuid4(),
    )
    transferred = [
        c
        for c in uow.store.claims.values()
        if c.event_id == event_a.id and c.claim_type == ClaimType.RAW
    ]
    # event_a already had its own RAW claim + the transferred one = 2
    assert len(transferred) == 2


async def test_merge_preserves_manual_override_claim_type(uow):
    """Merging a MANUAL_OVERRIDE claim must NOT downgrade it to RAW."""
    src = Source(id=uuid4(), name="Src", kind=SourceKind.EXTERNAL, reliability_tier=1)
    await uow.sources.add(src)
    event_a = AccidentEvent(id=uuid4())
    event_b = AccidentEvent(id=uuid4())
    await uow.events.add(event_a)
    await uow.events.add(event_b)
    settings = make_settings()

    # Ingest a RAW claim into event_b, then manually upgrade it to MANUAL_OVERRIDE
    await IngestSourceData(uow, settings).execute(
        source_id=src.id,
        raw_payload={"r": "flight_phase_val"},
        ingestion_run_id=uuid4(),
        claims_data=[IngestionClaimDTO(field_name="flight_phase", field_value="cruise")],
        event_id=event_b.id,
    )
    # Simulate upgrade: find that claim and change its type
    override_claim = next(
        c
        for c in uow.store.claims.values()
        if c.event_id == event_b.id and c.field_name == "flight_phase"
    )
    override_claim.claim_type = ClaimType.MANUAL_OVERRIDE
    uow.store.claims[override_claim.id] = override_claim

    # Ingest a claim into event_a so it exists
    await IngestSourceData(uow, settings).execute(
        source_id=src.id,
        raw_payload={"r": "2024-01-01"},
        ingestion_run_id=uuid4(),
        claims_data=[IngestionClaimDTO(field_name="event_date", field_value="2024-01-01")],
        event_id=event_a.id,
    )

    await MergeDuplicateEvents(uow).execute(
        source_event_id=event_b.id,
        target_event_id=event_a.id,
        resolved_by=uuid4(),
    )

    transferred_override = [
        c
        for c in uow.store.claims.values()
        if c.event_id == event_a.id
        and c.field_name == "flight_phase"
        and c.claim_type == ClaimType.MANUAL_OVERRIDE
    ]
    assert len(transferred_override) == 1, (
        "MANUAL_OVERRIDE claim must remain MANUAL_OVERRIDE after merge"
    )


async def test_merge_preserves_created_by(uow):
    """Merged claims must retain the original created_by, not the merge resolver."""
    src = Source(id=uuid4(), name="Src", kind=SourceKind.EXTERNAL, reliability_tier=1)
    await uow.sources.add(src)
    event_a = AccidentEvent(id=uuid4())
    event_b = AccidentEvent(id=uuid4())
    await uow.events.add(event_a)
    await uow.events.add(event_b)
    settings = make_settings()

    original_author = uuid4()
    await IngestSourceData(uow, settings).execute(
        source_id=src.id,
        raw_payload={"r": "val"},
        ingestion_run_id=uuid4(),
        claims_data=[IngestionClaimDTO(field_name="event_date", field_value="2024-01-01")],
        event_id=event_b.id,
    )
    # Manually set the original author on the source claim
    source_claim = next(c for c in uow.store.claims.values() if c.event_id == event_b.id)
    source_claim.created_by = original_author
    uow.store.claims[source_claim.id] = source_claim

    await IngestSourceData(uow, settings).execute(
        source_id=src.id,
        raw_payload={"r": "val2"},
        ingestion_run_id=uuid4(),
        claims_data=[IngestionClaimDTO(field_name="flight_phase", field_value="landing")],
        event_id=event_a.id,
    )

    merge_resolver = uuid4()
    await MergeDuplicateEvents(uow).execute(
        source_event_id=event_b.id,
        target_event_id=event_a.id,
        resolved_by=merge_resolver,
    )

    transferred = next(
        c
        for c in uow.store.claims.values()
        if c.event_id == event_a.id and c.field_name == "event_date"
    )
    assert transferred.created_by == original_author, (
        "created_by must be preserved from the original claim, not replaced with the merge resolver"
    )
    assert transferred.created_by != merge_resolver


async def test_merge_supersede_history_records_original_claim_type(uow):
    """ClaimHistory for superseded source claims must record the original claim_type."""
    src = Source(id=uuid4(), name="Src", kind=SourceKind.EXTERNAL, reliability_tier=1)
    await uow.sources.add(src)
    event_a = AccidentEvent(id=uuid4())
    event_b = AccidentEvent(id=uuid4())
    await uow.events.add(event_a)
    await uow.events.add(event_b)
    settings = make_settings()

    await IngestSourceData(uow, settings).execute(
        source_id=src.id,
        raw_payload={"r": "a"},
        ingestion_run_id=uuid4(),
        claims_data=[IngestionClaimDTO(field_name="registration", field_value="PH-ABC")],
        event_id=event_b.id,
    )
    # Upgrade the claim to CONFIRMED
    confirmed_claim = next(c for c in uow.store.claims.values() if c.event_id == event_b.id)
    confirmed_claim.claim_type = ClaimType.CONFIRMED
    uow.store.claims[confirmed_claim.id] = confirmed_claim

    await IngestSourceData(uow, settings).execute(
        source_id=src.id,
        raw_payload={"r": "b"},
        ingestion_run_id=uuid4(),
        claims_data=[IngestionClaimDTO(field_name="event_date", field_value="2024-01-01")],
        event_id=event_a.id,
    )

    await MergeDuplicateEvents(uow).execute(
        source_event_id=event_b.id,
        target_event_id=event_a.id,
        resolved_by=uuid4(),
    )

    supersede_entries = [
        h
        for h in uow.store.claim_history
        if h.claim_id == confirmed_claim.id and h.action == "superseded"
    ]
    assert len(supersede_entries) == 1
    assert supersede_entries[0].from_claim_type == ClaimType.CONFIRMED, (
        "supersede history must record the original claim_type, not ClaimType.RAW"
    )


# ── 4. Bootstrap role validation ──────────────────────────────────────────────


async def test_role_enum_contains_expected_values():
    """Role.values() must include the canonical three roles."""
    assert "admin" in Role.values()
    assert "reviewer" in Role.values()
    assert "analyst" in Role.values()


async def test_role_enum_does_not_contain_curator():
    """'curator' is a retired role name and must not appear in Role.values()."""
    assert "curator" not in Role.values()


async def test_bootstrap_rejects_invalid_role(monkeypatch, capsys):
    """bootstrap command must reject unknown role strings before touching the DB."""
    from typer.testing import CliRunner

    from atlas.presentation.cli.commands import app as cli_app

    runner = CliRunner()
    result = runner.invoke(cli_app, ["bootstrap", "--role", "admn"])
    assert result.exit_code != 0
    # Error message should mention the invalid role
    combined = (result.output or "") + (str(result.exception) if result.exception else "")
    assert "admn" in combined or result.exit_code == 2


async def test_bootstrap_accepts_valid_roles(monkeypatch):
    """Role.values() covers what bootstrap accepts (static assertion, no DB needed)."""
    valid = Role.values()
    assert "admin" in valid
    assert "reviewer" in valid
    assert "analyst" in valid
    # Nothing outside the known three
    assert len(valid) == 3


# ── 5. DuplicateReviewStatus lifecycle ────────────────────────────────────────


async def test_confirmed_duplicate_status_exists_for_legacy():
    """CONFIRMED_DUPLICATE must still exist to avoid breaking legacy DB rows."""
    assert DuplicateReviewStatus.CONFIRMED_DUPLICATE == "CONFIRMED_DUPLICATE"


async def test_review_confirm_transitions_to_merged_not_confirmed_duplicate():
    """Confirming a review must result in status=MERGED, not CONFIRMED_DUPLICATE."""
    uow = InMemoryUnitOfWork()
    _event_a, _event_b, review = await _setup_review(uow)
    await ReviewDuplicate(uow).execute(review_id=review.id, action="CONFIRM", resolved_by=uuid4())
    updated = uow.store.duplicate_reviews[review.id]
    assert updated.status == DuplicateReviewStatus.MERGED
    assert updated.status != DuplicateReviewStatus.CONFIRMED_DUPLICATE


async def test_review_confirm_directly_transitions_to_merged(uow):
    _event_a, _event_b, review = await _setup_review(uow)
    await ReviewDuplicate(uow).execute(review_id=review.id, action="CONFIRM", resolved_by=uuid4())
    updated = uow.store.duplicate_reviews[review.id]
    # No intermediate CONFIRMED_DUPLICATE state - goes straight to MERGED
    assert updated.status == DuplicateReviewStatus.MERGED


# ── 6. Duplicate field_name in single ingestion payload ───────────────────────


async def test_duplicate_field_name_in_single_payload_raises(uow):
    """A payload with two claims for the same field_name must be rejected."""
    src = Source(id=uuid4(), name="Src", kind=SourceKind.EXTERNAL, reliability_tier=1)
    await uow.sources.add(src)

    with pytest.raises(DuplicateClaimFieldError, match="event_date"):
        await IngestSourceData(uow, make_settings()).execute(
            source_id=src.id,
            raw_payload={"r": "x"},
            ingestion_run_id=uuid4(),
            claims_data=[
                IngestionClaimDTO(field_name="event_date", field_value="2024-01-01"),
                IngestionClaimDTO(field_name="event_date", field_value="2024-06-15"),
            ],
        )


async def test_distinct_field_names_in_single_payload_are_accepted(uow):
    """A payload with distinct field_names must be accepted normally."""
    src = Source(id=uuid4(), name="Src", kind=SourceKind.EXTERNAL, reliability_tier=1)
    await uow.sources.add(src)

    event_id = await IngestSourceData(uow, make_settings()).execute(
        source_id=src.id,
        raw_payload={"r": "x"},
        ingestion_run_id=uuid4(),
        claims_data=[
            IngestionClaimDTO(field_name="event_date", field_value="2024-01-01"),
            IngestionClaimDTO(field_name="registration", field_value="PH-ABC"),
        ],
    )
    assert event_id is not None


async def test_duplicate_field_no_claims_written_on_rejection(uow):
    """When duplicate field rejection fires, no claims must have been persisted."""
    src = Source(id=uuid4(), name="Src", kind=SourceKind.EXTERNAL, reliability_tier=1)
    await uow.sources.add(src)
    claim_count_before = len(uow.store.claims)

    with pytest.raises(DuplicateClaimFieldError):
        await IngestSourceData(uow, make_settings()).execute(
            source_id=src.id,
            raw_payload={"r": "x"},
            ingestion_run_id=uuid4(),
            claims_data=[
                IngestionClaimDTO(field_name="location", field_value="Amsterdam"),
                IngestionClaimDTO(field_name="location", field_value="Rotterdam"),
            ],
        )

    # Rejection must happen before any DB writes
    assert len(uow.store.claims) == claim_count_before


async def test_multiple_duplicate_fields_all_reported(uow):
    """Error message must list all duplicated field names."""
    src = Source(id=uuid4(), name="Src", kind=SourceKind.EXTERNAL, reliability_tier=1)
    await uow.sources.add(src)

    with pytest.raises(DuplicateClaimFieldError) as exc_info:
        await IngestSourceData(uow, make_settings()).execute(
            source_id=src.id,
            raw_payload={"r": "x"},
            ingestion_run_id=uuid4(),
            claims_data=[
                IngestionClaimDTO(field_name="event_date", field_value="2024-01-01"),
                IngestionClaimDTO(field_name="registration", field_value="PH-ABC"),
                IngestionClaimDTO(field_name="event_date", field_value="2024-06-15"),
                IngestionClaimDTO(field_name="registration", field_value="N12345"),
            ],
        )
    assert "event_date" in str(exc_info.value)
    assert "registration" in str(exc_info.value)


# ── 7. ReviewActionResponse.merge_result populated on confirm ─────────────────


async def test_review_confirm_via_api_returns_merge_result(monkeypatch):
    """POST /api/v1/admin/reviews/{id}/resolve with confirm must return merge_result."""
    app = _make_app(monkeypatch)
    reviewer = CurrentUser(user_id=uuid4(), role=Role.REVIEWER)
    client, uow = _authed_with_uow(app, reviewer)

    _event_a, _event_b, review = await _setup_review(uow)

    async with client as c:
        resp = await c.post(
            f"/api/v1/admin/reviews/{review.id}/resolve",
            json={"action": "confirm", "note": "Same crash"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["action"] == "confirm"
    assert body["merge_result"] is not None
    mr = body["merge_result"]
    assert "target_event_id" in mr
    assert "source_event_id" in mr
    assert "claims_moved" in mr


async def test_review_reject_via_api_returns_no_merge_result(monkeypatch):
    """POST /api/v1/admin/reviews/{id}/resolve with reject must return merge_result=null."""
    app = _make_app(monkeypatch)
    reviewer = CurrentUser(user_id=uuid4(), role=Role.REVIEWER)
    client, uow = _authed_with_uow(app, reviewer)

    _event_a, _event_b, review = await _setup_review(uow)

    async with client as c:
        resp = await c.post(
            f"/api/v1/admin/reviews/{review.id}/resolve",
            json={"action": "reject"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["action"] == "reject"
    assert body["merge_result"] is None


# ── 8. ReviewActionRequest.action validated by Pydantic ───────────────────────


async def test_review_invalid_action_rejected_by_pydantic(monkeypatch):
    """action='approve' is not in Literal['confirm','reject'] - must return 422."""
    app = _make_app(monkeypatch)
    reviewer = CurrentUser(user_id=uuid4(), role=Role.REVIEWER)
    client, uow = _authed_with_uow(app, reviewer)

    _event_a, _event_b, review = await _setup_review(uow)

    async with client as c:
        resp = await c.post(
            f"/api/v1/admin/reviews/{review.id}/resolve",
            json={"action": "approve"},
        )
    assert resp.status_code == 422


async def test_review_valid_actions_accepted_by_pydantic(monkeypatch):
    """Both 'confirm' and 'reject' must pass Pydantic validation."""
    app = _make_app(monkeypatch)
    reviewer = CurrentUser(user_id=uuid4(), role=Role.REVIEWER)

    for action in ("confirm", "reject"):
        client, uow = _authed_with_uow(app, reviewer)
        _event_a, _event_b, review = await _setup_review(uow)
        async with client as c:
            resp = await c.post(
                f"/api/v1/admin/reviews/{review.id}/resolve",
                json={"action": action},
            )
        assert resp.status_code == 200, f"action={action!r} must return 200, got {resp.status_code}"
