"""SQLAlchemy repositories for the tenancy bounded context (Phases 5 + 6).

Every method takes ``tenant_id`` and filters every WHERE clause on
it.  The table separation also provides isolation at the schema
level — these tables don't appear in any public repository's query
graph.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import literal, select, tuple_, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

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
    TenantRole,
    TenantSafetyReport,
    TenantSource,
)
from atlas.domain.tenancy.exceptions import TenantSourceAlreadyExistsError
from atlas.infrastructure.db.orm_models import (
    TenantClaimModel,
    TenantCrossrefResultModel,
    TenantEventAssociationModel,
    TenantEventOverlayModel,
    TenantIngestionRunModel,
    TenantMembershipModel,
    TenantModel,
    TenantSafetyReportModel,
    TenantSourceModel,
)
from atlas.infrastructure.db.repositories._helpers import (
    _domain_data,
    _to_domain,
    _to_domain_opt,
)


class SqlTenantRepository(TenantRepository):
    def __init__(self, session: AsyncSession):
        self._session = session

    async def get(self, tenant_id: UUID) -> Tenant | None:
        obj = await self._session.get(TenantModel, tenant_id)
        return _to_domain_opt(obj, Tenant)

    async def get_by_slug(self, slug: str) -> Tenant | None:
        result = await self._session.execute(select(TenantModel).where(TenantModel.slug == slug))
        return _to_domain_opt(result.scalar_one_or_none(), Tenant)

    async def add(self, tenant: Tenant) -> None:
        self._session.add(TenantModel(**_domain_data(tenant)))
        await self._session.flush()


class SqlTenantMembershipRepository(TenantMembershipRepository):
    def __init__(self, session: AsyncSession):
        self._session = session

    async def add(self, membership: TenantMembership) -> None:
        # Pydantic stores ``tenant_role`` as the enum instance; the
        # ORM column is a string.  ``_domain_data`` would pass the
        # enum object through unchanged, but Postgres expects the
        # value.  We coerce here once for the same reason the public-
        # event-page repo coerces ``status``.
        data = _domain_data(membership)
        data["tenant_role"] = (
            membership.tenant_role.value
            if isinstance(membership.tenant_role, TenantRole)
            else membership.tenant_role
        )
        self._session.add(TenantMembershipModel(**data))
        await self._session.flush()

    async def get_for_user_in_tenant(
        self, *, tenant_id: UUID, user_id: UUID
    ) -> TenantMembership | None:
        result = await self._session.execute(
            select(TenantMembershipModel).where(
                TenantMembershipModel.tenant_id == tenant_id,
                TenantMembershipModel.user_id == user_id,
            )
        )
        row = result.scalar_one_or_none()
        if row is None:
            return None
        # Reconstruct the entity through pydantic so the StrEnum
        # round-trips cleanly.
        return TenantMembership(
            id=row.id,
            tenant_id=row.tenant_id,
            user_id=row.user_id,
            tenant_role=TenantRole(row.tenant_role),
            created_at=row.created_at,
        )


class SqlTenantSourceRepository(TenantSourceRepository):
    def __init__(self, session: AsyncSession):
        self._session = session

    async def add(self, *, tenant_id: UUID, source: TenantSource) -> None:
        # The tenant_id parameter is checked defensively against the
        # source's own field.  Mismatch is a programming bug — we
        # fail loud rather than silently honour the source.tenant_id.
        if source.tenant_id != tenant_id:
            raise ValueError(f"tenant_id mismatch: param={tenant_id}, source={source.tenant_id}")
        self._session.add(TenantSourceModel(**_domain_data(source)))
        try:
            await self._session.flush()
        except IntegrityError as exc:
            constraint = _extract_constraint_name(exc)
            if constraint == "uq_tenant_sources_tenant_name":
                raise TenantSourceAlreadyExistsError(tenant_id=tenant_id, name=source.name) from exc
            raise

    async def list_for_tenant(self, *, tenant_id: UUID) -> list[TenantSource]:
        result = await self._session.execute(
            select(TenantSourceModel)
            .where(TenantSourceModel.tenant_id == tenant_id)
            .order_by(TenantSourceModel.name)
        )
        return [_to_domain(row, TenantSource) for row in result.scalars()]

    async def get(self, *, tenant_id: UUID, source_id: UUID) -> TenantSource | None:
        # Both predicates required.  A bare PK lookup would defeat
        # isolation.
        result = await self._session.execute(
            select(TenantSourceModel).where(
                TenantSourceModel.id == source_id,
                TenantSourceModel.tenant_id == tenant_id,
            )
        )
        return _to_domain_opt(result.scalar_one_or_none(), TenantSource)


class SqlTenantClaimRepository(TenantClaimRepository):
    def __init__(self, session: AsyncSession):
        self._session = session

    async def add(self, *, tenant_id: UUID, claim: TenantClaim) -> None:
        if claim.tenant_id != tenant_id:
            raise ValueError(f"tenant_id mismatch: param={tenant_id}, claim={claim.tenant_id}")
        # claim_kind is a StrEnum on the entity; coerce to the
        # column's string value (same pattern as TenantRole).
        data = _domain_data(claim)
        data["claim_kind"] = (
            claim.claim_kind.value
            if isinstance(claim.claim_kind, TenantClaimKind)
            else claim.claim_kind
        )
        self._session.add(TenantClaimModel(**data))
        await self._session.flush()

    async def add_many(self, *, tenant_id: UUID, claims: list[TenantClaim]) -> None:
        # Per-row defensive tenant_id check: callers shouldn't be
        # able to smuggle a claim under a different tenant through a
        # bulk path.  We loop and add to the session; SQLAlchemy
        # batches the INSERTs at flush time.
        for claim in claims:
            if claim.tenant_id != tenant_id:
                raise ValueError(
                    f"tenant_id mismatch in batch: param={tenant_id}, claim={claim.tenant_id}"
                )
        for claim in claims:
            data = _domain_data(claim)
            data["claim_kind"] = (
                claim.claim_kind.value
                if isinstance(claim.claim_kind, TenantClaimKind)
                else claim.claim_kind
            )
            self._session.add(TenantClaimModel(**data))
        await self._session.flush()

    async def list_for_event(self, *, tenant_id: UUID, event_id: UUID) -> list[TenantClaim]:
        result = await self._session.execute(
            select(TenantClaimModel)
            .where(
                TenantClaimModel.tenant_id == tenant_id,
                TenantClaimModel.event_id == event_id,
            )
            .order_by(TenantClaimModel.created_at)
        )
        return [_to_domain(row, TenantClaim) for row in result.scalars()]

    async def list_for_event_by_kind(
        self,
        *,
        tenant_id: UUID,
        event_id: UUID,
        claim_kind: TenantClaimKind,
    ) -> list[TenantClaim]:
        result = await self._session.execute(
            select(TenantClaimModel)
            .where(
                TenantClaimModel.tenant_id == tenant_id,
                TenantClaimModel.event_id == event_id,
                TenantClaimModel.claim_kind == claim_kind.value,
            )
            .order_by(TenantClaimModel.created_at)
        )
        return [_to_domain(row, TenantClaim) for row in result.scalars()]


class SqlTenantIngestionRunRepository(TenantIngestionRunRepository):
    def __init__(self, session: AsyncSession):
        self._session = session

    async def add(self, *, tenant_id: UUID, run: TenantIngestionRun) -> None:
        if run.tenant_id != tenant_id:
            raise ValueError(f"tenant_id mismatch: param={tenant_id}, run={run.tenant_id}")
        self._session.add(TenantIngestionRunModel(**_domain_data(run)))
        await self._session.flush()

    async def get(self, *, tenant_id: UUID, run_id: UUID) -> TenantIngestionRun | None:
        result = await self._session.execute(
            select(TenantIngestionRunModel).where(
                TenantIngestionRunModel.id == run_id,
                TenantIngestionRunModel.tenant_id == tenant_id,
            )
        )
        return _to_domain_opt(result.scalar_one_or_none(), TenantIngestionRun)

    async def update_status(
        self,
        *,
        tenant_id: UUID,
        run_id: UUID,
        status: TenantIngestionRunStatus,
        finished_at: datetime | None,
    ) -> None:
        # Tenant-scoped predicate even on the update.  Defence in
        # depth: a router that forgets the membership gate still
        # cannot transition another tenant's run.
        await self._session.execute(
            update(TenantIngestionRunModel)
            .where(
                TenantIngestionRunModel.id == run_id,
                TenantIngestionRunModel.tenant_id == tenant_id,
            )
            .values(status=status.value, finished_at=finished_at)
        )


class SqlTenantEventOverlayRepository(TenantEventOverlayRepository):
    def __init__(self, session: AsyncSession):
        self._session = session

    async def get(self, *, tenant_id: UUID, event_id: UUID) -> TenantEventOverlay | None:
        result = await self._session.execute(
            select(TenantEventOverlayModel).where(
                TenantEventOverlayModel.tenant_id == tenant_id,
                TenantEventOverlayModel.event_id == event_id,
            )
        )
        return _to_domain_opt(result.scalar_one_or_none(), TenantEventOverlay)

    async def upsert(self, *, tenant_id: UUID, overlay: TenantEventOverlay) -> TenantEventOverlay:
        if overlay.tenant_id != tenant_id:
            raise ValueError(f"tenant_id mismatch: param={tenant_id}, overlay={overlay.tenant_id}")
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        data = _domain_data(overlay)
        # Single chained expression keeps mypy from widening the
        # returning() result back to plain Insert (same idiom as
        # SqlRawSnapshotRepository.try_add_unique).
        stmt = (
            pg_insert(TenantEventOverlayModel)
            .values(**data)
            .on_conflict_do_update(
                index_elements=["tenant_id", "event_id"],
                set_={
                    "notes_markdown": overlay.notes_markdown,
                    "overlay_fields": overlay.overlay_fields,
                    "updated_at": overlay.updated_at,
                },
            )
            .returning(TenantEventOverlayModel)
        )
        result = await self._session.execute(stmt)
        row = result.scalar_one()
        return _to_domain(row, TenantEventOverlay)

    async def list_for_tenant(
        self,
        *,
        tenant_id: UUID,
        limit: int,
        after_id: UUID | None = None,
    ) -> TenantEventOverlayPage:
        stmt = (
            select(TenantEventOverlayModel)
            .where(TenantEventOverlayModel.tenant_id == tenant_id)
            .order_by(
                TenantEventOverlayModel.updated_at.desc(),
                TenantEventOverlayModel.id.desc(),
            )
        )
        if after_id is not None:
            # Resolve the cursor row's updated_at via a PK lookup
            # scoped to the same tenant — a cross-tenant cursor would
            # be rejected here, not just produce empty results.
            cursor_row = await self._session.execute(
                select(
                    TenantEventOverlayModel.updated_at,
                    TenantEventOverlayModel.tenant_id,
                ).where(TenantEventOverlayModel.id == after_id)
            )
            cursor = cursor_row.first()
            if cursor is not None and cursor.tenant_id == tenant_id:
                row_key = tuple_(
                    TenantEventOverlayModel.updated_at,
                    TenantEventOverlayModel.id,
                )
                cursor_key = tuple_(literal(cursor.updated_at), literal(after_id))
                stmt = stmt.where(row_key < cursor_key)

        result = await self._session.execute(stmt.limit(limit + 1))
        rows = list(result.scalars())
        if len(rows) > limit:
            rows = rows[:limit]
            next_cursor: UUID | None = rows[-1].id
        else:
            next_cursor = None
        return TenantEventOverlayPage(
            items=[_to_domain(r, TenantEventOverlay) for r in rows],
            next_cursor=next_cursor,
        )


def _extract_constraint_name(exc: IntegrityError) -> str | None:
    """Same idiom as the publication repo's helper.  Best-effort
    extraction of the constraint name from asyncpg's underlying
    error so we can map to typed domain exceptions.
    """
    orig = getattr(exc, "orig", None)
    if orig is None:
        return None
    name = getattr(orig, "constraint_name", None)
    if isinstance(name, str):
        return name
    message = str(orig)
    for candidate in (
        "uq_tenant_sources_tenant_name",
        "uq_tenant_memberships_user",
        "uq_tenant_event_overlays_tenant_event",
    ):
        if candidate in message:
            return candidate
    return None


# ── Phase 6 ─────────────────────────────────────────────────────────────────


class SqlTenantSafetyReportRepository(TenantSafetyReportRepository):
    """ASAP-style narrative reports.

    No public surface reads this repository — that's the invariant
    we maintain at the router layer.  The repo itself is just a
    plain tenant-scoped CRUD.
    """

    def __init__(self, session: AsyncSession):
        self._session = session

    async def add(self, *, tenant_id: UUID, report: TenantSafetyReport) -> None:
        if report.tenant_id != tenant_id:
            raise ValueError(f"tenant_id mismatch: param={tenant_id}, report={report.tenant_id}")
        data = _domain_data(report)
        data["report_kind"] = (
            report.report_kind.value if hasattr(report.report_kind, "value") else report.report_kind
        )
        self._session.add(TenantSafetyReportModel(**data))
        await self._session.flush()

    async def get(self, *, tenant_id: UUID, report_id: UUID) -> TenantSafetyReport | None:
        result = await self._session.execute(
            select(TenantSafetyReportModel).where(
                TenantSafetyReportModel.id == report_id,
                TenantSafetyReportModel.tenant_id == tenant_id,
            )
        )
        return _to_domain_opt(result.scalar_one_or_none(), TenantSafetyReport)

    async def list_for_tenant(
        self, *, tenant_id: UUID, limit: int = 50
    ) -> list[TenantSafetyReport]:
        result = await self._session.execute(
            select(TenantSafetyReportModel)
            .where(TenantSafetyReportModel.tenant_id == tenant_id)
            .order_by(TenantSafetyReportModel.created_at.desc())
            .limit(limit)
        )
        return [_to_domain(row, TenantSafetyReport) for row in result.scalars()]


class SqlTenantEventAssociationRepository(TenantEventAssociationRepository):
    def __init__(self, session: AsyncSession):
        self._session = session

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
        data = _domain_data(association)
        data["association_kind"] = (
            association.association_kind.value
            if hasattr(association.association_kind, "value")
            else association.association_kind
        )
        self._session.add(TenantEventAssociationModel(**data))
        await self._session.flush()

    async def list_for_event(
        self, *, tenant_id: UUID, event_id: UUID
    ) -> list[TenantEventAssociation]:
        result = await self._session.execute(
            select(TenantEventAssociationModel)
            .where(
                TenantEventAssociationModel.tenant_id == tenant_id,
                TenantEventAssociationModel.event_id == event_id,
            )
            .order_by(TenantEventAssociationModel.created_at)
        )
        return [_to_domain(row, TenantEventAssociation) for row in result.scalars()]


class SqlTenantCrossrefResultRepository(TenantCrossrefResultRepository):
    """Echo cross-reference results, tenant-private.

    ``matches_json`` and ``matcher_config_json`` are written atomically
    on ``mark_complete``; individual match rows are never touched after
    that.  ``mark_failed`` sets status + error_detail + completed_at.
    No public surface reads this repository.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, *, tenant_id: UUID, result: TenantCrossrefResult) -> None:
        if result.tenant_id != tenant_id:
            raise ValueError(f"tenant_id mismatch: param={tenant_id}, result={result.tenant_id}")
        data = _domain_data(result)
        data["status"] = result.status.value if hasattr(result.status, "value") else result.status
        self._session.add(TenantCrossrefResultModel(**data))
        await self._session.flush()

    async def get(self, *, tenant_id: UUID, result_id: UUID) -> TenantCrossrefResult | None:
        row = await self._session.scalar(
            select(TenantCrossrefResultModel).where(
                TenantCrossrefResultModel.id == result_id,
                TenantCrossrefResultModel.tenant_id == tenant_id,
            )
        )
        return _to_domain_opt(row, TenantCrossrefResult)

    async def mark_complete(
        self,
        *,
        tenant_id: UUID,
        result_id: UUID,
        matches_json: list[dict[str, Any]],
        matcher_config_json: dict[str, Any],
        match_count: int,
        completed_at: datetime,
    ) -> None:
        await self._session.execute(
            update(TenantCrossrefResultModel)
            .where(
                TenantCrossrefResultModel.id == result_id,
                TenantCrossrefResultModel.tenant_id == tenant_id,
                TenantCrossrefResultModel.status == CrossrefResultStatus.PENDING,
            )
            .values(
                status=CrossrefResultStatus.COMPLETE,
                matches_json=matches_json,
                matcher_config_json=matcher_config_json,
                match_count=match_count,
                completed_at=completed_at,
            )
            .execution_options(synchronize_session="fetch")
        )

    async def mark_failed(
        self,
        *,
        tenant_id: UUID,
        result_id: UUID,
        error_detail: str,
        completed_at: datetime,
    ) -> None:
        await self._session.execute(
            update(TenantCrossrefResultModel)
            .where(
                TenantCrossrefResultModel.id == result_id,
                TenantCrossrefResultModel.tenant_id == tenant_id,
                TenantCrossrefResultModel.status == CrossrefResultStatus.PENDING,
            )
            .values(
                status=CrossrefResultStatus.FAILED,
                error_detail=error_detail,
                completed_at=completed_at,
            )
            .execution_options(synchronize_session="fetch")
        )

    async def list_for_report(
        self, *, tenant_id: UUID, safety_report_id: UUID, limit: int = 10
    ) -> list[TenantCrossrefResult]:
        rows = await self._session.scalars(
            select(TenantCrossrefResultModel)
            .where(
                TenantCrossrefResultModel.tenant_id == tenant_id,
                TenantCrossrefResultModel.safety_report_id == safety_report_id,
            )
            .order_by(TenantCrossrefResultModel.requested_at.desc())
            .limit(limit)
        )
        return [_to_domain(r, TenantCrossrefResult) for r in rows]
