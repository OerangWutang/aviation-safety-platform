"""Shared helpers for the split ``repositories`` package.

Concentrating these here lets each per-domain repository module
(``sources.py``, ``hermes.py``, ...) import exactly what it needs by
name without dragging in unrelated SQL/ORM dependencies.

Everything here used to live at the top of the old
``repositories.py`` monolith; the behaviour is unchanged.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Iterator
from enum import Enum
from typing import TYPE_CHECKING, Any, TypeVar
from uuid import UUID

from sqlalchemy import literal, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

if TYPE_CHECKING:
    pass

from atlas.domain.constants import (
    DISPUTED_MARKER,
    MAX_REGISTRATION_ALIASES,
    DisputedType,
)
from atlas.domain.exceptions import MappingError

logger = logging.getLogger(__name__)

# Postgres advisory lock namespaces.  Use the two-int form of
# pg_advisory_xact_lock(namespace, key) so unrelated domain locks never share
# the same global 64-bit key space.
ADVISORY_LOCK_SOURCE_RECORD_CORRECTION = 1
ADVISORY_LOCK_REPROJECTION = 2
ADVISORY_LOCK_IDENTITY_RESOLUTION = 3
ADVISORY_LOCK_ORION_IDENTIFIER = 4

# Keep bulk IN/UPDATE statements comfortably below asyncpg/Postgres
# protocol parameter limits, with room for additional bound values.
BULK_ID_CHUNK_SIZE = 10_000

T = TypeVar("T")


def _unwrap_enums(value: Any) -> Any:
    if isinstance(value, DisputedType):
        return DISPUTED_MARKER
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {key: _unwrap_enums(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_unwrap_enums(item) for item in value]
    return value


def _domain_data(entity: Any, *, exclude: set[str] | None = None) -> dict[str, Any]:
    """Serialize a domain entity to a dict while preserving UUID/datetime objects."""
    unwrapped = _unwrap_enums(entity.model_dump(mode="python", exclude=exclude or set()))
    # ``_unwrap_enums`` preserves the dict shape because the input is a dict.
    assert isinstance(unwrapped, dict)
    return unwrapped


def _normalise_registration_lookup(value: str) -> str:
    """Normalize raw or pre-normalized registration text for identity lookup."""
    return re.sub(r"[-/\s]", "", str(value).lower().strip())


def _chunked(items: Iterable[T], size: int = BULK_ID_CHUNK_SIZE) -> Iterator[list[T]]:
    """Yield bounded chunks so bulk SQL never exceeds driver parameter limits."""
    chunk: list[T] = []
    for item in items:
        chunk.append(item)
        if len(chunk) >= size:
            yield chunk
            chunk = []
    if chunk:
        yield chunk


def _capped_jsonb_union_sql(array_expression: str) -> str:
    """Return SQL that unions JSONB array values while keeping newest aliases bounded."""
    return (
        "COALESCE("
        " ("
        " SELECT jsonb_agg(value ORDER BY last_pos, value)"
        " FROM ("
        " SELECT elem AS value, MAX(ord) AS last_pos"
        f" FROM jsonb_array_elements({array_expression}) WITH ORDINALITY AS t(elem, ord)"
        " GROUP BY elem"
        " ORDER BY MAX(ord) DESC, elem ASC"
        f" LIMIT {MAX_REGISTRATION_ALIASES}"
        " ) AS capped_aliases"
        " ),"
        " '[]'::jsonb"
        ")"
    )


async def _apply_created_at_uuid_cursor(
    session: AsyncSession,
    stmt: Any,
    model: Any,
    after_id: UUID | None,
    *,
    descending: bool = False,
) -> Any:
    """Apply a stable keyset cursor ordered by (created_at, id).

    Resolve the cursor row in a separate primary-key lookup before building the
    page query.  Keeping the cursor timestamp as a bound value avoids a scalar
    subquery in the WHERE clause, which gives Postgres concrete bounds for
    planner statistics and keeps the intended composite index scan attractive.
    Invalid/deleted cursors are treated as absent cursors for backward-compatible
    API behavior.
    """
    if after_id is None:
        return stmt

    cursor_created_at = await session.scalar(select(model.created_at).where(model.id == after_id))
    if cursor_created_at is None:
        return stmt

    row_key = tuple_(model.created_at, model.id)
    # SQLAlchemy resolves bare Python scalars at runtime via implicit coercion,
    # but the typed stubs require ColumnElement on both sides of the tuple
    # comparison. ``literal()`` is the documented way to lift Python scalars
    # into the expression language without changing emitted SQL.
    cursor_key = tuple_(literal(cursor_created_at), literal(after_id))
    return stmt.where(row_key < cursor_key if descending else row_key > cursor_key)


async def _apply_last_detected_at_uuid_cursor(
    session: AsyncSession,
    stmt: Any,
    model: Any,
    after_id: UUID | None,
    *,
    descending: bool = True,
) -> Any:
    """Apply a stable keyset cursor ordered by (last_detected_at, id).

    Mirror of :func:`_apply_created_at_uuid_cursor` for tables whose stable
    ordering key is ``last_detected_at`` rather than ``created_at``.  Used by
    :class:`SqlArgusSignalRepository.list_page` to walk
    ``ix_argus_signals_last_detected_id_desc`` (migration 032) without the
    silent-skip/duplicate hazards of offset pagination on a non-unique sort
    key.

    Defaults to ``descending=True`` because Argus signal lists are newest-
    first.  Invalid or stale cursors fall back to "no cursor" — same
    backward-compatible behaviour as the sibling helper.
    """
    if after_id is None:
        return stmt

    cursor_last_detected = await session.scalar(
        select(model.last_detected_at).where(model.id == after_id)
    )
    if cursor_last_detected is None:
        return stmt

    row_key = tuple_(model.last_detected_at, model.id)
    cursor_key = tuple_(literal(cursor_last_detected), literal(after_id))
    return stmt.where(row_key < cursor_key if descending else row_key > cursor_key)


def _to_domain(obj: Any, domain_cls: type[T]) -> T:
    if obj is None:
        raise MappingError(f"Cannot map None to {domain_cls.__name__}")
    data = {column.name: getattr(obj, column.name) for column in obj.__table__.columns}
    try:
        return domain_cls(**data)
    except (TypeError, ValueError, KeyError) as exc:
        # Narrow catch: only handle expected mapping/validation failures.
        # Infrastructure errors should propagate with their original type.
        logger.error(
            "Mapping error (schema drift?) %s -> %s",
            obj.__class__.__name__,
            domain_cls.__name__,
            exc_info=True,
        )
        raise MappingError(
            f"Failed mapping {obj.__class__.__name__} -> {domain_cls.__name__}: {exc}"
        ) from exc


def _to_domain_opt(obj: Any, domain_cls: type[T]) -> T | None:
    if obj is None:
        return None
    return _to_domain(obj, domain_cls)
