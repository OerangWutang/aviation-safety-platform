"""SQLAlchemy repositories for the orion aggregate.

Carved out of the old ``repositories.py`` monolith in r9; behaviour
unchanged.  Public ``Sql*`` classes are re-exported from
``atlas.infrastructure.db.repositories`` so existing imports keep
working.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from atlas.domain.entities import (
    OrionEntity,
    OrionEntityClaimLink,
    OrionEntityIdentifier,
    OrionEntityReview,
    OrionRelationship,
)
from atlas.domain.enums import (
    OrionEntityType,
    OrionReviewStatus,
)
from atlas.domain.exceptions import ConcurrentUpsertError, MappingError
from atlas.domain.interfaces.repositories import (
    OrionEntityClaimLinkRepository,
    OrionEntityRepository,
    OrionEntityReviewRepository,
    OrionIdentifierRepository,
    OrionRelationshipRepository,
)
from atlas.infrastructure.db.orm_models import (
    OrionEntityClaimLinkModel,
    OrionEntityIdentifierModel,
    OrionEntityModel,
    OrionEntityReviewModel,
    OrionRelationshipModel,
)
from atlas.infrastructure.db.repositories._helpers import (
    ADVISORY_LOCK_ORION_IDENTIFIER,
    _domain_data,
    _to_domain,
    _to_domain_opt,
)


class SqlOrionEntityRepository(OrionEntityRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, id: UUID) -> OrionEntity | None:
        return _to_domain_opt(await self._session.get(OrionEntityModel, id), OrionEntity)

    async def add(self, entity: OrionEntity) -> None:
        self._session.add(OrionEntityModel(**_domain_data(entity)))

    async def lock_for_identifier_identity(
        self,
        entity_type: OrionEntityType,
        identifier_type: str,
        normalized_value: str,
    ) -> None:
        """Serialize strong-identifier entity resolution for this transaction."""
        key = f"{entity_type.value}:{identifier_type}:{normalized_value}"
        await self._session.execute(
            text("SELECT pg_advisory_xact_lock(CAST(:namespace AS integer), hashtext(:k))"),
            {"namespace": ADVISORY_LOCK_ORION_IDENTIFIER, "k": key},
        )

    async def save(self, entity: OrionEntity) -> None:
        row = await self._session.get(OrionEntityModel, entity.id)
        if row is None:
            await self.add(entity)
            return
        row.canonical_name = entity.canonical_name
        row.status = entity.status
        row.confidence = entity.confidence
        row.merged_into_entity_id = entity.merged_into_entity_id
        row.updated_at = datetime.now(UTC)

    async def find_by_identifier(
        self,
        entity_type: OrionEntityType,
        identifier_type: str,
        normalized_value: str,
    ) -> OrionEntity | None:
        """Return the entity that currently owns the given identifier.

        Only *active* identifiers (``valid_to IS NULL``) are considered.
        Historical identifiers (``valid_to`` set) represent past ownership
        and must not resolve to an entity — migration 034 allows multiple
        historical rows with the same triple, so looking through them would
        be ambiguous and would attach new extractions to stale entities.
        """
        from sqlalchemy import and_

        stmt = (
            select(OrionEntityModel)
            .join(
                OrionEntityIdentifierModel,
                OrionEntityIdentifierModel.entity_id == OrionEntityModel.id,
            )
            .where(
                and_(
                    OrionEntityModel.entity_type == entity_type.value,
                    OrionEntityModel.status == "ACTIVE",
                    OrionEntityIdentifierModel.identifier_type == identifier_type,
                    OrionEntityIdentifierModel.normalized_value == normalized_value,
                    OrionEntityIdentifierModel.valid_to.is_(None),  # active only
                )
            )
            .limit(1)
        )
        result = await self._session.execute(stmt)
        return _to_domain_opt(result.scalar_one_or_none(), OrionEntity)

    async def find_by_canonical_name(
        self,
        entity_type: OrionEntityType,
        normalized_name: str,
    ) -> OrionEntity | None:
        stmt = (
            select(OrionEntityModel)
            .where(
                OrionEntityModel.entity_type == entity_type.value,
                OrionEntityModel.status == "ACTIVE",
                func.lower(OrionEntityModel.canonical_name) == normalized_name,
            )
            .limit(1)
        )
        result = await self._session.execute(stmt)
        return _to_domain_opt(result.scalar_one_or_none(), OrionEntity)

    async def search(
        self,
        query: str,
        entity_type: OrionEntityType | None = None,
        limit: int = 25,
    ) -> list[OrionEntity]:
        from sqlalchemy import or_, union

        q_lower = query.lower()
        name_conditions = [
            OrionEntityModel.status == "ACTIVE",
            func.lower(OrionEntityModel.canonical_name).contains(q_lower),
        ]
        if entity_type is not None:
            name_conditions.append(OrionEntityModel.entity_type == entity_type.value)
        name_stmt = select(OrionEntityModel.id).where(*name_conditions)

        ident_conditions = [
            OrionEntityModel.status == "ACTIVE",
            or_(
                func.lower(OrionEntityIdentifierModel.identifier_value).contains(q_lower),
                OrionEntityIdentifierModel.normalized_value.contains(q_lower),
            ),
        ]
        if entity_type is not None:
            ident_conditions.append(OrionEntityModel.entity_type == entity_type.value)
        ident_stmt = (
            select(OrionEntityModel.id)
            .join(
                OrionEntityIdentifierModel,
                OrionEntityIdentifierModel.entity_id == OrionEntityModel.id,
            )
            .where(*ident_conditions)
        )

        combined_ids_subq = union(name_stmt, ident_stmt).subquery()
        final_stmt = (
            select(OrionEntityModel)
            .where(OrionEntityModel.id.in_(select(combined_ids_subq.c.id)))
            .limit(limit)
        )
        result = await self._session.execute(final_stmt)
        return [_to_domain(obj, OrionEntity) for obj in result.scalars()]

    async def list_for_event(self, event_id: UUID) -> list[OrionEntity]:
        from sqlalchemy import or_

        stmt = (
            select(OrionEntityModel)
            .join(
                OrionRelationshipModel,
                or_(
                    OrionRelationshipModel.subject_entity_id == OrionEntityModel.id,
                    OrionRelationshipModel.object_entity_id == OrionEntityModel.id,
                ),
            )
            .where(OrionRelationshipModel.accident_event_id == event_id)
            .distinct()
        )
        result = await self._session.execute(stmt)
        return [_to_domain(obj, OrionEntity) for obj in result.scalars()]


class SqlOrionIdentifierRepository(OrionIdentifierRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def _data_with_entity_type(self, identifier: OrionEntityIdentifier) -> dict[str, Any]:
        data = _domain_data(identifier)
        if data.get("entity_type") is None:
            entity_type = await self._session.scalar(
                select(OrionEntityModel.entity_type).where(
                    OrionEntityModel.id == identifier.entity_id
                )
            )
            if entity_type is None:
                raise MappingError(
                    f"Cannot add Orion identifier for missing entity {identifier.entity_id}"
                )
            data["entity_type"] = entity_type
        return data

    async def add(self, identifier: OrionEntityIdentifier) -> None:
        self._session.add(
            OrionEntityIdentifierModel(**(await self._data_with_entity_type(identifier)))
        )

    async def try_add(self, identifier: OrionEntityIdentifier) -> bool:
        data = await self._data_with_entity_type(identifier)
        stmt = (
            insert(OrionEntityIdentifierModel)
            .values(**data)
            .on_conflict_do_nothing(
                index_elements=["entity_type", "identifier_type", "normalized_value"],
                index_where=text("valid_to IS NULL"),
            )
            .returning(OrionEntityIdentifierModel.id)
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none() is not None

    async def list_for_entity(self, entity_id: UUID) -> list[OrionEntityIdentifier]:
        result = await self._session.execute(
            select(OrionEntityIdentifierModel).where(
                OrionEntityIdentifierModel.entity_id == entity_id
            )
        )
        return [_to_domain(obj, OrionEntityIdentifier) for obj in result.scalars()]


class SqlOrionRelationshipRepository(OrionRelationshipRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, relationship: OrionRelationship) -> None:
        self._session.add(OrionRelationshipModel(**_domain_data(relationship)))

    async def upsert_relationship(
        self, relationship: OrionRelationship
    ) -> tuple[OrionRelationship, bool]:
        """Insert an Orion relationship idempotently against the two partial
        unique indexes.

        ``orion_relationships`` carries two partial indexes:
        - ``uq_orion_relationships_event_level`` (WHERE subject_entity_id IS NULL)
          on ``(relationship_type, object_entity_id, accident_event_id)``
        - ``uq_orion_relationships_entity_level`` (WHERE subject_entity_id IS NOT NULL)
          on ``(subject_entity_id, relationship_type, object_entity_id, accident_event_id)``

        SQLAlchemy's ``on_conflict_do_nothing(index_where=...)`` targets the
        correct partial index depending on whether ``subject_entity_id`` is
        NULL, making both paths race-safe under concurrent extractors.
        """
        data = _domain_data(relationship)
        if relationship.subject_entity_id is None:
            stmt = (
                insert(OrionRelationshipModel)
                .values(**data)
                .on_conflict_do_nothing(
                    index_elements=["relationship_type", "object_entity_id", "accident_event_id"],
                    index_where=OrionRelationshipModel.subject_entity_id.is_(None),
                )
                .returning(OrionRelationshipModel)
            )
        else:
            stmt = (
                insert(OrionRelationshipModel)
                .values(**data)
                .on_conflict_do_nothing(
                    index_elements=[
                        "subject_entity_id",
                        "relationship_type",
                        "object_entity_id",
                        "accident_event_id",
                    ],
                    index_where=OrionRelationshipModel.subject_entity_id.isnot(None),
                )
                .returning(OrionRelationshipModel)
            )
        result = await self._session.execute(stmt)
        row = result.scalar_one_or_none()
        if row is not None:
            return _to_domain(row, OrionRelationship), True
        # DO NOTHING branch — re-select the pre-existing row.
        if relationship.subject_entity_id is None:
            re_stmt = select(OrionRelationshipModel).where(
                OrionRelationshipModel.subject_entity_id.is_(None),
                OrionRelationshipModel.relationship_type == relationship.relationship_type.value,
                OrionRelationshipModel.object_entity_id == relationship.object_entity_id,
                OrionRelationshipModel.accident_event_id == relationship.accident_event_id,
            )
        else:
            re_stmt = select(OrionRelationshipModel).where(
                OrionRelationshipModel.subject_entity_id == relationship.subject_entity_id,
                OrionRelationshipModel.relationship_type == relationship.relationship_type.value,
                OrionRelationshipModel.object_entity_id == relationship.object_entity_id,
                OrionRelationshipModel.accident_event_id == relationship.accident_event_id,
            )
        existing = await self._session.execute(re_stmt)
        row = existing.scalar_one_or_none()
        if row is not None:
            return _to_domain(row, OrionRelationship), False
        # ON CONFLICT fired but the re-select returned nothing — indicates a
        # race with a concurrent DELETE, a partial-index discrepancy, or a
        # session-visibility bug.  Fail loudly rather than returning an
        # unpersisted object that the caller would treat as a real DB row.
        raise ConcurrentUpsertError(
            f"OrionRelationship upsert: ON CONFLICT fired for relationship "
            f"({relationship.subject_entity_id!r}, {relationship.relationship_type!r}, "
            f"{relationship.object_entity_id!r}) on event {relationship.accident_event_id} "
            "but re-select found no existing row."
        )

    async def list_for_entity(self, entity_id: UUID) -> list[OrionRelationship]:
        from sqlalchemy import or_

        result = await self._session.execute(
            select(OrionRelationshipModel).where(
                or_(
                    OrionRelationshipModel.subject_entity_id == entity_id,
                    OrionRelationshipModel.object_entity_id == entity_id,
                )
            )
        )
        return [_to_domain(obj, OrionRelationship) for obj in result.scalars()]

    async def list_for_event(self, event_id: UUID) -> list[OrionRelationship]:
        result = await self._session.execute(
            select(OrionRelationshipModel).where(
                OrionRelationshipModel.accident_event_id == event_id
            )
        )
        return [_to_domain(obj, OrionRelationship) for obj in result.scalars()]


class SqlOrionEntityClaimLinkRepository(OrionEntityClaimLinkRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, link: OrionEntityClaimLink) -> None:
        stmt = (
            insert(OrionEntityClaimLinkModel)
            .values(**_domain_data(link))
            .on_conflict_do_nothing(constraint="uq_orion_entity_claim_links_entity_claim_event")
        )
        await self._session.execute(stmt)

    async def list_for_entity(self, entity_id: UUID) -> list[OrionEntityClaimLink]:
        result = await self._session.execute(
            select(OrionEntityClaimLinkModel).where(
                OrionEntityClaimLinkModel.entity_id == entity_id
            )
        )
        return [_to_domain(obj, OrionEntityClaimLink) for obj in result.scalars()]


class SqlOrionEntityReviewRepository(OrionEntityReviewRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, review: OrionEntityReview) -> None:
        self._session.add(OrionEntityReviewModel(**_domain_data(review)))

    async def list_pending(self, limit: int = 50, offset: int = 0) -> list[OrionEntityReview]:
        stmt = (
            select(OrionEntityReviewModel)
            .where(OrionEntityReviewModel.status == "PENDING")
            .order_by(OrionEntityReviewModel.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        result = await self._session.execute(stmt)
        return [_to_domain(obj, OrionEntityReview) for obj in result.scalars()]

    async def mark_merged(
        self, review_id: UUID, resolved_by: UUID, note: str | None = None
    ) -> None:
        row = await self._session.get(OrionEntityReviewModel, review_id)
        if row:
            row.status = OrionReviewStatus.MERGED.value
            row.resolved_at = datetime.now(UTC)
            row.resolved_by = resolved_by
            row.resolution_note = note

    async def mark_rejected(
        self, review_id: UUID, resolved_by: UUID, note: str | None = None
    ) -> None:
        row = await self._session.get(OrionEntityReviewModel, review_id)
        if row:
            row.status = OrionReviewStatus.REJECTED.value
            row.resolved_at = datetime.now(UTC)
            row.resolved_by = resolved_by
            row.resolution_note = note


# ── Chronos SQL Repositories ──────────────────────────────────────────────────
