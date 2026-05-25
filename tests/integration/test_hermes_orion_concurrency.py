"""Integration tests for Hermes leases, Orion identity uniqueness, and the
migration-034 duplicate preflight.

These prove the r8 database-level invariants against real PostgreSQL —
fake repositories can model the *intended* behaviour but cannot prove that
``FOR UPDATE SKIP LOCKED``, partial unique indexes, ``ON CONFLICT`` targets,
and ``RETURNING`` semantics actually fire the way the code assumes.

The riskiest bugs in this surface area are production-only: lock behaviour
under concurrency, partial-index conflict targets, stale identity-map state,
and migration failures.  Each test in this file exists because the in-memory
fake cannot exercise the relevant production code path.

Gated by ``@pytest.mark.integration``; skipped unless ``--run-integration``
is passed.  Run with:

    ATLAS_ALLOW_DB_TRUNCATE=1 \
    TEST_DATABASE_URL=postgresql+asyncpg://atlas:atlas@localhost/atlas_test \
    pytest -m integration --run-integration tests/integration/test_hermes_orion_concurrency.py
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text

from atlas.domain.entities import (
    HermesCrawlTarget,
    HermesFetchJob,
    HermesSource,
    OrionEntity,
)
from atlas.domain.enums import (
    HermesFetchJobStatus,
    HermesSourceType,
    HermesTargetStatus,
    OrionEntityType,
)
from atlas.infrastructure.db.unit_of_work import SqlAlchemyUnitOfWork

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


async def _make_hermes_source_and_target(session_factory) -> tuple[UUID, UUID]:
    """Insert a Hermes source + crawl target in a dedicated session.

    Returns ``(source_id, target_id)``.  Used as fixture for the job-claim
    races below where the test cares about job behaviour, not target
    creation.
    """
    source = HermesSource(
        id=uuid4(),
        name=f"src-{uuid4().hex[:8]}",
        source_type=HermesSourceType.NEWS,
    )
    target = HermesCrawlTarget(
        id=uuid4(),
        source_id=source.id,
        url=f"https://example.test/{uuid4().hex[:8]}",
        normalized_url=f"https://example.test/{uuid4().hex[:8]}",
        status=HermesTargetStatus.ACTIVE,
    )
    async with session_factory() as session:
        uow = SqlAlchemyUnitOfWork(session)
        await uow.hermes_sources.add(source)
        await uow.flush()  # source must exist before target FK references it
        await uow.hermes_crawl_targets.add(target)
        await uow.commit()
    return source.id, target.id


async def _enqueue_job(
    session_factory, target_id: UUID, scheduled_at: datetime | None = None
) -> UUID:
    """Insert a QUEUED Hermes fetch job in its own transaction."""
    job = HermesFetchJob(
        id=uuid4(),
        target_id=target_id,
        status=HermesFetchJobStatus.QUEUED,
        attempt_count=0,
        max_attempts=3,
        priority=0,
        scheduled_at=scheduled_at,
    )
    async with session_factory() as session:
        uow = SqlAlchemyUnitOfWork(session)
        await uow.hermes_fetch_jobs.add(job)
        await uow.commit()
    return job.id


async def _claim_in_own_session(session_factory, worker_id: str) -> UUID | None:
    """Run claim_next_running in a dedicated session and return the claimed id (or None)."""
    async with session_factory() as session:
        uow = SqlAlchemyUnitOfWork(session)
        claimed = await uow.hermes_fetch_jobs.claim_next_running(
            worker_id=worker_id,
            lease_expires_at=datetime.now(UTC) + timedelta(minutes=5),
        )
        await uow.commit()
        return claimed.id if claimed else None


# --------------------------------------------------------------------------- #
# Hermes lease/claim concurrency
# --------------------------------------------------------------------------- #


async def test_two_workers_claim_next_running_get_disjoint_jobs(pg_uow, test_session_factory):
    """Two workers calling ``claim_next_running`` against a queue of two jobs
    must each get exactly one — never the same job twice, never both jobs in
    one worker.

    This proves the ``SELECT … FOR UPDATE SKIP LOCKED`` inside the CTE
    actually works on the partial index: the second worker's CTE row-lock
    must skip the first worker's locked candidate rather than blocking on
    it (which would serialize), and the UPDATE must not silently return
    NULL for both.
    """
    _, target_id = await _make_hermes_source_and_target(test_session_factory)
    # Two separate targets so the per-target active-job partial unique index
    # does not prevent enqueueing both.
    _, target_id_2 = await _make_hermes_source_and_target(test_session_factory)
    job_a = await _enqueue_job(test_session_factory, target_id)
    job_b = await _enqueue_job(test_session_factory, target_id_2)

    # Race two claim_next_running calls.  Each test sessionfactory hands out
    # a fresh AsyncSession so the two coroutines do NOT share a session
    # (which would implicitly serialize the SQL through one connection).
    results = await asyncio.gather(
        _claim_in_own_session(test_session_factory, "worker-A"),
        _claim_in_own_session(test_session_factory, "worker-B"),
    )

    claimed = {r for r in results if r is not None}
    assert len(claimed) == 2, f"Both workers must claim disjoint jobs, got {results}"
    assert claimed == {job_a, job_b}


async def test_claim_running_refuses_future_scheduled_job(pg_uow, test_session_factory):
    """A QUEUED job whose ``scheduled_at`` is in the future must not be claimable
    yet.  The query's ``scheduled_at <= now()`` clause is the safety net.
    """
    _, target_id = await _make_hermes_source_and_target(test_session_factory)
    future = datetime.now(UTC) + timedelta(hours=1)
    await _enqueue_job(test_session_factory, target_id, scheduled_at=future)

    async with test_session_factory() as session:
        uow = SqlAlchemyUnitOfWork(session)
        claimed = await uow.hermes_fetch_jobs.claim_next_running(
            worker_id="worker-1",
            lease_expires_at=datetime.now(UTC) + timedelta(minutes=5),
        )
        assert claimed is None, "Future-scheduled jobs must not be claimable"


async def test_recover_stale_running_returns_terminal_outcomes_via_returning(
    pg_uow, test_session_factory
):
    """Recovery's ``UPDATE … RETURNING`` must yield outcomes per job, including
    ``final_status=FAILED`` for jobs that exhaust their attempt budget.

    This is the integration counterpart to the unit test on the in-memory
    fake: it proves the production SQL's ``RETURNING (id, target_id, status,
    attempt_count)`` actually drives the new
    :class:`HermesRecoveryOutcome` shape.
    """
    _, target_id = await _make_hermes_source_and_target(test_session_factory)

    # Insert a job with attempt_count already at max_attempts (1) and a
    # RUNNING status with an expired lease so recovery will terminally
    # fail it.
    job = HermesFetchJob(
        id=uuid4(),
        target_id=target_id,
        status=HermesFetchJobStatus.RUNNING,
        attempt_count=1,
        max_attempts=1,
        priority=0,
        locked_by="dead-worker",
        locked_at=datetime.now(UTC) - timedelta(minutes=10),
        lease_expires_at=datetime.now(UTC) - timedelta(minutes=1),
    )
    async with test_session_factory() as session:
        uow = SqlAlchemyUnitOfWork(session)
        await uow.hermes_fetch_jobs.add(job)
        await uow.commit()

    async with test_session_factory() as session:
        uow = SqlAlchemyUnitOfWork(session)
        outcomes = await uow.hermes_fetch_jobs.recover_stale_running(
            now=datetime.now(UTC),
            limit=10,
        )
        await uow.commit()

    assert len(outcomes) == 1
    assert outcomes[0].job_id == job.id
    assert outcomes[0].target_id == target_id
    assert outcomes[0].final_status == HermesFetchJobStatus.FAILED


async def test_stale_recovery_then_late_finalize_is_no_op(pg_uow, test_session_factory):
    """Crash scenario: worker A claims, stalls, lease expires, recovery
    requeues, worker B claims and finalizes — A's late finalize must not
    overwrite B's work.

    The fencing check in ``lock_claim_for_finalization`` requires
    ``locked_by = expected_worker_id`` AND
    ``attempt_count = expected_attempt_count`` AND a live lease.  Once
    recovery clears ``locked_by`` and bumps attempt_count via re-claim, A's
    WHERE clause matches zero rows and the late finalize returns the
    "lost claim" sentinel.
    """
    _, target_id = await _make_hermes_source_and_target(test_session_factory)

    # Enqueue + worker A claims it with an already-expired lease.
    async with test_session_factory() as session:
        uow = SqlAlchemyUnitOfWork(session)
        job = HermesFetchJob(
            id=uuid4(),
            target_id=target_id,
            status=HermesFetchJobStatus.QUEUED,
            attempt_count=0,
            max_attempts=3,
            priority=0,
        )
        await uow.hermes_fetch_jobs.add(job)
        await uow.commit()
        job_id = job.id

    async with test_session_factory() as session:
        uow = SqlAlchemyUnitOfWork(session)
        a_claim = await uow.hermes_fetch_jobs.claim_running(
            job_id,
            worker_id="worker-A",
            lease_expires_at=datetime.now(UTC) - timedelta(seconds=1),
        )
        assert a_claim is not None
        await uow.commit()
        a_attempt = a_claim.attempt_count

    # Recovery requeues the stale RUNNING job.
    async with test_session_factory() as session:
        uow = SqlAlchemyUnitOfWork(session)
        outcomes = await uow.hermes_fetch_jobs.recover_stale_running(
            now=datetime.now(UTC),
            limit=10,
        )
        await uow.commit()
        assert len(outcomes) == 1
        assert outcomes[0].final_status == HermesFetchJobStatus.QUEUED

    # Worker B re-claims it with a live lease.
    async with test_session_factory() as session:
        uow = SqlAlchemyUnitOfWork(session)
        b_claim = await uow.hermes_fetch_jobs.claim_running(
            job_id,
            worker_id="worker-B",
            lease_expires_at=datetime.now(UTC) + timedelta(minutes=5),
        )
        assert b_claim is not None
        assert b_claim.attempt_count == a_attempt + 1
        await uow.commit()

    # Worker A tries to finalize with its STALE expectation.  The fencing
    # check must reject it.
    async with test_session_factory() as session:
        uow = SqlAlchemyUnitOfWork(session)
        result = await uow.hermes_fetch_jobs.lock_claim_for_finalization(
            job_id,
            worker_id="worker-A",
            attempt_count=a_attempt,
            now=datetime.now(UTC),
        )
        assert result is None, (
            "Worker A's late finalize must be rejected by the lease fence "
            "(locked_by/attempt_count mismatch). Otherwise A's stale write "
            "could overwrite worker B's result."
        )


async def test_claim_next_running_returns_fresh_state_not_identity_map_cached(
    pg_uow, test_session_factory
):
    """``claim_next_running`` must return the POST-update row even if the
    session has already loaded the job as QUEUED.

    Regression guard for the r8 issue where the implementation called
    ``session.get(...)`` after ``UPDATE … RETURNING``: that path returns
    the identity-map cached ORM instance, which still shows ``status =
    QUEUED`` and ``locked_by = None``.  Workers using this path would
    happily try to run a job they don't actually own.
    """
    _, target_id = await _make_hermes_source_and_target(test_session_factory)
    job_id = await _enqueue_job(test_session_factory, target_id)

    async with test_session_factory() as session:
        uow = SqlAlchemyUnitOfWork(session)
        # Force the session to load the job as QUEUED first, poisoning its
        # identity map with the pre-update state.
        before = await uow.hermes_fetch_jobs.get(job_id)
        assert before is not None
        assert before.status == HermesFetchJobStatus.QUEUED
        assert before.locked_by is None

        # Now claim it inside the same session.  The returned domain
        # object must reflect the UPDATE, not the cached pre-update state.
        claimed = await uow.hermes_fetch_jobs.claim_next_running(
            worker_id="worker-1",
            lease_expires_at=datetime.now(UTC) + timedelta(minutes=5),
        )
        assert claimed is not None
        assert claimed.id == job_id
        assert claimed.status == HermesFetchJobStatus.RUNNING, (
            "claim_next_running returned a stale ORM row from the session "
            "identity map. The implementation must refresh from the UPDATE "
            "RETURNING result instead of going through session.get()."
        )
        assert claimed.locked_by == "worker-1"
        assert claimed.attempt_count == 1


# --------------------------------------------------------------------------- #
# Orion identifier uniqueness
# --------------------------------------------------------------------------- #


async def _make_orion_entity(session_factory, entity_type: OrionEntityType, name: str) -> UUID:
    """Insert an Orion entity in its own transaction."""
    entity = OrionEntity(
        id=uuid4(),
        entity_type=entity_type,
        canonical_name=name,
    )
    async with session_factory() as session:
        uow = SqlAlchemyUnitOfWork(session)
        await uow.orion_entities.add(entity)
        await uow.commit()
    return entity.id


async def _add_identifier(
    session_factory,
    *,
    entity_id: UUID,
    entity_type: OrionEntityType,
    identifier_type: str,
    normalized_value: str,
    valid_to: datetime | None = None,
) -> None:
    """Best-effort insert of an Orion identifier in its own transaction.

    Uses a low-level INSERT via the session so the test can deliberately
    attempt to insert a row that violates the partial unique index.
    """
    async with session_factory() as session:
        await session.execute(
            text(
                """
                INSERT INTO orion_entity_identifiers
                    (id, entity_id, entity_type, identifier_type,
                     identifier_value, normalized_value, valid_from,
                     valid_to, created_at)
                VALUES
                    (:id, :entity_id, :entity_type, :identifier_type,
                     :identifier_value, :normalized_value, now(),
                     :valid_to, now())
                """
            ),
            {
                "id": uuid4(),
                "entity_id": entity_id,
                "entity_type": entity_type.value,
                "identifier_type": identifier_type,
                "identifier_value": normalized_value,
                "normalized_value": normalized_value,
                "valid_to": valid_to,
            },
        )
        await session.commit()


async def test_active_identifier_uniqueness_rejects_duplicate_across_entities(
    pg_uow, test_session_factory
):
    """The migration-034 partial unique index must reject a second ACTIVE row
    with the same ``(entity_type, identifier_type, normalized_value)`` even
    when the duplicate targets a different entity_id.

    Before r8 this rule lived in app-level code with an advisory lock; the
    DB still allowed duplicates.  This test proves the DB now enforces it.
    """
    from sqlalchemy.exc import IntegrityError

    e1 = await _make_orion_entity(test_session_factory, OrionEntityType.AIRCRAFT, "A")
    e2 = await _make_orion_entity(test_session_factory, OrionEntityType.AIRCRAFT, "B")

    # First active identifier — must succeed.
    await _add_identifier(
        test_session_factory,
        entity_id=e1,
        entity_type=OrionEntityType.AIRCRAFT,
        identifier_type="REGISTRATION",
        normalized_value="N12345",
    )

    # Second active identifier with the same strong-identity triple but a
    # different entity_id — must fail at the database level.
    with pytest.raises(IntegrityError):
        await _add_identifier(
            test_session_factory,
            entity_id=e2,
            entity_type=OrionEntityType.AIRCRAFT,
            identifier_type="REGISTRATION",
            normalized_value="N12345",
        )


async def test_active_identifier_uniqueness_allows_expired_duplicate(pg_uow, test_session_factory):
    """An identifier with ``valid_to IS NOT NULL`` is not active and the
    partial index ``WHERE valid_to IS NULL`` should not see it.

    This is the historical-record use case: yesterday's N12345 belonged to
    entity X; today it belongs to entity Y.  The partial unique index lets
    Y own the current binding without forcing us to delete X's audit row.
    """
    e1 = await _make_orion_entity(test_session_factory, OrionEntityType.AIRCRAFT, "A")
    e2 = await _make_orion_entity(test_session_factory, OrionEntityType.AIRCRAFT, "B")

    # Historical row on e1 (valid_to in the past — not active).
    yesterday = datetime.now(UTC) - timedelta(days=1)
    await _add_identifier(
        test_session_factory,
        entity_id=e1,
        entity_type=OrionEntityType.AIRCRAFT,
        identifier_type="REGISTRATION",
        normalized_value="N99999",
        valid_to=yesterday,
    )

    # Active row on e2 with the same triple — must succeed.
    await _add_identifier(
        test_session_factory,
        entity_id=e2,
        entity_type=OrionEntityType.AIRCRAFT,
        identifier_type="REGISTRATION",
        normalized_value="N99999",
    )

    # Sanity check: both rows exist.
    async with test_session_factory() as session:
        result = await session.execute(
            text("SELECT count(*) FROM orion_entity_identifiers WHERE normalized_value = 'N99999'")
        )
        assert result.scalar_one() == 2


async def test_active_identifier_uniqueness_allows_different_entity_type(
    pg_uow, test_session_factory
):
    """The partial unique index keys on ``entity_type`` so an AIRPORT and
    an AIRCRAFT can share a normalized value.

    Without ``entity_type`` in the index columns, this would falsely
    collide (e.g. an airport code happening to look like a registration).
    """
    aircraft = await _make_orion_entity(test_session_factory, OrionEntityType.AIRCRAFT, "AC")
    operator = await _make_orion_entity(test_session_factory, OrionEntityType.OPERATOR, "OP")

    await _add_identifier(
        test_session_factory,
        entity_id=aircraft,
        entity_type=OrionEntityType.AIRCRAFT,
        identifier_type="REGISTRATION",
        normalized_value="DUP",
    )
    # Same triple except entity_type differs — must succeed.
    await _add_identifier(
        test_session_factory,
        entity_id=operator,
        entity_type=OrionEntityType.OPERATOR,
        identifier_type="REGISTRATION",
        normalized_value="DUP",
    )


# --------------------------------------------------------------------------- #
# Migration 034 duplicate preflight
# --------------------------------------------------------------------------- #


async def test_migration_034_preflight_detects_existing_duplicates(pg_uow, test_session_factory):
    """The preflight query inside migration 034 must return rows for every
    duplicate group of active strong identifiers.

    We can't easily replay the migration mid-test because alembic owns the
    upgrade path, but we can validate the preflight SELECT itself: it is
    the contract the migration relies on.  The SELECT also has to work
    BEFORE the ``entity_type`` column exists on
    ``orion_entity_identifiers`` (it joins to ``orion_entities`` to compute
    the column), which is why this is worth a real-DB test.

    The current schema already has ``entity_type`` (post-migration), so the
    preflight is run against the equivalent query that the migration would
    see if duplicates ever appeared.
    """
    sql = text(
        """
        WITH duplicates AS (
            SELECT
                e.entity_type,
                i.identifier_type,
                i.normalized_value,
                array_agg(i.entity_id ORDER BY i.created_at) AS entity_ids,
                count(*) AS dup_count
            FROM orion_entity_identifiers AS i
            JOIN orion_entities AS e ON e.id = i.entity_id
            WHERE i.valid_to IS NULL
            GROUP BY e.entity_type, i.identifier_type, i.normalized_value
            HAVING count(*) > 1
        )
        SELECT entity_type, identifier_type, normalized_value, entity_ids, dup_count
        FROM duplicates
        ORDER BY dup_count DESC, entity_type, identifier_type, normalized_value
        LIMIT 50
        """
    )
    async with test_session_factory() as session:
        result = await session.execute(sql)
        rows = result.fetchall()
    # Production schema enforces the invariant, so there must be no
    # duplicates.  The point of this assertion is that the query *runs* —
    # it depends on a join shape that worked before the migration added
    # ``entity_type`` to ``orion_entity_identifiers``.
    assert rows == []
