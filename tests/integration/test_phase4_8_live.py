"""Phase 4-8 integration tests against a live PostgreSQL instance.

These exercise the SQL repositories and use cases I built this
session through the real async UnitOfWork, validating behaviours
that fakes cannot:

- The HFACS attribution partial-unique index (COALESCE sentinel)
  actually rejects duplicate category-only attributions.
- The SHELO interaction natural-key unique constraint holds.
- The NL saved-query repo round-trips JSONB filters faithfully.
- The metering rollup ``ON CONFLICT DO UPDATE`` is idempotent when
  driven through the real ComputeDailyRollups use case.

Run with::

    TEST_DATABASE_URL=postgresql+asyncpg://.../atlas_test \\
    ATLAS_ALLOW_DB_TRUNCATE=1 \\
    pytest tests/integration/test_phase4_8_live.py --run-integration
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text

from atlas.application.use_cases.metering import (
    ComputeDailyRollups,
    ComputeDailyRollupsInput,
    GetAdminUsageSummary,
    GetAdminUsageSummaryInput,
)
from atlas.domain.causality.entities import (
    EventHfacsAttribution,
    SheloClass,
    SheloFactor,
    SheloFactorInteraction,
    SheloInteractionKind,
)
from atlas.domain.causality.exceptions import (
    HfacsAttributionConflictError,
    SheloFactorInteractionConflictError,
)
from atlas.domain.metering.entities import MetricKind, UsageEvent
from atlas.domain.nl_search.entities import SavedNlQuery

pytestmark = pytest.mark.integration


async def _seed_event(uow) -> UUID:
    """Insert a minimal accident_events row and return its id."""
    event_id = uuid4()
    await uow.session.execute(
        text("INSERT INTO accident_events (id) VALUES (:id)"),
        {"id": event_id},
    )
    return event_id


async def _first_category_id(uow):
    row = await uow.session.execute(text("SELECT id FROM hfacs_categories ORDER BY code LIMIT 1"))
    return row.scalar_one()


class TestHfacsLive:
    async def test_seed_data_present(self, pg_uow) -> None:
        """The 19-row HFACS taxonomy data migration actually loaded."""
        cats = await pg_uow.hfacs_categories.list_all()
        assert len(cats) == 19
        tiers = {c.tier.value for c in cats}
        assert tiers == {
            "ORGANIZATIONAL",
            "SUPERVISION",
            "PRECONDITIONS",
            "UNSAFE_ACTS",
        }

    async def test_duplicate_attribution_rejected_by_db(self, pg_uow) -> None:
        """The COALESCE-sentinel partial unique index rejects a
        second category-only attribution for the same (event,
        category).  The SQL repo's pre-check raises the typed error
        before the index even fires."""
        event_id = await _seed_event(pg_uow)
        category_id = await _first_category_id(pg_uow)
        await pg_uow.event_hfacs_attributions.add(
            EventHfacsAttribution(
                event_id=event_id,
                category_id=category_id,
                subcategory_id=None,
                confidence=0.5,
                editor_user_id=uuid4(),
            )
        )
        with pytest.raises(HfacsAttributionConflictError):
            await pg_uow.event_hfacs_attributions.add(
                EventHfacsAttribution(
                    event_id=event_id,
                    category_id=category_id,
                    subcategory_id=None,
                    confidence=0.9,
                    editor_user_id=uuid4(),
                )
            )

    async def test_attribution_round_trips(self, pg_uow) -> None:
        event_id = await _seed_event(pg_uow)
        category_id = await _first_category_id(pg_uow)
        attribution = EventHfacsAttribution(
            event_id=event_id,
            category_id=category_id,
            confidence=0.75,
            note="approach CRM breakdown",
            editor_user_id=uuid4(),
        )
        await pg_uow.event_hfacs_attributions.add(attribution)
        await pg_uow.commit()
        fetched = await pg_uow.event_hfacs_attributions.list_for_event(event_id)
        assert len(fetched) == 1
        assert fetched[0].confidence == 0.75
        assert fetched[0].note == "approach CRM breakdown"


class TestSheloLive:
    async def test_interaction_natural_key_conflict(self, pg_uow) -> None:
        event_id = await _seed_event(pg_uow)
        f1 = SheloFactor(
            event_id=event_id,
            factor_class=SheloClass.SOFTWARE,
            label="FADEC",
            editor_user_id=uuid4(),
        )
        f2 = SheloFactor(
            event_id=event_id,
            factor_class=SheloClass.LIVEWARE,
            label="fatigue",
            editor_user_id=uuid4(),
        )
        await pg_uow.shelo_factors.add(f1)
        await pg_uow.shelo_factors.add(f2)
        await pg_uow.shelo_factor_interactions.add(
            SheloFactorInteraction(
                event_id=event_id,
                source_factor_id=f1.id,
                target_factor_id=f2.id,
                interaction_kind=SheloInteractionKind.AGGRAVATED,
                editor_user_id=uuid4(),
            )
        )
        with pytest.raises(SheloFactorInteractionConflictError):
            await pg_uow.shelo_factor_interactions.add(
                SheloFactorInteraction(
                    event_id=event_id,
                    source_factor_id=f1.id,
                    target_factor_id=f2.id,
                    interaction_kind=SheloInteractionKind.AGGRAVATED,
                    editor_user_id=uuid4(),
                )
            )

    async def test_different_kinds_coexist(self, pg_uow) -> None:
        event_id = await _seed_event(pg_uow)
        f1 = SheloFactor(
            event_id=event_id,
            factor_class=SheloClass.SOFTWARE,
            label="a",
            editor_user_id=uuid4(),
        )
        f2 = SheloFactor(
            event_id=event_id,
            factor_class=SheloClass.HARDWARE,
            label="b",
            editor_user_id=uuid4(),
        )
        await pg_uow.shelo_factors.add(f1)
        await pg_uow.shelo_factors.add(f2)
        for kind in (
            SheloInteractionKind.AGGRAVATED,
            SheloInteractionKind.PRECONDITION,
        ):
            await pg_uow.shelo_factor_interactions.add(
                SheloFactorInteraction(
                    event_id=event_id,
                    source_factor_id=f1.id,
                    target_factor_id=f2.id,
                    interaction_kind=kind,
                    editor_user_id=uuid4(),
                )
            )
        await pg_uow.commit()
        edges = await pg_uow.shelo_factor_interactions.list_for_event(event_id)
        assert len(edges) == 2


class TestNlSearchLive:
    async def test_saved_query_jsonb_round_trip(self, pg_uow) -> None:
        user_id = uuid4()
        filters = {
            "aircraft_type": "Boeing 737",
            "hfacs_category_codes": ["PRE-CRM"],
            "fatalities_min": 1,
            "free_text_remainder": "unstable approach",
        }
        await pg_uow.saved_nl_queries.add(
            SavedNlQuery(
                user_id=user_id,
                label="example",
                raw_query="737 fatal CRM unstable approach",
                frozen_filters=filters,
            )
        )
        await pg_uow.commit()
        listed = await pg_uow.saved_nl_queries.list_for_user(user_id)
        assert len(listed) == 1
        # JSONB must round-trip the dict faithfully, including the
        # nested list.
        assert listed[0].frozen_filters == filters

    async def test_cross_user_delete_returns_false(self, pg_uow) -> None:
        owner = uuid4()
        saved = SavedNlQuery(
            user_id=owner,
            label="x",
            raw_query="x",
            frozen_filters={},
        )
        await pg_uow.saved_nl_queries.add(saved)
        await pg_uow.commit()
        # Another user's delete must not remove it.
        deleted = await pg_uow.saved_nl_queries.delete_for_user(saved_id=saved.id, user_id=uuid4())
        assert deleted is False
        # Owner's delete succeeds.
        deleted_owner = await pg_uow.saved_nl_queries.delete_for_user(
            saved_id=saved.id, user_id=owner
        )
        assert deleted_owner is True


class TestMeteringLive:
    async def test_rollup_upsert_idempotent(self, pg_uow) -> None:
        """ComputeDailyRollups run twice over the same day yields one
        row per (tenant, metric, day) with a stable count — the
        ON CONFLICT DO UPDATE path against real Postgres."""
        # Seed a tenant (usage_events.tenant_id FKs to tenants).
        tenant_id = uuid4()
        await pg_uow.session.execute(
            text(
                "INSERT INTO tenants (id, slug, display_name, is_active) "
                "VALUES (:id, :slug, :dn, true)"
            ),
            {"id": tenant_id, "slug": f"t-{tenant_id.hex[:8]}", "dn": "T"},
        )
        # Two claim events that day.
        for _ in range(2):
            await pg_uow.usage_events.add(
                UsageEvent(
                    metric_kind=MetricKind.TENANT_CLAIM_INGESTED,
                    tenant_id=tenant_id,
                    recorded_at=datetime(2024, 6, 1, 10, tzinfo=UTC),
                )
            )
        await pg_uow.commit()

        day = date(2024, 6, 1)
        inp = ComputeDailyRollupsInput(day_from=day, day_to=day)
        await ComputeDailyRollups(pg_uow).execute(inp)
        # Re-run: must replace, not accumulate.
        await ComputeDailyRollups(pg_uow).execute(inp)

        row = await pg_uow.session.execute(
            text(
                "SELECT count(*), max(count) FROM usage_daily_rollups "
                "WHERE tenant_id=:tid AND metric_kind="
                "'TENANT_CLAIM_INGESTED' AND day=:day"
            ),
            {"tid": tenant_id, "day": day},
        )
        n_rows, final_count = row.one()
        assert n_rows == 1
        assert final_count == 2

    async def test_bulk_add_many_persists_all(self, pg_uow) -> None:
        """The bulk add_many path I added this session inserts every
        row in one flush."""
        tenant_id = uuid4()
        await pg_uow.session.execute(
            text(
                "INSERT INTO tenants (id, slug, display_name, is_active) "
                "VALUES (:id, :slug, :dn, true)"
            ),
            {"id": tenant_id, "slug": f"t-{tenant_id.hex[:8]}", "dn": "T"},
        )
        events = [
            UsageEvent(
                metric_kind=MetricKind.TENANT_CLAIM_INGESTED,
                tenant_id=tenant_id,
                recorded_at=datetime(2024, 6, 1, 10, tzinfo=UTC),
            )
            for _ in range(50)
        ]
        await pg_uow.usage_events.add_many(events)
        await pg_uow.commit()
        count = await pg_uow.usage_events.count_in_range(
            tenant_id=tenant_id,
            metric_kind=MetricKind.TENANT_CLAIM_INGESTED,
            start=datetime(2024, 6, 1, tzinfo=UTC),
            end=datetime(2024, 6, 2, tzinfo=UTC),
        )
        assert count == 50

    async def test_admin_summary_sentinel_maps_to_none(self, pg_uow) -> None:
        """A system-wide NL metric rolls up under the sentinel tenant
        and the admin summary maps it back to None."""
        await pg_uow.usage_events.add(
            UsageEvent(
                metric_kind=MetricKind.NL_QUERY_EXECUTED,
                tenant_id=None,
                recorded_at=datetime(2024, 6, 1, 12, tzinfo=UTC),
            )
        )
        await pg_uow.commit()
        day = date(2024, 6, 1)
        await ComputeDailyRollups(pg_uow).execute(
            ComputeDailyRollupsInput(day_from=day, day_to=day)
        )
        summary = await GetAdminUsageSummary(pg_uow).execute(
            GetAdminUsageSummaryInput(day_from=day, day_to=day)
        )
        nl_rows = [r for r in summary if r.metric_kind == MetricKind.NL_QUERY_EXECUTED]
        assert nl_rows
        assert nl_rows[0].tenant_id is None
        assert nl_rows[0].total_count == 1
