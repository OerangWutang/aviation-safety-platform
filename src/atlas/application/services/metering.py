"""Metering service (Phase 8).

A thin helper that records a usage event.  Use-case call sites
invoke ``MeteringService.record(...)`` as the last step before
commit so a metered action and its meter land in the same UoW —
either both commit or neither does.

The service is intentionally minimal: it constructs a
``UsageEvent`` and hands it to the repo.  It does NOT commit; the
calling use case owns the transaction boundary (consistent with
the UoW discipline across all phases).

Wiring guidance
---------------

A metered action's use case takes a ``MeteringService`` (or
constructs one from its UoW) and calls ``record`` right before
``await uow.commit()``.  Example (pseudo):

    await self._uow.tenant_claims.add_many(...)
    await MeteringService(self._uow).record(
        metric_kind=MetricKind.TENANT_CLAIM_INGESTED,
        tenant_id=tenant_id,
        user_id=user_id,
        resource_id=event_id,
        quantity=len(claims),
    )
    await self._uow.commit()

``quantity`` lets a batch action record N events with one call —
the service emits N rows.  This keeps per-claim granularity in the
``usage_events`` audit trail while letting batch use cases stay
terse.
"""

from __future__ import annotations

from uuid import UUID

from atlas.application.unit_of_work import UnitOfWork
from atlas.domain.metering.entities import MetricKind, UsageEvent


class MeteringService:
    def __init__(self, uow: UnitOfWork):
        self._uow = uow

    async def record(
        self,
        *,
        metric_kind: MetricKind,
        tenant_id: UUID | None = None,
        user_id: UUID | None = None,
        resource_id: UUID | None = None,
        quantity: int = 1,
    ) -> None:
        """Emit ``quantity`` usage events of the given kind.

        Does not commit — the caller owns the transaction.  A
        ``quantity`` < 1 is a no-op (a batch action that processed
        zero items records nothing).

        The events are inserted via a single bulk ``add_many`` so a
        large-quantity recording (e.g. a 1000-claim batch) costs one
        database round trip, not N.
        """
        n = max(0, quantity)
        if n == 0:
            return
        events = [
            UsageEvent(
                metric_kind=metric_kind,
                tenant_id=tenant_id,
                user_id=user_id,
                resource_id=resource_id,
            )
            for _ in range(n)
        ]
        await self._uow.usage_events.add_many(events)


__all__ = ["MeteringService"]
