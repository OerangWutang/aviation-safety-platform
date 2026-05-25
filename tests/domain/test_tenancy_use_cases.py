"""Tenancy use-case tests (Phase 5).

The headline contract being tested: **tenant isolation at every
layer**.  Each test pins one of the three isolation layers:

1. The auth gate (``require_tenant_membership``) — exercised by the
   API tests in ``tests/api/test_tenancy_api.py``.
2. The use case — verifies cross-tenant access denial here.
3. The repository — confirms that every method requires
   ``tenant_id`` and that cross-tenant probes return None.

Public read paths are exercised separately to confirm that
tenant rows never appear in public responses.
"""

from __future__ import annotations

import inspect
from uuid import uuid4

import pytest

from atlas.application.use_cases.tenancy import (
    GetTenantEventOverlay,
    ListTenantEvents,
    RegisterTenantSource,
    RegisterTenantSourceInput,
    UpsertTenantEventOverlay,
    UpsertTenantEventOverlayInput,
)
from atlas.domain.entities import AccidentEvent, ProjectedAccidentRecord
from atlas.domain.tenancy.entities import (
    Tenant,
    TenantMembership,
    TenantRole,
)
from atlas.domain.tenancy.exceptions import (
    CrossTenantAccessError,
    TenantSourceAlreadyExistsError,
)
from tests.domain._fake_uow import InMemoryUnitOfWork

# ── Helpers ──────────────────────────────────────────────────────────────────


def _seed_tenant(uow: InMemoryUnitOfWork, *, slug: str = "acme") -> Tenant:
    t = Tenant(slug=slug, display_name=slug.upper())
    uow.store.tenancy.tenants[t.id] = t
    return t


def _seed_membership(
    uow: InMemoryUnitOfWork,
    *,
    tenant: Tenant,
    user_id=None,
    role: TenantRole = TenantRole.OWNER,
) -> TenantMembership:
    m = TenantMembership(
        tenant_id=tenant.id,
        user_id=user_id or uuid4(),
        tenant_role=role,
    )
    uow.store.tenancy.memberships.append(m)
    return m


def _seed_event_with_projection(uow: InMemoryUnitOfWork, *, fields=None):
    e = AccidentEvent()
    uow.store.events[e.id] = e
    uow.store.projections[e.id] = ProjectedAccidentRecord(
        event_id=e.id,
        fields=fields or {"operator": "Public Airlines"},
        completeness_score=0.9,
    )
    return e


# ── Repository isolation contract ───────────────────────────────────────────


class TestRepositoryIsolationContract:
    """Pins the layer-3 invariant: tenant_id is a required parameter
    on every tenant repo method.

    If a future contributor adds a method without ``tenant_id`` as a
    required keyword, this test fails.  The cost of one test is much
    less than the cost of a tenant data leak.
    """

    @pytest.mark.parametrize(
        "repo_attr",
        [
            "tenant_sources",
            "tenant_claims",
            "tenant_event_overlays",
            "tenant_ingestion_runs",
        ],
    )
    def test_every_data_method_requires_tenant_id(self, repo_attr: str) -> None:
        uow = InMemoryUnitOfWork()
        repo = getattr(uow, repo_attr)
        public_methods = [
            name for name in dir(repo) if not name.startswith("_") and callable(getattr(repo, name))
        ]
        # Skip the methods inherited from ABC/object.
        repo_methods = [
            name
            for name in public_methods
            if name
            not in {
                "register",  # abc.ABC.register, present on subclasses
            }
        ]
        for name in repo_methods:
            sig = inspect.signature(getattr(repo, name))
            params = sig.parameters
            # ``tenant_id`` must be either present as a parameter, or
            # this is a method we explicitly accept without it.
            assert "tenant_id" in params, (
                f"{repo_attr}.{name} signature {sig} is missing "
                f"tenant_id — that's a tenant-isolation hazard."
            )


# ── Register source ──────────────────────────────────────────────────────────


class TestRegisterTenantSource:
    async def test_owner_can_register_source(self) -> None:
        uow = InMemoryUnitOfWork()
        tenant = _seed_tenant(uow)
        source = await RegisterTenantSource(uow).execute(
            RegisterTenantSourceInput(
                tenant_id=tenant.id,
                caller_tenant_id=tenant.id,
                caller_tenant_role=TenantRole.OWNER.value,
                name="Internal FOQA",
            )
        )
        assert source.tenant_id == tenant.id
        assert source.name == "Internal FOQA"

    async def test_cross_tenant_access_rejected(self) -> None:
        uow = InMemoryUnitOfWork()
        tenant_a = _seed_tenant(uow, slug="a")
        tenant_b = _seed_tenant(uow, slug="b")
        with pytest.raises(CrossTenantAccessError):
            await RegisterTenantSource(uow).execute(
                RegisterTenantSourceInput(
                    tenant_id=tenant_b.id,
                    caller_tenant_id=tenant_a.id,
                    caller_tenant_role=TenantRole.OWNER.value,
                    name="Leak",
                )
            )

    async def test_read_only_cannot_register(self) -> None:
        uow = InMemoryUnitOfWork()
        tenant = _seed_tenant(uow)
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as excinfo:
            await RegisterTenantSource(uow).execute(
                RegisterTenantSourceInput(
                    tenant_id=tenant.id,
                    caller_tenant_id=tenant.id,
                    caller_tenant_role=TenantRole.READ_ONLY.value,
                    name="should fail",
                )
            )
        assert excinfo.value.status_code == 403

    async def test_duplicate_source_name_rejected(self) -> None:
        uow = InMemoryUnitOfWork()
        tenant = _seed_tenant(uow)
        await RegisterTenantSource(uow).execute(
            RegisterTenantSourceInput(
                tenant_id=tenant.id,
                caller_tenant_id=tenant.id,
                caller_tenant_role=TenantRole.OWNER.value,
                name="Dup",
            )
        )
        with pytest.raises(TenantSourceAlreadyExistsError):
            await RegisterTenantSource(uow).execute(
                RegisterTenantSourceInput(
                    tenant_id=tenant.id,
                    caller_tenant_id=tenant.id,
                    caller_tenant_role=TenantRole.OWNER.value,
                    name="Dup",
                )
            )

    async def test_same_name_different_tenants_is_allowed(self) -> None:
        """Cross-tenant: "Operations" in tenant A and "Operations" in
        tenant B should not collide.  Composite uniqueness is the
        contract."""
        uow = InMemoryUnitOfWork()
        a = _seed_tenant(uow, slug="a")
        b = _seed_tenant(uow, slug="b")
        await RegisterTenantSource(uow).execute(
            RegisterTenantSourceInput(
                tenant_id=a.id,
                caller_tenant_id=a.id,
                caller_tenant_role=TenantRole.OWNER.value,
                name="Operations",
            )
        )
        await RegisterTenantSource(uow).execute(
            RegisterTenantSourceInput(
                tenant_id=b.id,
                caller_tenant_id=b.id,
                caller_tenant_role=TenantRole.OWNER.value,
                name="Operations",
            )
        )
        # Each tenant sees only its own source.
        a_sources = await uow.tenant_sources.list_for_tenant(tenant_id=a.id)
        b_sources = await uow.tenant_sources.list_for_tenant(tenant_id=b.id)
        assert {s.name for s in a_sources} == {"Operations"}
        assert {s.name for s in b_sources} == {"Operations"}
        assert {s.id for s in a_sources}.isdisjoint({s.id for s in b_sources})


# ── Cross-tenant probe via repository ────────────────────────────────────────


class TestRepositoryCrossTenantProbe:
    async def test_get_with_other_tenant_id_returns_none(self) -> None:
        """Even with a known source id, a query under the wrong
        tenant_id must return None — the tenant_id is an
        access-control check, not a hint."""
        uow = InMemoryUnitOfWork()
        a = _seed_tenant(uow, slug="a")
        b = _seed_tenant(uow, slug="b")
        # Source belongs to tenant A.
        source = await RegisterTenantSource(uow).execute(
            RegisterTenantSourceInput(
                tenant_id=a.id,
                caller_tenant_id=a.id,
                caller_tenant_role=TenantRole.OWNER.value,
                name="A's source",
            )
        )
        # Tenant B tries to look it up by id — must miss.
        found_from_b = await uow.tenant_sources.get(tenant_id=b.id, source_id=source.id)
        assert found_from_b is None
        # And tenant A's own lookup still works.
        found_from_a = await uow.tenant_sources.get(tenant_id=a.id, source_id=source.id)
        assert found_from_a is not None

    async def test_list_for_tenant_filters_by_tenant(self) -> None:
        uow = InMemoryUnitOfWork()
        a = _seed_tenant(uow, slug="a")
        b = _seed_tenant(uow, slug="b")
        await RegisterTenantSource(uow).execute(
            RegisterTenantSourceInput(
                tenant_id=a.id,
                caller_tenant_id=a.id,
                caller_tenant_role=TenantRole.OWNER.value,
                name="Only A's",
            )
        )
        b_sources = await uow.tenant_sources.list_for_tenant(tenant_id=b.id)
        assert b_sources == []


# ── Event overlay ────────────────────────────────────────────────────────────


class TestUpsertAndGetOverlay:
    async def test_upsert_creates_then_replaces(self) -> None:
        uow = InMemoryUnitOfWork()
        tenant = _seed_tenant(uow)
        event = _seed_event_with_projection(uow)

        first = await UpsertTenantEventOverlay(uow).execute(
            UpsertTenantEventOverlayInput(
                tenant_id=tenant.id,
                caller_tenant_id=tenant.id,
                caller_tenant_role=TenantRole.OWNER.value,
                event_id=event.id,
                notes_markdown="First note",
            )
        )
        second = await UpsertTenantEventOverlay(uow).execute(
            UpsertTenantEventOverlayInput(
                tenant_id=tenant.id,
                caller_tenant_id=tenant.id,
                caller_tenant_role=TenantRole.OWNER.value,
                event_id=event.id,
                notes_markdown="Second note",
                overlay_fields={"internal_severity": "high"},
            )
        )
        # Same row updated in place.
        assert first.id == second.id
        assert second.notes_markdown == "Second note"
        assert second.overlay_fields == {"internal_severity": "high"}
        assert second.updated_at >= first.updated_at

    async def test_read_returns_overlay_plus_public_context(self) -> None:
        uow = InMemoryUnitOfWork()
        tenant = _seed_tenant(uow)
        event = _seed_event_with_projection(uow, fields={"operator": "Public Airlines"})
        await UpsertTenantEventOverlay(uow).execute(
            UpsertTenantEventOverlayInput(
                tenant_id=tenant.id,
                caller_tenant_id=tenant.id,
                caller_tenant_role=TenantRole.OWNER.value,
                event_id=event.id,
                notes_markdown="Internal investigation note",
            )
        )

        view = await GetTenantEventOverlay(uow).execute(
            tenant_id=tenant.id,
            caller_tenant_id=tenant.id,
            event_id=event.id,
        )
        assert view.overlay is not None
        assert view.overlay.notes_markdown == "Internal investigation note"
        # Public projection context is included so the tenant UI can
        # render side-by-side without a second round-trip.
        assert view.public_fields == {"operator": "Public Airlines"}

    async def test_read_returns_none_overlay_when_unset(self) -> None:
        uow = InMemoryUnitOfWork()
        tenant = _seed_tenant(uow)
        event = _seed_event_with_projection(uow)
        view = await GetTenantEventOverlay(uow).execute(
            tenant_id=tenant.id,
            caller_tenant_id=tenant.id,
            event_id=event.id,
        )
        assert view.overlay is None
        # Public context still present.
        assert "operator" in view.public_fields

    async def test_overlay_isolated_across_tenants(self) -> None:
        """Both tenants annotate the same public event.  Each sees
        only its own overlay."""
        uow = InMemoryUnitOfWork()
        a = _seed_tenant(uow, slug="a")
        b = _seed_tenant(uow, slug="b")
        event = _seed_event_with_projection(uow)

        await UpsertTenantEventOverlay(uow).execute(
            UpsertTenantEventOverlayInput(
                tenant_id=a.id,
                caller_tenant_id=a.id,
                caller_tenant_role=TenantRole.OWNER.value,
                event_id=event.id,
                notes_markdown="A's notes",
            )
        )
        await UpsertTenantEventOverlay(uow).execute(
            UpsertTenantEventOverlayInput(
                tenant_id=b.id,
                caller_tenant_id=b.id,
                caller_tenant_role=TenantRole.OWNER.value,
                event_id=event.id,
                notes_markdown="B's notes",
            )
        )

        a_view = await GetTenantEventOverlay(uow).execute(
            tenant_id=a.id, caller_tenant_id=a.id, event_id=event.id
        )
        b_view = await GetTenantEventOverlay(uow).execute(
            tenant_id=b.id, caller_tenant_id=b.id, event_id=event.id
        )
        assert a_view.overlay is not None
        assert b_view.overlay is not None
        assert a_view.overlay.notes_markdown == "A's notes"
        assert b_view.overlay.notes_markdown == "B's notes"
        assert a_view.overlay.id != b_view.overlay.id

    async def test_cross_tenant_read_rejected(self) -> None:
        uow = InMemoryUnitOfWork()
        a = _seed_tenant(uow, slug="a")
        b = _seed_tenant(uow, slug="b")
        event = _seed_event_with_projection(uow)
        with pytest.raises(CrossTenantAccessError):
            await GetTenantEventOverlay(uow).execute(
                tenant_id=b.id,
                caller_tenant_id=a.id,  # caller from A targeting B
                event_id=event.id,
            )


# ── List ─────────────────────────────────────────────────────────────────────


class TestListTenantEvents:
    async def test_lists_only_events_with_overlays_for_this_tenant(
        self,
    ) -> None:
        uow = InMemoryUnitOfWork()
        a = _seed_tenant(uow, slug="a")
        b = _seed_tenant(uow, slug="b")
        event_x = _seed_event_with_projection(uow)
        event_y = _seed_event_with_projection(uow)
        # Both A and B annotate event_x; only A annotates event_y.
        await UpsertTenantEventOverlay(uow).execute(
            UpsertTenantEventOverlayInput(
                tenant_id=a.id,
                caller_tenant_id=a.id,
                caller_tenant_role=TenantRole.OWNER.value,
                event_id=event_x.id,
                notes_markdown="A on X",
            )
        )
        await UpsertTenantEventOverlay(uow).execute(
            UpsertTenantEventOverlayInput(
                tenant_id=a.id,
                caller_tenant_id=a.id,
                caller_tenant_role=TenantRole.OWNER.value,
                event_id=event_y.id,
                notes_markdown="A on Y",
            )
        )
        await UpsertTenantEventOverlay(uow).execute(
            UpsertTenantEventOverlayInput(
                tenant_id=b.id,
                caller_tenant_id=b.id,
                caller_tenant_role=TenantRole.OWNER.value,
                event_id=event_x.id,
                notes_markdown="B on X",
            )
        )
        a_list = await ListTenantEvents(uow).execute(tenant_id=a.id, caller_tenant_id=a.id)
        b_list = await ListTenantEvents(uow).execute(tenant_id=b.id, caller_tenant_id=b.id)
        assert {item.event_id for item in a_list.items} == {
            event_x.id,
            event_y.id,
        }
        assert {item.event_id for item in b_list.items} == {event_x.id}

    async def test_cross_tenant_list_rejected(self) -> None:
        uow = InMemoryUnitOfWork()
        a = _seed_tenant(uow, slug="a")
        b = _seed_tenant(uow, slug="b")
        with pytest.raises(CrossTenantAccessError):
            await ListTenantEvents(uow).execute(tenant_id=b.id, caller_tenant_id=a.id)


# ── Public surfaces never include tenant data ────────────────────────────────


class TestPublicSurfacesExcludeTenantData:
    """The strongest isolation pin: when tenant claims and overlays
    exist for an event, public surfaces never surface any of them.

    These tests exercise the *parallel tables* design directly.  If
    a future refactor moved tenant claims into the public ``claims``
    table with a ``tenant_id`` column, the tests below would only
    pass if every single read path applied the filter — which is
    exactly the contamination risk we ruled out by construction."""

    async def test_public_projection_unchanged_by_tenant_overlay(
        self,
    ) -> None:
        uow = InMemoryUnitOfWork()
        tenant = _seed_tenant(uow)
        event = _seed_event_with_projection(
            uow,
            fields={"operator": "Public Airlines", "location": "Anchorage"},
        )
        await UpsertTenantEventOverlay(uow).execute(
            UpsertTenantEventOverlayInput(
                tenant_id=tenant.id,
                caller_tenant_id=tenant.id,
                caller_tenant_role=TenantRole.OWNER.value,
                event_id=event.id,
                notes_markdown="Internal",
                overlay_fields={"operator": "INTERNAL VALUE"},
            )
        )
        # Public projection store is untouched.
        projection = await uow.projections.get(event.id)
        assert projection is not None
        assert projection.fields["operator"] == "Public Airlines"
        # Tenant overlay had its own value but it lives in a
        # different table; the public projection's read never sees it.
        assert "INTERNAL VALUE" not in str(projection.fields)

    async def test_public_claims_repo_excludes_tenant_claims_by_design(
        self,
    ) -> None:
        """``find_active_by_event`` only returns rows from the public
        ``claims`` store.  Tenant claims live in
        ``store.tenancy.claims`` — a different dict — so they cannot
        appear in this list."""
        uow = InMemoryUnitOfWork()
        tenant = _seed_tenant(uow)
        event = _seed_event_with_projection(uow)
        # Manually drop a tenant claim into the tenancy store.
        from atlas.domain.tenancy.entities import TenantClaim

        tenant_source = await RegisterTenantSource(uow).execute(
            RegisterTenantSourceInput(
                tenant_id=tenant.id,
                caller_tenant_id=tenant.id,
                caller_tenant_role=TenantRole.OWNER.value,
                name="tenant src",
            )
        )
        tenant_claim = TenantClaim(
            tenant_id=tenant.id,
            event_id=event.id,
            tenant_source_id=tenant_source.id,
            field_name="operator",
            field_value="TENANT SECRET",
        )
        await uow.tenant_claims.add(tenant_id=tenant.id, claim=tenant_claim)

        # Public claims read returns nothing because no public claim
        # exists.
        public_claims = await uow.claims.find_active_by_event(event.id)
        assert public_claims == []
        # The tenant claim exists in its own table.
        tenant_claims = await uow.tenant_claims.list_for_event(
            tenant_id=tenant.id, event_id=event.id
        )
        assert {c.field_value for c in tenant_claims} == {"TENANT SECRET"}
