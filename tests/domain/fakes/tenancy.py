"""Fake tenant-scoped repositories (tenant, membership, claims, reports, crossref, etc.)."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from atlas.domain.interfaces.repositories import (
    TenantClaimRepository,
    TenantCrossrefResultRepository,
    TenantEventAssociationRepository,
    TenantEventOverlayPage,
    TenantEventOverlayRepository,
    TenantIngestionRunRepository,
    TenantMembershipRepository,
    TenantRepository,
    TenantSafetyReportRepository,
    TenantSourceRepository,
)
from atlas.domain.tenancy.entities import (
    CrossrefResultStatus,
    Tenant,
    TenantClaim,
    TenantClaimKind,
    TenantCrossrefResult,
    TenantEventAssociation,
    TenantEventOverlay,
    TenantIngestionRun,
    TenantIngestionRunStatus,
    TenantMembership,
    TenantSafetyReport,
    TenantSource,
)
from atlas.domain.tenancy.exceptions import TenantSourceAlreadyExistsError
from tests.domain.fakes._store import (
    _TenancyStore,
)


class FakeTenantRepository(TenantRepository):
    def __init__(self, s: _TenancyStore) -> None:
        self._s = s

    async def get(self, tenant_id: UUID) -> Tenant | None:
        return self._s.tenants.get(tenant_id)

    async def get_by_slug(self, slug: str) -> Tenant | None:
        for t in self._s.tenants.values():
            if t.slug == slug:
                return t
        return None

    async def add(self, tenant: Tenant) -> None:
        self._s.tenants[tenant.id] = tenant


class FakeTenantMembershipRepository(TenantMembershipRepository):
    def __init__(self, s: _TenancyStore) -> None:
        self._s = s

    async def add(self, membership: TenantMembership) -> None:
        # Mirror the DB-level (tenant_id, user_id) uniqueness.
        for m in self._s.memberships:
            if m.tenant_id == membership.tenant_id and m.user_id == membership.user_id:
                raise ValueError("Membership already exists")
        self._s.memberships.append(membership)

    async def get_for_user_in_tenant(
        self, *, tenant_id: UUID, user_id: UUID
    ) -> TenantMembership | None:
        for m in self._s.memberships:
            if m.tenant_id == tenant_id and m.user_id == user_id:
                return m
        return None


class FakeTenantSourceRepository(TenantSourceRepository):
    def __init__(self, s: _TenancyStore) -> None:
        self._s = s

    async def add(self, *, tenant_id: UUID, source: TenantSource) -> None:
        if source.tenant_id != tenant_id:
            raise ValueError(f"tenant_id mismatch: param={tenant_id}, source={source.tenant_id}")
        for existing in self._s.sources.values():
            if existing.tenant_id == tenant_id and existing.name == source.name:
                raise TenantSourceAlreadyExistsError(tenant_id=tenant_id, name=source.name)
        self._s.sources[source.id] = source

    async def list_for_tenant(self, *, tenant_id: UUID) -> list[TenantSource]:
        return sorted(
            (s for s in self._s.sources.values() if s.tenant_id == tenant_id),
            key=lambda s: s.name,
        )

    async def get(self, *, tenant_id: UUID, source_id: UUID) -> TenantSource | None:
        source = self._s.sources.get(source_id)
        # Cross-tenant probe must not leak the row; return None.
        if source is None or source.tenant_id != tenant_id:
            return None
        return source


class FakeTenantClaimRepository(TenantClaimRepository):
    def __init__(self, s: _TenancyStore) -> None:
        self._s = s

    async def add(self, *, tenant_id: UUID, claim: TenantClaim) -> None:
        if claim.tenant_id != tenant_id:
            raise ValueError(f"tenant_id mismatch: param={tenant_id}, claim={claim.tenant_id}")
        self._s.claims[claim.id] = claim

    async def add_many(self, *, tenant_id: UUID, claims: list[TenantClaim]) -> None:
        for claim in claims:
            if claim.tenant_id != tenant_id:
                raise ValueError(
                    f"tenant_id mismatch in batch: param={tenant_id}, claim={claim.tenant_id}"
                )
        for claim in claims:
            self._s.claims[claim.id] = claim

    async def list_for_event(self, *, tenant_id: UUID, event_id: UUID) -> list[TenantClaim]:
        return sorted(
            (
                c
                for c in self._s.claims.values()
                if c.tenant_id == tenant_id and c.event_id == event_id
            ),
            key=lambda c: c.created_at,
        )

    async def list_for_event_by_kind(
        self,
        *,
        tenant_id: UUID,
        event_id: UUID,
        claim_kind: TenantClaimKind,
    ) -> list[TenantClaim]:
        return sorted(
            (
                c
                for c in self._s.claims.values()
                if c.tenant_id == tenant_id
                and c.event_id == event_id
                and c.claim_kind == claim_kind
            ),
            key=lambda c: c.created_at,
        )


class FakeTenantIngestionRunRepository(TenantIngestionRunRepository):
    def __init__(self, s: _TenancyStore) -> None:
        self._s = s

    async def add(self, *, tenant_id: UUID, run: TenantIngestionRun) -> None:
        if run.tenant_id != tenant_id:
            raise ValueError(f"tenant_id mismatch: param={tenant_id}, run={run.tenant_id}")
        self._s.ingestion_runs[run.id] = run

    async def get(self, *, tenant_id: UUID, run_id: UUID) -> TenantIngestionRun | None:
        run = self._s.ingestion_runs.get(run_id)
        if run is None or run.tenant_id != tenant_id:
            return None
        return run.model_copy(deep=True)

    async def update_status(
        self,
        *,
        tenant_id: UUID,
        run_id: UUID,
        status: TenantIngestionRunStatus,
        finished_at: datetime | None,
    ) -> None:
        run = self._s.ingestion_runs.get(run_id)
        if run is None or run.tenant_id != tenant_id:
            # Mirror the SQL repo's silent no-op for cross-tenant
            # updates: the access control happens above; this is
            # belt-and-braces.
            return
        self._s.ingestion_runs[run_id] = run.model_copy(
            update={"status": status.value, "finished_at": finished_at}
        )


class FakeTenantEventOverlayRepository(TenantEventOverlayRepository):
    def __init__(self, s: _TenancyStore) -> None:
        self._s = s

    async def get(self, *, tenant_id: UUID, event_id: UUID) -> TenantEventOverlay | None:
        for o in self._s.overlays.values():
            if o.tenant_id == tenant_id and o.event_id == event_id:
                return o.model_copy(deep=True)
        return None

    async def upsert(self, *, tenant_id: UUID, overlay: TenantEventOverlay) -> TenantEventOverlay:
        if overlay.tenant_id != tenant_id:
            raise ValueError(f"tenant_id mismatch: param={tenant_id}, overlay={overlay.tenant_id}")
        # Find existing row by (tenant_id, event_id) and replace.
        for existing_id, existing in self._s.overlays.items():
            if existing.tenant_id == tenant_id and existing.event_id == overlay.event_id:
                merged = existing.model_copy(
                    update={
                        "notes_markdown": overlay.notes_markdown,
                        "overlay_fields": overlay.overlay_fields,
                        "updated_at": overlay.updated_at,
                    }
                )
                self._s.overlays[existing_id] = merged
                return merged.model_copy(deep=True)
        # No existing row → insert.
        self._s.overlays[overlay.id] = overlay.model_copy(deep=True)
        return overlay.model_copy(deep=True)

    async def list_for_tenant(
        self,
        *,
        tenant_id: UUID,
        limit: int,
        after_id: UUID | None = None,
    ) -> TenantEventOverlayPage:
        rows = [o for o in self._s.overlays.values() if o.tenant_id == tenant_id]
        rows.sort(key=lambda o: (o.updated_at, o.id), reverse=True)

        if after_id is not None:
            cursor = self._s.overlays.get(after_id)
            # Cross-tenant cursor: silently drop, mirroring SQL repo.
            if cursor is not None and cursor.tenant_id == tenant_id:
                cursor_key = (cursor.updated_at, cursor.id)
                rows = [r for r in rows if (r.updated_at, r.id) < cursor_key]

        page_items = rows[: limit + 1]
        next_cursor: UUID | None = None
        if len(page_items) > limit:
            page_items = page_items[:limit]
            next_cursor = page_items[-1].id
        return TenantEventOverlayPage(
            items=[o.model_copy(deep=True) for o in page_items],
            next_cursor=next_cursor,
        )


# ── Maps fake (Phase 3) ─────────────────────────────────────────────────────


class FakeTenantSafetyReportRepository(TenantSafetyReportRepository):
    def __init__(self, s: _TenancyStore) -> None:
        self._s = s

    async def add(self, *, tenant_id: UUID, report: TenantSafetyReport) -> None:
        if report.tenant_id != tenant_id:
            raise ValueError(f"tenant_id mismatch: param={tenant_id}, report={report.tenant_id}")
        self._s.safety_reports[report.id] = report.model_copy(deep=True)

    async def get(self, *, tenant_id: UUID, report_id: UUID) -> TenantSafetyReport | None:
        report = self._s.safety_reports.get(report_id)
        if report is None or report.tenant_id != tenant_id:
            return None
        return report.model_copy(deep=True)

    async def list_for_tenant(
        self, *, tenant_id: UUID, limit: int = 50
    ) -> list[TenantSafetyReport]:
        rows = sorted(
            (r for r in self._s.safety_reports.values() if r.tenant_id == tenant_id),
            key=lambda r: r.created_at,
            reverse=True,
        )
        return [r.model_copy(deep=True) for r in rows[:limit]]


class FakeTenantCrossrefResultRepository(TenantCrossrefResultRepository):
    """In-memory crossref result store for unit tests."""

    def __init__(self, s: _TenancyStore) -> None:
        self._s = s

    async def add(self, *, tenant_id: UUID, result: TenantCrossrefResult) -> None:
        if result.tenant_id != tenant_id:
            raise ValueError(f"tenant_id mismatch: param={tenant_id}, result={result.tenant_id}")
        self._s.crossref_results[result.id] = result.model_copy(deep=True)

    async def get(self, *, tenant_id: UUID, result_id: UUID) -> TenantCrossrefResult | None:
        r = self._s.crossref_results.get(result_id)
        if r is None or r.tenant_id != tenant_id:
            return None
        return r.model_copy(deep=True)

    async def mark_complete(
        self,
        *,
        tenant_id: UUID,
        result_id: UUID,
        matches_json: list[dict],
        matcher_config_json: dict,
        match_count: int,
        completed_at: datetime,
    ) -> None:
        r = self._s.crossref_results.get(result_id)
        if r is None or r.tenant_id != tenant_id:
            return
        self._s.crossref_results[result_id] = r.model_copy(
            update={
                "status": CrossrefResultStatus.COMPLETE,
                "matches_json": matches_json,
                "matcher_config_json": matcher_config_json,
                "match_count": match_count,
                "completed_at": completed_at,
            },
            deep=True,
        )

    async def mark_failed(
        self,
        *,
        tenant_id: UUID,
        result_id: UUID,
        error_detail: str,
        completed_at: datetime,
    ) -> None:
        r = self._s.crossref_results.get(result_id)
        if r is None or r.tenant_id != tenant_id:
            return
        self._s.crossref_results[result_id] = r.model_copy(
            update={
                "status": CrossrefResultStatus.FAILED,
                "error_detail": error_detail,
                "completed_at": completed_at,
            },
            deep=True,
        )

    async def list_for_report(
        self, *, tenant_id: UUID, safety_report_id: UUID, limit: int = 10
    ) -> list[TenantCrossrefResult]:
        rows = sorted(
            (
                r
                for r in self._s.crossref_results.values()
                if r.tenant_id == tenant_id and r.safety_report_id == safety_report_id
            ),
            key=lambda r: r.requested_at,
            reverse=True,
        )
        return [r.model_copy(deep=True) for r in rows[:limit]]


class FakeTenantEventAssociationRepository(TenantEventAssociationRepository):
    def __init__(self, s: _TenancyStore) -> None:
        self._s = s

    async def add(
        self,
        *,
        tenant_id: UUID,
        association: TenantEventAssociation,
    ) -> None:
        if association.tenant_id != tenant_id:
            raise ValueError(
                f"tenant_id mismatch: param={tenant_id}, association={association.tenant_id}"
            )
        self._s.event_associations[association.id] = association.model_copy(deep=True)

    async def list_for_event(
        self, *, tenant_id: UUID, event_id: UUID
    ) -> list[TenantEventAssociation]:
        return sorted(
            (
                a.model_copy(deep=True)
                for a in self._s.event_associations.values()
                if a.tenant_id == tenant_id and a.event_id == event_id
            ),
            key=lambda a: a.created_at,
        )


# ── Phase 4 fakes ───────────────────────────────────────────────────────────
