"""Tenancy use cases.

Each use case takes ``tenant_id`` as an explicit parameter — even when
the caller already passed it via the auth dependency — because:

- the repository methods require it, and an extra read of ``self._uow.tenants``
  at the use-case layer would mask a missing path-vs-key check;
- defence in depth: a future router that calls a use case directly
  must pass tenant_id, which is a parameter the type checker can
  enforce.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from atlas.application.unit_of_work import UnitOfWork
from atlas.application.use_cases.tenant_ingestion import _require_write_role
from atlas.domain.tenancy.entities import (
    TenantEventOverlay,
    TenantSource,
)
from atlas.domain.tenancy.exceptions import (
    CrossTenantAccessError,
    TenantNotFoundError,
)
from atlas.domain.utils import utc_now

logger = logging.getLogger(__name__)


# ── Register source ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class RegisterTenantSourceInput:
    tenant_id: UUID
    caller_tenant_id: UUID
    caller_tenant_role: str
    name: str
    kind: str = "EXTERNAL"
    reliability_tier: int = 1


class RegisterTenantSource:
    """Register a new source owned by the tenant.

    Permissions: OWNER or MEMBER.  READ_ONLY cannot register sources.

    The use case re-checks the caller's tenant_id against the path
    tenant_id — even though the auth dependency already enforced it —
    so this code path is correct even when called directly (CLI,
    tests, future workers).
    """

    def __init__(self, uow: UnitOfWork):
        self._uow = uow

    async def execute(self, input: RegisterTenantSourceInput) -> TenantSource:
        # Layer 2 of isolation: use case re-verifies the path/key
        # match.  See ARCHITECTURE.md "three isolation layers".
        if input.caller_tenant_id != input.tenant_id:
            raise CrossTenantAccessError(
                caller_tenant_id=input.caller_tenant_id,
                target_tenant_id=input.tenant_id,
            )
        _require_write_role(input.caller_tenant_role)

        source = TenantSource(
            tenant_id=input.tenant_id,
            name=input.name,
            kind=input.kind,
            reliability_tier=input.reliability_tier,
        )
        # Repository (layer 3): tenant_id is a required parameter on
        # the method signature.  Calling the repo without it would
        # raise TypeError.
        await self._uow.tenant_sources.add(tenant_id=input.tenant_id, source=source)
        await self._uow.commit()
        return source


# ── Get event overlay ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class TenantEventOverlayView:
    """Composed view: the overlay + a snapshot of the public projection.

    A tenant overlay only makes sense in context of the public event
    it's annotating.  The use case fetches both and returns them as a
    single composed view so the router doesn't need to do a second
    read.
    """

    event_id: UUID
    overlay: TenantEventOverlay | None
    public_fields: dict[str, Any]
    public_completeness_score: float
    public_projection_version: int


class GetTenantEventOverlay:
    """Read one event's tenant overlay alongside the public projection.

    Returns the public projection even when no overlay exists yet —
    that's the common "I want to start writing an overlay; what do I
    see today?" case.  ``overlay=None`` is the explicit signal.
    """

    def __init__(self, uow: UnitOfWork):
        self._uow = uow

    async def execute(
        self,
        *,
        tenant_id: UUID,
        caller_tenant_id: UUID,
        event_id: UUID,
    ) -> TenantEventOverlayView:
        if caller_tenant_id != tenant_id:
            raise CrossTenantAccessError(
                caller_tenant_id=caller_tenant_id,
                target_tenant_id=tenant_id,
            )
        projection = await self._uow.projections.get(event_id)
        if projection is None:
            raise TenantNotFoundError(f"No public projection exists for event {event_id}")
        overlay = await self._uow.tenant_event_overlays.get(tenant_id=tenant_id, event_id=event_id)
        await self._uow.rollback()
        return TenantEventOverlayView(
            event_id=event_id,
            overlay=overlay,
            public_fields=projection.fields,
            public_completeness_score=projection.completeness_score,
            public_projection_version=projection.projection_version,
        )


# ── List tenant events ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class TenantEventListItem:
    event_id: UUID
    has_overlay: bool
    overlay_updated_at: datetime | None
    notes_preview: str | None


@dataclass(frozen=True)
class TenantEventListResult:
    items: list[TenantEventListItem]
    next_cursor: UUID | None
    limit: int


# Hard cap on per-request page size; protects an expensive plan from
# being forced by an adversarial caller.
_TENANT_EVENT_LIST_MAX_LIMIT = 100
_TENANT_EVENT_LIST_DEFAULT_LIMIT = 25


class ListTenantEvents:
    """List events that have tenant overlays.

    Returns only events the tenant has *touched* (has an overlay row for).

    Permissions: any tenant role can list.  Tenant-side OWNER and
    READ_ONLY get the same view.
    """

    def __init__(self, uow: UnitOfWork):
        self._uow = uow

    async def execute(
        self,
        *,
        tenant_id: UUID,
        caller_tenant_id: UUID,
        limit: int = _TENANT_EVENT_LIST_DEFAULT_LIMIT,
        after_id: UUID | None = None,
    ) -> TenantEventListResult:
        if caller_tenant_id != tenant_id:
            raise CrossTenantAccessError(
                caller_tenant_id=caller_tenant_id,
                target_tenant_id=tenant_id,
            )
        bounded_limit = max(1, min(limit, _TENANT_EVENT_LIST_MAX_LIMIT))
        page = await self._uow.tenant_event_overlays.list_for_tenant(
            tenant_id=tenant_id, limit=bounded_limit, after_id=after_id
        )
        await self._uow.rollback()
        items = [
            TenantEventListItem(
                event_id=o.event_id,
                has_overlay=True,
                overlay_updated_at=o.updated_at,
                notes_preview=(_preview(o.notes_markdown) if o.notes_markdown else None),
            )
            for o in page.items
        ]
        return TenantEventListResult(
            items=items,
            next_cursor=page.next_cursor,
            limit=bounded_limit,
        )


def _preview(text: str, *, max_chars: int = 160) -> str:
    """Return a short single-line preview of the notes content.

    Strips Markdown header markers from the leading characters so the
    preview reads as prose, not as raw syntax.  Doesn't try to be a
    full Markdown renderer — anything beyond leading `#` markers
    stays as-is.
    """
    stripped = text.lstrip().lstrip("#").lstrip()
    one_line = " ".join(stripped.split())
    if len(one_line) <= max_chars:
        return one_line
    return one_line[: max_chars - 1] + "\u2026"


# ── Upsert event overlay (write path; permission-gated) ──────────────────────


@dataclass(frozen=True)
class UpsertTenantEventOverlayInput:
    tenant_id: UUID
    caller_tenant_id: UUID
    caller_tenant_role: str
    event_id: UUID
    notes_markdown: str | None = None
    overlay_fields: dict[str, Any] | None = None


class UpsertTenantEventOverlay:
    """Create or replace the overlay for ``(tenant_id, event_id)``.

    The overlay write is a simple last-write-wins upsert.

    Permissions: OWNER or MEMBER.  READ_ONLY cannot write overlays.
    """

    def __init__(self, uow: UnitOfWork):
        self._uow = uow

    async def execute(self, input: UpsertTenantEventOverlayInput) -> TenantEventOverlay:
        if input.caller_tenant_id != input.tenant_id:
            raise CrossTenantAccessError(
                caller_tenant_id=input.caller_tenant_id,
                target_tenant_id=input.tenant_id,
            )
        _require_write_role(input.caller_tenant_role)
        # Validate event exists in public canonical store.  This
        # closes off the "create an overlay for a non-existent event
        # id" attack.
        projection = await self._uow.projections.get(input.event_id)
        if projection is None:
            raise TenantNotFoundError(f"No public projection exists for event {input.event_id}")
        moment = utc_now()
        existing = await self._uow.tenant_event_overlays.get(
            tenant_id=input.tenant_id, event_id=input.event_id
        )
        if existing is None:
            overlay = TenantEventOverlay(
                tenant_id=input.tenant_id,
                event_id=input.event_id,
                notes_markdown=input.notes_markdown,
                overlay_fields=input.overlay_fields or {},
                created_at=moment,
                updated_at=moment,
            )
        else:
            overlay = existing.model_copy(
                update={
                    "notes_markdown": input.notes_markdown,
                    "overlay_fields": input.overlay_fields or {},
                    "updated_at": moment,
                }
            )
        result = await self._uow.tenant_event_overlays.upsert(
            tenant_id=input.tenant_id, overlay=overlay
        )
        await self._uow.commit()
        return result


__all__ = [
    "GetTenantEventOverlay",
    "ListTenantEvents",
    "RegisterTenantSource",
    "RegisterTenantSourceInput",
    "TenantEventListItem",
    "TenantEventListResult",
    "TenantEventOverlayView",
    "UpsertTenantEventOverlay",
    "UpsertTenantEventOverlayInput",
]
