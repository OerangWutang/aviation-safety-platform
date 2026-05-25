"""SQLAlchemy repositories for the identity aggregate.

Carved out of the old ``repositories.py`` monolith in r9; behaviour
unchanged.  Public ``Sql*`` classes are re-exported from
``atlas.infrastructure.db.repositories`` so existing imports keep
working.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import JSONB, insert
from sqlalchemy.ext.asyncio import AsyncSession

from atlas.domain.constants import MAX_REGISTRATION_ALIASES
from atlas.domain.entities import (
    EventIdentityIndex,
)
from atlas.domain.interfaces.repositories import (
    EventIdentityIndexRepository,
)
from atlas.infrastructure.db.orm_models import (
    AccidentEventModel,
    EventIdentityIndexModel,
)
from atlas.infrastructure.db.repositories._helpers import (
    ADVISORY_LOCK_IDENTITY_RESOLUTION,
    _capped_jsonb_union_sql,
    _domain_data,
    _normalise_registration_lookup,
    _to_domain,
    logger,
)


class SqlEventIdentityIndexRepository(EventIdentityIndexRepository):
    """SQL-backed synchronous event identity substrate.

    Written in the ingestion transaction (before commit) so that the next
    ingestion arriving for the same accident finds the entry immediately,
    without waiting for the outbox worker to build a projection.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def upsert(self, entry: EventIdentityIndex) -> None:
        """Insert or update the identity record.

        On conflict (same event_id):
          - Update each field only if the incoming value is non-None,
            preserving richer data already stored for this event.
          - Accumulate source_record_ids and registration_norms via JSONB
            array union so the lists grow rather than being overwritten.
          - Always bump updated_at.
        """
        data = _domain_data(entry)
        stmt = insert(EventIdentityIndexModel).values(**data)
        stmt = stmt.on_conflict_do_update(
            index_elements=["event_id"],
            set_={
                # Coalesce: keep existing value if the new one is NULL,
                # otherwise take the new (richer) value.
                "event_date_norm": func.coalesce(
                    stmt.excluded.event_date_norm,
                    EventIdentityIndexModel.event_date_norm,
                ),
                "registration_norm": func.coalesce(
                    stmt.excluded.registration_norm,
                    EventIdentityIndexModel.registration_norm,
                ),
                "operator_norm": func.coalesce(
                    stmt.excluded.operator_norm,
                    EventIdentityIndexModel.operator_norm,
                ),
                "location_norm": func.coalesce(
                    stmt.excluded.location_norm,
                    EventIdentityIndexModel.location_norm,
                ),
                "aircraft_type_norm": func.coalesce(
                    stmt.excluded.aircraft_type_norm,
                    EventIdentityIndexModel.aircraft_type_norm,
                ),
                # Merge source_record_id arrays: union via JSONB array ops.
                # COALESCE(..., '[]'::jsonb) guards against jsonb_agg returning
                # NULL when both operand arrays are empty (zero input rows).
                "source_record_ids": text(
                    "COALESCE("
                    "  (SELECT jsonb_agg(DISTINCT elem)"
                    "   FROM jsonb_array_elements("
                    "     event_identity_index.source_record_ids || EXCLUDED.source_record_ids"
                    "   ) AS elem),"
                    "  '[]'::jsonb"
                    ")"
                ),
                # Accumulate registration aliases with a hard cap.  This keeps
                # historical lookup useful without letting bad/malicious source
                # data append unbounded aliases that review-bomb curators.
                "registration_norms": text(
                    _capped_jsonb_union_sql(
                        "event_identity_index.registration_norms || EXCLUDED.registration_norms"
                    )
                ),
                "updated_at": stmt.excluded.updated_at,
            },
        )
        await self._session.execute(stmt)

    async def enrich_identity_index_from_alias(
        self,
        entry: EventIdentityIndex,
    ) -> None:
        """Enrich a canonical identity row without clobbering scalars.

        Regular ``upsert`` intentionally prefers incoming non-null scalar
        values.  That is correct for direct ingestions into the canonical event,
        but unsafe when the match came through a merged-event alias: the alias
        may be an old/corrected-away registration.  Here target scalars win,
        while arrays still union so aliases and source_record_ids are retained.
        """
        data = _domain_data(entry)
        stmt = insert(EventIdentityIndexModel).values(**data)
        stmt = stmt.on_conflict_do_update(
            index_elements=["event_id"],
            set_={
                "event_date_norm": func.coalesce(
                    EventIdentityIndexModel.event_date_norm,
                    stmt.excluded.event_date_norm,
                ),
                "registration_norm": func.coalesce(
                    EventIdentityIndexModel.registration_norm,
                    stmt.excluded.registration_norm,
                ),
                "operator_norm": func.coalesce(
                    EventIdentityIndexModel.operator_norm,
                    stmt.excluded.operator_norm,
                ),
                "location_norm": func.coalesce(
                    EventIdentityIndexModel.location_norm,
                    stmt.excluded.location_norm,
                ),
                "aircraft_type_norm": func.coalesce(
                    EventIdentityIndexModel.aircraft_type_norm,
                    stmt.excluded.aircraft_type_norm,
                ),
                "source_record_ids": text(
                    "COALESCE("
                    "  (SELECT jsonb_agg(DISTINCT elem)"
                    "   FROM jsonb_array_elements("
                    "     event_identity_index.source_record_ids || EXCLUDED.source_record_ids"
                    "   ) AS elem),"
                    "  '[]'::jsonb"
                    ")"
                ),
                "registration_norms": text(
                    _capped_jsonb_union_sql(
                        "event_identity_index.registration_norms"
                        " || EXCLUDED.registration_norms"
                        " || CASE"
                        "      WHEN EXCLUDED.registration_norm IS NOT NULL"
                        "      THEN jsonb_build_array(EXCLUDED.registration_norm)"
                        "      ELSE '[]'::jsonb"
                        "    END"
                    )
                ),
                "updated_at": stmt.excluded.updated_at,
            },
        )
        await self._session.execute(stmt)

    async def merge_identity_index(
        self,
        source_event_id: UUID,
        target_event_id: UUID,
    ) -> None:
        """Union source identity aliases into the canonical target row.

        If the target row exists, its scalar identity fields win and source
        scalars fill only NULL gaps.  If the target row is missing but the
        source row exists, a target row is created from the source identity
        data.  Arrays are always unioned.  The source row is intentionally left
        in place as a historical alias for older candidate lookups.
        """
        result = await self._session.execute(
            text(
                f"""
                INSERT INTO event_identity_index (
                    event_id,
                    event_date_norm,
                    registration_norm,
                    operator_norm,
                    location_norm,
                    aircraft_type_norm,
                    source_record_ids,
                    registration_norms,
                    updated_at
                )
                SELECT
                    :target_event_id,
                    src.event_date_norm,
                    src.registration_norm,
                    src.operator_norm,
                    src.location_norm,
                    src.aircraft_type_norm,
                    src.source_record_ids,
                    COALESCE(
                        (
                            SELECT jsonb_agg(value ORDER BY last_pos, value)
                            FROM (
                                SELECT elem AS value, MAX(ord) AS last_pos
                                FROM jsonb_array_elements(
                                    src.registration_norms
                                    || CASE
                                           WHEN src.registration_norm IS NOT NULL
                                           THEN jsonb_build_array(src.registration_norm)
                                           ELSE '[]'::jsonb
                                       END
                                ) WITH ORDINALITY AS t(elem, ord)
                                GROUP BY elem
                                ORDER BY MAX(ord) DESC, elem ASC
                                LIMIT {MAX_REGISTRATION_ALIASES}
                            ) AS capped_aliases
                        ),
                        '[]'::jsonb
                    ),
                    NOW()
                FROM event_identity_index AS src
                WHERE src.event_id = :source_event_id
                ON CONFLICT (event_id) DO UPDATE
                SET
                    event_date_norm = COALESCE(
                        event_identity_index.event_date_norm,
                        EXCLUDED.event_date_norm
                    ),
                    registration_norm = COALESCE(
                        event_identity_index.registration_norm,
                        EXCLUDED.registration_norm
                    ),
                    operator_norm = COALESCE(
                        event_identity_index.operator_norm,
                        EXCLUDED.operator_norm
                    ),
                    location_norm = COALESCE(
                        event_identity_index.location_norm,
                        EXCLUDED.location_norm
                    ),
                    aircraft_type_norm = COALESCE(
                        event_identity_index.aircraft_type_norm,
                        EXCLUDED.aircraft_type_norm
                    ),
                    source_record_ids = COALESCE(
                        (
                            SELECT jsonb_agg(DISTINCT elem)
                            FROM jsonb_array_elements(
                                event_identity_index.source_record_ids
                                || EXCLUDED.source_record_ids
                            ) AS elem
                        ),
                        '[]'::jsonb
                    ),
                    registration_norms = COALESCE(
                        (
                            SELECT jsonb_agg(value ORDER BY last_pos, value)
                            FROM (
                                SELECT elem AS value, MAX(ord) AS last_pos
                                FROM jsonb_array_elements(
                                    event_identity_index.registration_norms
                                    || EXCLUDED.registration_norms
                                    || CASE
                                           WHEN EXCLUDED.registration_norm IS NOT NULL
                                           THEN jsonb_build_array(EXCLUDED.registration_norm)
                                           ELSE '[]'::jsonb
                                       END
                                ) WITH ORDINALITY AS t(elem, ord)
                                GROUP BY elem
                                ORDER BY MAX(ord) DESC, elem ASC
                                LIMIT {MAX_REGISTRATION_ALIASES}
                            ) AS capped_aliases
                        ),
                        '[]'::jsonb
                    ),
                    updated_at = NOW()
                """
            ),
            {
                "source_event_id": source_event_id,
                "target_event_id": target_event_id,
            },
        )
        if getattr(result, "rowcount", 0) == 0:
            logger.warning(
                "merge_identity_index: source identity row missing for %s -> %s",
                source_event_id,
                target_event_id,
            )

    async def find_candidates(
        self,
        event_date_norm: str,
        limit: int = 50,
    ) -> list[EventIdentityIndex]:
        """Return identity entries whose date is within ±1 day.

        Merged events are intentionally included as historical identity aliases;
        the use case resolves candidates to their canonical event before writing.
        ISO dates (YYYY-MM-DD) are lexicographically ordered, so string
        BETWEEN works correctly without a DATE cast.  Pre-computing the
        boundary strings in Python avoids any timezone conversion surprises.
        """
        from datetime import date as _date
        from datetime import timedelta as _td

        try:
            centre = _date.fromisoformat(event_date_norm)
        except (ValueError, TypeError):
            return []
        lo = str(centre - _td(days=1))
        hi = str(centre + _td(days=1))

        result = await self._session.execute(
            select(EventIdentityIndexModel)
            .join(
                AccidentEventModel,
                AccidentEventModel.id == EventIdentityIndexModel.event_id,
            )
            .where(
                EventIdentityIndexModel.event_date_norm.isnot(None),
                EventIdentityIndexModel.event_date_norm.between(lo, hi),
            )
            # Merged events are returned as historical aliases (the use case
            # resolves them via merge pointers), but a merged tombstone must
            # never out-rank a canonical event with the same date - otherwise
            # the matcher computes its score and matched_fields against an
            # absorbed event's scalars instead of the surviving canonical row.
            # Mirrors the ordering used by ``find_by_registration``.
            .order_by(
                AccidentEventModel.merged_into_event_id.isnot(None).asc(),
                EventIdentityIndexModel.updated_at.desc(),
            )
            .limit(limit)
        )
        return [_to_domain(obj, EventIdentityIndex) for obj in result.scalars()]

    async def lock_for_identity_resolution(
        self,
        event_date_norm: str,
        registration_norm: str | None,
    ) -> None:
        """Acquire a transaction-scoped advisory lock for this identity key.

        Two concurrent ingestions with the same (date, registration) will
        serialise here.  The second transaction will then find the identity
        entry already written by the first rather than both creating new events.

        The lock uses the two-int advisory-lock form with a dedicated identity
        namespace and ``hashtext('{date}:{reg}')``. Two different identity keys
        may still collide within this namespace, causing extra serialization,
        but they cannot collide with source-record correction or reprojection
        locks.

        The in-memory fake is a no-op because unit tests run single-threaded.
        """
        key = f"{event_date_norm}:{registration_norm or ''}"
        await self._session.execute(
            text("SELECT pg_advisory_xact_lock(CAST(:namespace AS integer), hashtext(:k))"),
            {"namespace": ADVISORY_LOCK_IDENTITY_RESOLUTION, "k": key},
        )

    async def find_by_registration(
        self,
        registration_norm: str,
        event_date_norm: str | None = None,
    ) -> list[EventIdentityIndex]:
        """Find identity entries by primary or historical registration alias.

        ``registration_norm`` may be passed as raw text (``N-OLD``) or in the
        stored normalized form (``nold``).  Normalize defensively here so
        diagnostics/tests/admin code cannot accidentally miss aliases by using
        the human-readable registration spelling.
        """
        registration_norm = _normalise_registration_lookup(registration_norm)
        from datetime import date as _date
        from datetime import timedelta as _td

        from sqlalchemy import cast, or_

        conditions: list[Any] = [
            or_(
                EventIdentityIndexModel.registration_norm == registration_norm,
                # JSONB @> operator: does the stored array contain this element?
                # Requires migration 013's GIN index for efficient execution.
                EventIdentityIndexModel.registration_norms.contains(
                    cast([registration_norm], JSONB)
                ),
            )
        ]
        if event_date_norm:
            try:
                centre = _date.fromisoformat(event_date_norm)
                lo = str(centre - _td(days=1))
                hi = str(centre + _td(days=1))
                conditions.append(EventIdentityIndexModel.event_date_norm.between(lo, hi))
            except (ValueError, TypeError):
                pass

        result = await self._session.execute(
            select(EventIdentityIndexModel)
            .join(
                AccidentEventModel,
                AccidentEventModel.id == EventIdentityIndexModel.event_id,
            )
            .where(*conditions)
            .order_by(
                AccidentEventModel.merged_into_event_id.isnot(None).asc(),
                EventIdentityIndexModel.updated_at.desc(),
                EventIdentityIndexModel.event_id.asc(),
            )
            .limit(20)  # Safety cap; registration is nearly unique per event
        )
        return [_to_domain(obj, EventIdentityIndex) for obj in result.scalars()]


# ── Orion SQLAlchemy Repositories ────────────────────────────────────────────
