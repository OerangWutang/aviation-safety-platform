"""Structural invariant tests for the Phase 4-8 surfaces.

These pin architectural guarantees that are otherwise only enforced
by convention and code review.  Each test reads source files and
asserts a property that, if violated, would be a privacy or
correctness regression invisible to behavioural tests.

The recurring principle: guarantees should be structural, not
procedural.  Where we can't enforce an invariant in the type system
or the schema, we enforce it with a test that fails the build.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_ROUTERS = Path("src/atlas/presentation/api/routers")
_REPOS = Path("src/atlas/infrastructure/db/repositories")


def _router_source(name: str) -> str:
    return (_ROUTERS / name).read_text()


class TestTenantSafetyReportIsolation:
    """Phase 6 invariant: tenant safety reports and the tenant
    ingestion write surface must never be reachable from a public
    or non-tenant router.

    Safety reports carry deidentified-but-sensitive ASAP narratives;
    leaking them onto the public surface would be a serious privacy
    breach.  The only router permitted to touch the tenant
    ingestion use cases is ``tenancy.py``.
    """

    _FORBIDDEN_SYMBOLS = (
        "tenant_safety_reports",
        "SubmitTenantSafetyReport",
        "ListTenantEvidenceForEvent",
        "tenant_ingestion",
    )

    def test_public_router_has_no_tenant_ingestion_access(self) -> None:
        source = _router_source("public.py")
        for symbol in self._FORBIDDEN_SYMBOLS:
            assert symbol not in source, (
                f"public.py references {symbol!r} — tenant safety "
                f"reports must never be reachable from the public "
                f"surface."
            )

    def test_only_tenancy_router_imports_ingestion_use_cases(self) -> None:
        offenders: list[str] = []
        for router in _ROUTERS.glob("*.py"):
            if router.name == "tenancy.py":
                continue
            text = router.read_text()
            if "use_cases.tenant_ingestion" in text:
                offenders.append(router.name)
        assert not offenders, (
            f"Routers other than tenancy.py import the tenant "
            f"ingestion use cases: {offenders}. The tenant safety "
            f"report surface must stay confined to the tenancy "
            f"router."
        )


class TestCausalityVisibilityInheritance:
    """Phase 4 invariant: every public causality read goes through
    the ``_require_published_event_for_slug`` gate so visibility
    inherits from the parent PublicEventPage.

    A public read that skipped the gate would leak HFACS/SHELO data
    for DRAFT or RETRACTED events.
    """

    def test_public_causality_reads_use_visibility_gate(self) -> None:
        source = Path("src/atlas/application/use_cases/causality.py").read_text()
        # Both public read use cases must call the gate helper.
        # Count the gate invocations vs the public-read classes.
        gate_calls = source.count("_require_published_event_for_slug(")
        # The helper is defined once and called by GetEventHfacs and
        # GetEventShelo — at least two call sites plus the def.
        assert gate_calls >= 3, (
            "Expected the visibility gate to be defined and called "
            "by both public causality reads (GetEventHfacs and "
            f"GetEventShelo); found {gate_calls} occurrences."
        )


class TestMeteringConflictParity:
    """Phase 4 follow-up: the SQL causality repos must pre-check
    natural keys in ``add`` so a duplicate surfaces as a typed
    conflict (-> 409), matching the in-memory fake.  Relying on a
    raw DB IntegrityError would produce a 500 in production while
    the fake-backed tests pass — a silent fake/SQL divergence.
    """

    def test_sql_hfacs_add_pre_checks_natural_key(self) -> None:
        source = (_REPOS / "causality.py").read_text()
        # The HFACS add method body must reference find_natural and
        # raise the typed conflict error.
        idx = source.find("async def add(self, attribution: EventHfacsAttribution)")
        assert idx != -1, "HFACS attribution add method not found"
        body = source[idx : idx + 800]
        assert "find_natural(" in body, (
            "SQL HFACS add must pre-check find_natural for fake/SQL parity."
        )
        assert "HfacsAttributionConflictError" in body, (
            "SQL HFACS add must raise the typed conflict error, not rely on a raw IntegrityError."
        )

    def test_sql_shelo_interaction_add_pre_checks_natural_key(
        self,
    ) -> None:
        source = (_REPOS / "causality.py").read_text()
        idx = source.find("async def add(self, interaction: SheloFactorInteraction)")
        assert idx != -1, "SHELO interaction add method not found"
        body = source[idx : idx + 900]
        assert "find_natural(" in body
        assert "SheloFactorInteractionConflictError" in body


class TestUsageEventBulkParity:
    """Phase 8 follow-up: the usage event repos must offer a bulk
    ``add_many`` so metering a large batch (e.g. 1000 claims) costs
    one database round trip rather than N flushes.  Both the SQL repo
    and the fake must implement it, and the metering service must use
    it.
    """

    def test_sql_usage_event_repo_has_add_many(self) -> None:
        source = (_REPOS / "metering.py").read_text()
        assert "async def add_many(self, events:" in source, (
            "SqlUsageEventRepository must implement add_many for bulk metering."
        )
        # The bulk path must use add_all (single multi-row insert),
        # not a per-row loop of add()+flush().
        idx = source.find("async def add_many(self, events:")
        body = source[idx : idx + 700]
        assert "add_all(" in body, "add_many must use session.add_all for a single bulk insert."

    def test_metering_service_uses_add_many(self) -> None:
        source = Path("src/atlas/application/services/metering.py").read_text()
        assert "add_many(" in source, (
            "MeteringService.record must use add_many so a "
            "large-quantity recording is one round trip, not N."
        )


@pytest.mark.parametrize(
    "migration,table",
    [
        ("042_causality.py", "hfacs_categories"),
        ("042_causality.py", "event_hfacs_attributions"),
        ("043_nl_search.py", "nl_query_log"),
        ("044_metering.py", "usage_events"),
        ("044_metering.py", "usage_daily_rollups"),
    ],
)
def test_phase_4_to_8_tables_have_downgrade(migration, table):
    """Every Phase 4-8 migration must drop the tables it creates in
    its downgrade — a half-written downgrade leaves the DB in a
    state that can't be cleanly rolled back."""
    source = Path("alembic/versions") / migration
    text = source.read_text()
    assert "create_table(" in text and table in text, f"{table} should be created in {migration}"
    assert f'drop_table("{table}")' in text, (
        f"{migration} creates {table} but its downgrade does not drop it."
    )


class TestClaimHistoryFlushOrdering:
    """Regression guard for the FK-ordering bug found during live-DB
    validation.

    The ORM layer declares no ``relationship()`` between ``ClaimModel``
    and ``ClaimHistoryModel`` (and none anywhere else), so SQLAlchemy
    cannot infer that a claim must be inserted before its history row.
    Without an explicit ``flush()`` between the two ``add`` calls,
    SQLAlchemy emits the ``claim_history`` INSERT first and Postgres
    rejects it (``claim_history_claim_id_fkey``).  This was invisible
    against fakes (no FK enforcement); it broke every real ingestion.

    These tests assert the ``flush()`` calls remain in the three
    write paths that create a claim and then its history row.  They
    are deliberately source-level: the behavioural proof lives in
    ``tests/integration/test_phase4_8_live.py`` and the other live
    integration tests, but those only run with ``--run-integration``,
    so this keeps a guard in the always-on suite.
    """

    def _src(self, rel: str) -> str:
        from pathlib import Path

        return Path("src/atlas/application") / rel

    def test_claim_writer_flushes_before_history(self) -> None:
        from pathlib import Path

        text = Path("src/atlas/application/ingestion/_claim_writer.py").read_text()
        # The new-claim path must flush between claims.add and
        # claim_history.add.
        idx = text.find("await self._uow.claims.add(claim)")
        assert idx != -1
        window = text[idx : idx + 1000]
        flush_pos = window.find("await self._uow.flush()")
        history_pos = window.find("claim_history.add")
        assert flush_pos != -1, "claim writer must flush after adding the claim"
        assert flush_pos < history_pos, "flush must come before the claim_history.add"

    def test_merge_flushes_before_history(self) -> None:
        from pathlib import Path

        text = Path("src/atlas/application/use_cases/merge_duplicate_events.py").read_text()
        idx = text.find("await self._uow.claims.add(new_claim)")
        assert idx != -1
        window = text[idx : idx + 1000]
        assert "await self._uow.flush()" in window
        assert window.find("await self._uow.flush()") < window.find("claim_history.add")

    def test_resolve_conflict_flushes_before_history(self) -> None:
        from pathlib import Path

        text = Path("src/atlas/application/use_cases/resolve_conflict.py").read_text()
        idx = text.find("await self._uow.claims.add(winning_claim)")
        assert idx != -1
        window = text[idx : idx + 1000]
        assert "await self._uow.flush()" in window
        assert window.find("await self._uow.flush()") < window.find("claim_history.add")

    def test_uow_protocol_has_flush(self) -> None:
        from pathlib import Path

        text = Path("src/atlas/application/unit_of_work.py").read_text()
        assert "async def flush(self)" in text, "UnitOfWork protocol must declare flush()"


class TestPublicUowWiring:
    """Assert that public-read routers use get_public_uow, not get_uow.

    These are static source checks — they verify the import and Depends()
    call are correct without running the app.  They catch the class of
    regression where someone adds a new endpoint to a public router and
    copies a get_uow() Depends() from another file.

    Kept as source-text checks (rather than AST) to stay consistent with
    the existing invariant test style in this file.
    """

    def test_public_router_imports_get_public_uow(self) -> None:
        from pathlib import Path

        text = Path("src/atlas/presentation/api/routers/public.py").read_text()
        assert "from atlas.presentation.api.dependencies import get_public_uow" in text, (
            "public.py must import get_public_uow from dependencies"
        )

    def test_public_router_has_no_get_uow_depends(self) -> None:
        from pathlib import Path

        text = Path("src/atlas/presentation/api/routers/public.py").read_text()
        # get_uow must not appear as a Depends() argument; the import line
        # itself is not present either after the refactor.
        assert "Depends(get_uow" not in text, (
            "public.py must use get_public_uow, not get_uow, for all endpoints"
        )

    def test_search_router_imports_get_public_uow(self) -> None:
        from pathlib import Path

        text = Path("src/atlas/presentation/api/routers/search.py").read_text()
        assert "from atlas.presentation.api.dependencies import get_public_uow" in text, (
            "search.py must import get_public_uow from dependencies"
        )

    def test_search_router_has_no_get_uow_depends(self) -> None:
        from pathlib import Path

        text = Path("src/atlas/presentation/api/routers/search.py").read_text()
        assert "Depends(get_uow" not in text, "search.py must use get_public_uow, not get_uow"

    def test_maps_router_imports_get_public_uow(self) -> None:
        from pathlib import Path

        text = Path("src/atlas/presentation/api/routers/maps.py").read_text()
        assert "from atlas.presentation.api.dependencies import get_public_uow" in text, (
            "maps.py must import get_public_uow from dependencies"
        )

    def test_maps_router_has_no_get_uow_depends(self) -> None:
        from pathlib import Path

        text = Path("src/atlas/presentation/api/routers/maps.py").read_text()
        assert "Depends(get_uow" not in text, "maps.py must use get_public_uow, not get_uow"

    def test_get_public_uow_is_exported_from_dependencies(self) -> None:
        from pathlib import Path

        text = Path("src/atlas/presentation/api/dependencies.py").read_text()
        assert "async def get_public_uow(" in text, (
            "dependencies.py must define get_public_uow as a FastAPI dependency"
        )

    def test_get_public_uow_uses_public_session_factory(self) -> None:
        from pathlib import Path

        text = Path("src/atlas/presentation/api/dependencies.py").read_text()
        idx = text.find("async def get_public_uow(")
        assert idx != -1
        # The docstring is intentionally detailed; use a generous window so
        # the assertion reaches the implementation body past the docstring.
        body = text[idx : idx + 2000]
        assert "async_public_session_factory" in body, (
            "get_public_uow must use async_public_session_factory, not async_session_factory"
        )

    def test_nl_search_router_documents_why_it_stays_on_get_uow(self) -> None:
        """Regression guard: nl_search writes query logs — it must not silently
        switch to get_public_uow without a matching write-path refactor."""
        from pathlib import Path

        text = Path("src/atlas/presentation/api/routers/nl_search.py").read_text()
        assert "nl_query_log" in text or "get_uow" in text, (
            "nl_search.py is expected to use get_uow due to query-log writes; "
            "if that has changed, review this test and the router docstring"
        )
