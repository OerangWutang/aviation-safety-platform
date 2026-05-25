"""SQLAlchemy repositories for the argus aggregate.

Carved out of the old ``repositories.py`` monolith in r9; behaviour
unchanged.  Public ``Sql*`` classes are re-exported from
``atlas.infrastructure.db.repositories`` so existing imports keep
working.
"""

from __future__ import annotations

import builtins
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import case, literal, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from atlas.domain.entities import (
    ArgusSignal,
    ArgusSignalEvidence,
    ArgusSignalReview,
)
from atlas.domain.enums import (
    ArgusEvidenceType,
    ArgusReviewDecision,
    ArgusSeverity,
    ArgusSignalStatus,
    ArgusSignalType,
)
from atlas.domain.interfaces.repositories import (
    ArgusSignalEvidenceRepository,
    ArgusSignalRepository,
    ArgusSignalReviewRepository,
)
from atlas.domain.services.argus_severity import severity_rank
from atlas.infrastructure.db.orm_models import (
    ArgusSignalEvidenceModel,
    ArgusSignalModel,
    ArgusSignalReviewModel,
)
from atlas.infrastructure.db.repositories._helpers import (
    _apply_last_detected_at_uuid_cursor,
    _domain_data,
)


def _argus_signal_to_domain(row: ArgusSignalModel) -> ArgusSignal:
    return ArgusSignal(
        id=row.id,
        signal_type=ArgusSignalType(row.signal_type),
        status=ArgusSignalStatus(row.status),
        severity=ArgusSeverity(row.severity),
        confidence=row.confidence,
        title=row.title,
        description=row.description,
        accident_event_id=row.accident_event_id,
        primary_entity_id=row.primary_entity_id,
        source_engine=row.source_engine,
        dedupe_key=row.dedupe_key,
        # ``version`` (added in migration 033) is the optimistic-concurrency
        # token.  Forgetting to copy this back would make every SQL read
        # report ``version=1`` and silently break the reviewer race check.
        version=row.version,
        first_detected_at=row.first_detected_at,
        last_detected_at=row.last_detected_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _argus_evidence_to_domain(row: ArgusSignalEvidenceModel) -> ArgusSignalEvidence:
    return ArgusSignalEvidence(
        id=row.id,
        signal_id=row.signal_id,
        evidence_type=ArgusEvidenceType(row.evidence_type),
        evidence_id=row.evidence_id,
        engine=row.engine,
        summary=row.summary,
        created_at=row.created_at,
    )


def _argus_review_to_domain(row: ArgusSignalReviewModel) -> ArgusSignalReview:
    return ArgusSignalReview(
        id=row.id,
        signal_id=row.signal_id,
        decision=ArgusReviewDecision(row.decision),
        reviewer_id=row.reviewer_id,
        note=row.note,
        created_at=row.created_at,
    )


class SqlArgusSignalRepository(ArgusSignalRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, id: UUID) -> ArgusSignal | None:
        row = await self._session.get(ArgusSignalModel, id)
        return _argus_signal_to_domain(row) if row else None

    async def add(self, signal: ArgusSignal) -> None:
        self._session.add(ArgusSignalModel(**_domain_data(signal)))

    async def save(self, signal: ArgusSignal) -> None:
        row = await self._session.get(ArgusSignalModel, signal.id)
        if row is None:
            self._session.add(ArgusSignalModel(**_domain_data(signal)))
            return
        data = _domain_data(signal)
        for k, v in data.items():
            setattr(row, k, v)

    async def find_by_dedupe_key(self, dedupe_key: str) -> ArgusSignal | None:
        result = await self._session.execute(
            select(ArgusSignalModel).where(ArgusSignalModel.dedupe_key == dedupe_key)
        )
        row = result.scalar_one_or_none()
        return _argus_signal_to_domain(row) if row else None

    async def upsert_signal(self, signal: ArgusSignal) -> tuple[ArgusSignal, bool]:
        """Atomically insert or refresh an Argus signal.

        Uses ``INSERT … ON CONFLICT (dedupe_key) DO UPDATE`` so two concurrent
        detection workers cannot both see "no row" and both try to insert —
        one would succeed and the other would fail with an ``IntegrityError``
        under the old select-then-insert pattern.  With this approach one
        writer wins the INSERT and the other lands in the DO UPDATE branch;
        both return the current row via ``RETURNING``.

        The ``created`` boolean is derived from ``xmax``: Postgres sets ``xmax``
        to zero on a freshly-inserted row, so ``xmax = 0`` → created,
        ``xmax != 0`` → updated.  This avoids a second SELECT to determine
        which branch ran.

        Version is intentionally NOT touched on upsert — detection passes are
        non-editorial and must not invalidate in-flight reviewer state.
        """
        now = datetime.now(UTC)
        # Severity escalation rank — computed once in Python from trusted enum
        # values (no user input).  Passed as a bound literal to the CASE so no
        # SQL interpolation risk.
        incoming_severity_rank = severity_rank(signal.severity)
        # Map stored severity string → integer rank, all within a SQL CASE.
        # Using SQLAlchemy ``case()`` instead of raw f-strings makes the
        # expression a parameterised bound value, not string interpolation.
        stored_rank_expr = case(
            (ArgusSignalModel.severity == "LOW", literal(0)),
            (ArgusSignalModel.severity == "MEDIUM", literal(1)),
            (ArgusSignalModel.severity == "HIGH", literal(2)),
            (ArgusSignalModel.severity == "CRITICAL", literal(3)),
            else_=literal(0),
        )
        severity_update_expr = case(
            (stored_rank_expr < literal(incoming_severity_rank), literal(signal.severity.value)),
            else_=ArgusSignalModel.severity,
        )
        confidence_update_expr = case(
            (ArgusSignalModel.confidence < literal(signal.confidence), literal(signal.confidence)),
            else_=ArgusSignalModel.confidence,
        )
        status_update_expr = case(
            (
                ArgusSignalModel.status == literal(ArgusSignalStatus.AUTO_RESOLVED.value),
                literal(ArgusSignalStatus.OPEN.value),
            ),
            else_=ArgusSignalModel.status,
        )
        stmt = (
            insert(ArgusSignalModel)
            .values(
                id=signal.id,
                signal_type=signal.signal_type.value,
                status=ArgusSignalStatus.OPEN.value,
                severity=signal.severity.value,
                confidence=signal.confidence,
                title=signal.title,
                description=signal.description,
                accident_event_id=signal.accident_event_id,
                primary_entity_id=signal.primary_entity_id,
                source_engine=signal.source_engine,
                dedupe_key=signal.dedupe_key,
                version=1,
                first_detected_at=signal.first_detected_at,
                last_detected_at=signal.last_detected_at,
                created_at=now,
                updated_at=now,
            )
            .on_conflict_do_update(
                index_elements=["dedupe_key"],
                set_={
                    "last_detected_at": signal.last_detected_at,
                    "updated_at": now,
                    "status": status_update_expr,
                    "severity": severity_update_expr,
                    "confidence": confidence_update_expr,
                },
            )
            .returning(ArgusSignalModel, text("(xmax = 0) AS was_inserted"))
        )
        result = await self._session.execute(stmt)
        row, was_inserted = result.one()
        return (_argus_signal_to_domain(row), bool(was_inserted))

    async def list(
        self,
        status: ArgusSignalStatus | None = None,
        signal_type: ArgusSignalType | None = None,
        severity: ArgusSeverity | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[ArgusSignal]:
        q = select(ArgusSignalModel)
        if status is not None:
            q = q.where(ArgusSignalModel.status == status.value)
        if signal_type is not None:
            q = q.where(ArgusSignalModel.signal_type == signal_type.value)
        if severity is not None:
            q = q.where(ArgusSignalModel.severity == severity.value)
        # G6: ``last_detected_at`` is not unique — a single detection pass
        # stamps many signals with the same ``now``.  Without a deterministic
        # tiebreaker, offset pagination can silently skip or duplicate rows.
        # ``id DESC`` is monotonically unique and supported by the composite
        # index ``ix_argus_signals_last_detected_id_desc`` (migration 032).
        q = (
            q.order_by(
                ArgusSignalModel.last_detected_at.desc(),
                ArgusSignalModel.id.desc(),
            )
            .limit(limit)
            .offset(offset)
        )
        result = await self._session.execute(q)
        return [_argus_signal_to_domain(r) for r in result.scalars().all()]

    async def list_page(
        self,
        *,
        status: ArgusSignalStatus | None = None,
        signal_type: ArgusSignalType | None = None,
        severity: ArgusSeverity | None = None,
        limit: int = 50,
        after_id: UUID | None = None,
    ) -> builtins.list[ArgusSignal]:
        # ``builtins.list`` (not bare ``list``) because the sibling ``list``
        # method above shadows the builtin in this class body and would make
        # the annotation parse as ``<method>[ArgusSignal]`` under
        # ``from __future__ import annotations``.  The interface module
        # avoids this by defining ``list_page`` before ``list``; the
        # implementation here keeps the public method order matching the
        # original repositories.py monolith for diff stability.
        # Keyset variant of ``list``.  Uses the composite index from
        # migration 032 plus a ``(last_detected_at, id) <`` keyset
        # predicate to walk the table without the silent-skip/duplicate
        # hazards of offset pagination.
        q = select(ArgusSignalModel)
        if status is not None:
            q = q.where(ArgusSignalModel.status == status.value)
        if signal_type is not None:
            q = q.where(ArgusSignalModel.signal_type == signal_type.value)
        if severity is not None:
            q = q.where(ArgusSignalModel.severity == severity.value)
        q = q.order_by(
            ArgusSignalModel.last_detected_at.desc(),
            ArgusSignalModel.id.desc(),
        ).limit(limit)
        q = await _apply_last_detected_at_uuid_cursor(
            self._session, q, ArgusSignalModel, after_id, descending=True
        )
        result = await self._session.execute(q)
        return [_argus_signal_to_domain(r) for r in result.scalars().all()]

    async def update_with_version_check(
        self,
        signal_id: UUID,
        expected_version: int,
        updates: dict[str, Any],
    ) -> ArgusSignal | None:
        # The SQL ``UPDATE … WHERE id = ? AND version = ?`` is atomic at the
        # row level: at most one concurrent reviewer can land the change.
        # The losing race returns zero rows from ``RETURNING``, which we
        # surface as ``None`` for the caller to map to an
        # ``ArgusSignalModifiedError`` 409.  ``updated_at`` is bumped here so
        # the client can detect freshness without an extra round-trip.
        stmt = (
            update(ArgusSignalModel)
            .where(
                ArgusSignalModel.id == signal_id,
                ArgusSignalModel.version == expected_version,
            )
            .values(
                **updates,
                version=ArgusSignalModel.version + 1,
                updated_at=datetime.now(UTC),
            )
            .returning(ArgusSignalModel)
        )
        result = await self._session.execute(stmt)
        row = result.scalar_one_or_none()
        return _argus_signal_to_domain(row) if row else None


class SqlArgusSignalEvidenceRepository(ArgusSignalEvidenceRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, evidence: ArgusSignalEvidence) -> None:
        self._session.add(ArgusSignalEvidenceModel(**_domain_data(evidence)))

    async def upsert_evidence(
        self, evidence: ArgusSignalEvidence
    ) -> tuple[ArgusSignalEvidence, bool]:
        """Atomically insert or skip an evidence link.

        Uses ``INSERT … ON CONFLICT (signal_id, evidence_type, evidence_id)
        DO NOTHING`` so concurrent detection passes that try to link the same
        evidence row to the same signal are both safe: one inserts, the other
        silently skips.  The ``RETURNING`` clause only yields a row on a real
        insert; a DO-NOTHING skips ``RETURNING``, so we re-select to get the
        persisted row in that case.

        This replaces the previous select-then-insert pattern, which had a
        TOCTOU race under concurrent callers.
        """
        stmt = (
            insert(ArgusSignalEvidenceModel)
            .values(
                id=evidence.id,
                signal_id=evidence.signal_id,
                evidence_type=evidence.evidence_type.value,
                evidence_id=evidence.evidence_id,
                engine=evidence.engine,
                summary=evidence.summary,
                created_at=datetime.now(UTC),
            )
            .on_conflict_do_nothing(
                constraint="uq_argus_signal_evidence_link",
            )
            .returning(ArgusSignalEvidenceModel)
        )
        result = await self._session.execute(stmt)
        row = result.scalar_one_or_none()
        if row is not None:
            return (_argus_evidence_to_domain(row), True)
        # DO NOTHING branch — re-select to return the pre-existing row.
        existing = await self._session.execute(
            select(ArgusSignalEvidenceModel).where(
                ArgusSignalEvidenceModel.signal_id == evidence.signal_id,
                ArgusSignalEvidenceModel.evidence_type == evidence.evidence_type.value,
                ArgusSignalEvidenceModel.evidence_id == evidence.evidence_id,
            )
        )
        row = existing.scalar_one()
        return (_argus_evidence_to_domain(row), False)

    async def list_for_signal(self, signal_id: UUID) -> list[ArgusSignalEvidence]:
        result = await self._session.execute(
            select(ArgusSignalEvidenceModel)
            .where(ArgusSignalEvidenceModel.signal_id == signal_id)
            .order_by(ArgusSignalEvidenceModel.created_at.asc())
        )
        return [_argus_evidence_to_domain(r) for r in result.scalars().all()]


class SqlArgusSignalReviewRepository(ArgusSignalReviewRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, review: ArgusSignalReview) -> None:
        self._session.add(ArgusSignalReviewModel(**_domain_data(review)))

    async def list_for_signal(self, signal_id: UUID) -> list[ArgusSignalReview]:
        result = await self._session.execute(
            select(ArgusSignalReviewModel)
            .where(ArgusSignalReviewModel.signal_id == signal_id)
            .order_by(ArgusSignalReviewModel.created_at.asc())
        )
        return [_argus_review_to_domain(r) for r in result.scalars().all()]
