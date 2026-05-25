"""Integration test: iter_all_claims() SQL streaming path.

This is the test that was identified as missing before the frontend handoff
review.  ``iter_all_claims()`` uses SQLAlchemy's ``session.stream()`` with
``yield_per=500`` — server-side cursor streaming that was written and reviewed
but never executed against a real database.

This test:
1. Inserts two projected_accident_records rows directly (bypassing the full
   ingestion pipeline) using the realistic NTSB-vocabulary fields format.
2. Calls ``iter_all_claims()`` on a live ``SqlProjectionRepository``.
3. Asserts the streaming query returns the correct rows in event_id order.
4. Passes each result through ``InMemoryCorpusLoader``'s mapping function to
   confirm the full production path works end-to-end.

Marked ``integration``: requires ``TEST_DATABASE_URL`` and
``--run-integration``.  The test creates its own probe rows and cleans them up
in a finally block; it does NOT require ``ATLAS_ALLOW_DB_TRUNCATE``.
"""

from __future__ import annotations

import os
import uuid

import pytest

pytest_asyncio = pytest.importorskip("pytest_asyncio")

from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

pytestmark = pytest.mark.integration

_DSN = os.environ.get("TEST_DATABASE_URL")

_FIELDS_A = {
    "occurred_on": "2020-03-15",
    "location_city": "Phoenix",
    "location_state": "AZ",
    "aircraft_make": "Cessna",
    "aircraft_model": "172S",
    "aircraft_category": "Airplane",
    "far_part": "Part 91: General Aviation",
    "highest_injury_level": "None",
    "probable_cause_narrative": "The pilot failed to maintain directional control on landing.",
    "causal_findings": [
        {
            "category_no": "01",
            "subcategory_no": "06",
            "role": "CAUSE",
            "finding_code": "0106",
            "description": "directional control",
        },
    ],
}
_FIELDS_B = {
    "occurred_on": "2021-07-22",
    "location_city": "Denver",
    "location_state": "CO",
    "aircraft_make": "Piper",
    "aircraft_model": "PA-28",
    "far_part": "Part 91: General Aviation",
    "highest_injury_level": "Fatal",
    "fatalities_total": 1,
    "probable_cause_narrative": "Fuel exhaustion due to inadequate preflight planning.",
    "causal_findings": [],
}


def _async_dsn(dsn: str) -> str:
    if "asyncpg" in dsn:
        return dsn
    return dsn.replace("postgresql://", "postgresql+asyncpg://", 1).replace(
        "postgres://", "postgresql+asyncpg://", 1
    )


@pytest_asyncio.fixture
async def engine():
    if not _DSN:
        pytest.skip("TEST_DATABASE_URL not set")
    # Check the projected_accident_records table exists (requires full schema).
    eng = create_async_engine(_async_dsn(_DSN))
    async with eng.connect() as conn:
        exists = (
            await conn.execute(
                text(
                    "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
                    "WHERE table_name = 'projected_accident_records')"
                )
            )
        ).scalar_one()
    if not exists:
        await eng.dispose()
        pytest.skip("projected_accident_records table not present (schema not migrated)")
    yield eng
    await eng.dispose()


@pytest.mark.asyncio
async def test_iter_all_claims_streams_rows_in_event_id_order(engine):
    """iter_all_claims() returns (event_id, fields) in deterministic event_id order."""
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    id_a = uuid.uuid4()
    id_b = uuid.uuid4()

    async with session_factory() as session:
        try:
            # Insert parent accident_events rows first (FK constraint), then
            # projected_accident_records probe rows.
            from atlas.infrastructure.db.orm_models import (
                AccidentEventModel,
                ProjectedAccidentRecordModel,
            )

            session.add(AccidentEventModel(id=id_a))
            session.add(AccidentEventModel(id=id_b))
            await session.flush()
            session.add(
                ProjectedAccidentRecordModel(
                    event_id=id_a,
                    fields=_FIELDS_A,
                    unresolved_conflict_fields=[],
                    completeness_score=0.9,
                )
            )
            session.add(
                ProjectedAccidentRecordModel(
                    event_id=id_b,
                    fields=_FIELDS_B,
                    unresolved_conflict_fields=[],
                    completeness_score=0.9,
                )
            )
            await session.commit()

            # Now exercise iter_all_claims() via SqlProjectionRepository.
            from atlas.infrastructure.db.repositories.projections import SqlProjectionRepository

            repo = SqlProjectionRepository(session)
            results = []
            async for event_id, fields in repo.iter_all_claims():
                if event_id in (id_a, id_b):
                    results.append((event_id, fields))

            # Should have retrieved both probe rows.
            assert len(results) == 2, f"expected 2 probe rows, got {len(results)}"

            # Rows must be in event_id (UUID string) order — same as ORDER BY event_id.
            result_ids = [str(r[0]) for r in results]
            assert result_ids == sorted(result_ids), "rows not in event_id order"

            # Fields must be the dicts we inserted, with JSONB round-trip intact.
            fields_by_id = {str(r[0]): r[1] for r in results}
            assert fields_by_id[str(id_a)]["aircraft_make"] == "Cessna"
            assert fields_by_id[str(id_b)]["aircraft_make"] == "Piper"
            assert isinstance(fields_by_id[str(id_a)]["causal_findings"], list)

        finally:
            # Clean up probe rows regardless of test outcome.
            from sqlalchemy import delete as sa_delete

            from atlas.infrastructure.db.orm_models import (
                AccidentEventModel as AEM,
            )
            from atlas.infrastructure.db.orm_models import (
                ProjectedAccidentRecordModel as PAR,
            )

            await session.execute(sa_delete(PAR).where(PAR.event_id.in_([id_a, id_b])))
            await session.execute(sa_delete(AEM).where(AEM.id.in_([id_a, id_b])))
            await session.commit()


@pytest.mark.asyncio
async def test_corpus_loader_uses_real_streaming_path(engine):
    """Full InMemoryCorpusLoader.load() path against a live database.

    This exercises the complete production path:
    create_uow() → SqlProjectionRepository.iter_all_claims()
    → precedent_record_from_ntsb_claims() → PrecedentRecord.
    """
    from atlas.application.use_cases.echo_crossref import InMemoryCorpusLoader

    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    probe_id = uuid.uuid4()

    async with session_factory() as session:
        try:
            from atlas.infrastructure.db.orm_models import (
                AccidentEventModel,
                ProjectedAccidentRecordModel,
            )

            session.add(AccidentEventModel(id=probe_id))
            await session.flush()
            session.add(
                ProjectedAccidentRecordModel(
                    event_id=probe_id,
                    fields=_FIELDS_A,
                    unresolved_conflict_fields=[],
                    completeness_score=0.9,
                )
            )
            await session.commit()

            # Build a minimal UoW-like object that wraps a real SQL repo.
            from atlas.infrastructure.db.repositories.projections import SqlProjectionRepository

            class _MinimalUoW:
                def __init__(self, s):
                    self.projections = SqlProjectionRepository(s)

            uow = _MinimalUoW(session)
            records = await InMemoryCorpusLoader().load(uow=uow)

            probe_records = [r for r in records if r.event_id == str(probe_id)]
            assert len(probe_records) == 1
            rec = probe_records[0]
            assert rec.far_part == "Part 91: General Aviation"
            assert rec.finding_categories == frozenset({"01.06"})
            assert "directional" in rec.terms or "control" in rec.terms
            assert rec.display_location == "Phoenix, AZ"

        finally:
            from sqlalchemy import delete as sa_delete

            from atlas.infrastructure.db.orm_models import (
                AccidentEventModel as AEM,
            )
            from atlas.infrastructure.db.orm_models import (
                ProjectedAccidentRecordModel as PAR,
            )

            await session.execute(sa_delete(PAR).where(PAR.event_id == probe_id))
            await session.execute(sa_delete(AEM).where(AEM.id == probe_id))
            await session.commit()
