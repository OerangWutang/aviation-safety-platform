"""Fake Orion entity/identifier/relationship/review repositories."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

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
from tests.domain.fakes._store import (
    _OrionStore,
)


class FakeOrionEntityRepository:
    def __init__(self, store: _OrionStore) -> None:
        self._s = store

    async def get(self, id: UUID) -> OrionEntity | None:
        return self._s.entities.get(id)

    async def add(self, entity: OrionEntity) -> None:
        self._s.entities[entity.id] = entity

    async def lock_for_identifier_identity(
        self,
        entity_type: OrionEntityType,
        identifier_type: str,
        normalized_value: str,
    ) -> None:
        return None

    async def save(self, entity: OrionEntity) -> None:
        self._s.entities[entity.id] = entity

    async def find_by_identifier(
        self,
        entity_type: OrionEntityType,
        identifier_type: str,
        normalized_value: str,
    ) -> OrionEntity | None:
        """Return the entity that currently owns the given identifier.

        Only active identifiers (``valid_to is None``) are matched,
        mirroring the SQL implementation which filters ``valid_to IS NULL``.
        Historical identifiers (valid_to set) represent past ownership and
        must not resolve — the partial unique index allows multiple historical
        rows for the same triple, so resolving through them would be
        ambiguous.
        """
        for ident in self._s.identifiers:
            if (
                ident.identifier_type == identifier_type
                and ident.normalized_value == normalized_value
                and ident.valid_to is None  # active only
            ):
                entity = self._s.entities.get(ident.entity_id)
                if entity and entity.entity_type == entity_type and entity.status == "ACTIVE":
                    return entity
        return None

    async def find_by_canonical_name(
        self, entity_type: OrionEntityType, normalized_name: str
    ) -> OrionEntity | None:
        from atlas.domain.services.orion_normalizers import normalize_name

        for entity in self._s.entities.values():
            if (
                entity.entity_type == entity_type
                and entity.status == "ACTIVE"
                and normalize_name(entity.canonical_name) == normalized_name
            ):
                return entity
        return None

    async def search(
        self, query: str, entity_type: OrionEntityType | None = None, limit: int = 25
    ) -> list[OrionEntity]:
        q = query.lower()
        results: list[OrionEntity] = []
        seen: set[UUID] = set()
        for entity in self._s.entities.values():
            if entity.status != "ACTIVE":
                continue
            if entity_type is not None and entity.entity_type != entity_type:
                continue
            name_match = q in entity.canonical_name.lower()
            ident_match = any(
                i.entity_id == entity.id
                and (q in i.identifier_value.lower() or q in i.normalized_value.lower())
                for i in self._s.identifiers
            )
            if (name_match or ident_match) and entity.id not in seen:
                seen.add(entity.id)
                results.append(entity)
        return results[:limit]

    async def list_for_event(self, event_id: UUID) -> list[OrionEntity]:
        entity_ids = {
            r.subject_entity_id
            for r in self._s.relationships
            if r.accident_event_id == event_id and r.subject_entity_id
        } | {r.object_entity_id for r in self._s.relationships if r.accident_event_id == event_id}
        return [entity for entity_id, entity in self._s.entities.items() if entity_id in entity_ids]


class FakeOrionIdentifierRepository:
    def __init__(self, store: _OrionStore) -> None:
        self._s = store

    def _with_entity_type(self, identifier: OrionEntityIdentifier) -> OrionEntityIdentifier:
        if identifier.entity_type is not None:
            return identifier
        entity = self._s.entities.get(identifier.entity_id)
        if entity is None:
            return identifier
        return identifier.model_copy(update={"entity_type": entity.entity_type})

    async def add(self, identifier: OrionEntityIdentifier) -> None:
        self._s.identifiers.append(self._with_entity_type(identifier))

    async def try_add(self, identifier: OrionEntityIdentifier) -> bool:
        identifier = self._with_entity_type(identifier)
        for existing in self._s.identifiers:
            if (
                existing.entity_id == identifier.entity_id
                and existing.identifier_type == identifier.identifier_type
                and existing.normalized_value == identifier.normalized_value
            ):
                return False
            if (
                existing.valid_to is None
                and identifier.valid_to is None
                and existing.entity_type == identifier.entity_type
                and existing.identifier_type == identifier.identifier_type
                and existing.normalized_value == identifier.normalized_value
            ):
                return False
        self._s.identifiers.append(identifier)
        return True

    async def list_for_entity(self, entity_id: UUID) -> list[OrionEntityIdentifier]:
        return [
            identifier for identifier in self._s.identifiers if identifier.entity_id == entity_id
        ]


class FakeOrionRelationshipRepository:
    def __init__(self, store: _OrionStore) -> None:
        self._s = store

    async def add(self, relationship: OrionRelationship) -> None:
        self._s.relationships.append(relationship)

    async def upsert_relationship(
        self, relationship: OrionRelationship
    ) -> tuple[OrionRelationship, bool]:
        for existing in self._s.relationships:
            if (
                existing.subject_entity_id == relationship.subject_entity_id
                and existing.relationship_type == relationship.relationship_type
                and existing.object_entity_id == relationship.object_entity_id
                and existing.accident_event_id == relationship.accident_event_id
            ):
                return existing, False
        self._s.relationships.append(relationship)
        return relationship, True

    async def list_for_entity(self, entity_id: UUID) -> list[OrionRelationship]:
        return [
            relationship
            for relationship in self._s.relationships
            if relationship.subject_entity_id == entity_id
            or relationship.object_entity_id == entity_id
        ]

    async def list_for_event(self, event_id: UUID) -> list[OrionRelationship]:
        return [
            relationship
            for relationship in self._s.relationships
            if relationship.accident_event_id == event_id
        ]


class FakeOrionEntityClaimLinkRepository:
    def __init__(self, store: _OrionStore) -> None:
        self._s = store

    async def add(self, link: OrionEntityClaimLink) -> None:
        if not any(
            existing.entity_id == link.entity_id
            and existing.claim_id == link.claim_id
            and existing.accident_event_id == link.accident_event_id
            for existing in self._s.claim_links
        ):
            self._s.claim_links.append(link)

    async def list_for_entity(self, entity_id: UUID) -> list[OrionEntityClaimLink]:
        return [link for link in self._s.claim_links if link.entity_id == entity_id]


class FakeOrionEntityReviewRepository:
    def __init__(self, store: _OrionStore) -> None:
        self._s = store

    async def add(self, review: OrionEntityReview) -> None:
        self._s.reviews.append(review)

    async def list_pending(self, limit: int = 50, offset: int = 0) -> list[OrionEntityReview]:
        pending = [
            review for review in self._s.reviews if review.status == OrionReviewStatus.PENDING
        ]
        return pending[offset : offset + limit]

    async def mark_merged(
        self, review_id: UUID, resolved_by: UUID, note: str | None = None
    ) -> None:
        for review in self._s.reviews:
            if review.id == review_id:
                review.status = OrionReviewStatus.MERGED
                review.resolved_at = datetime.now(UTC)
                review.resolved_by = resolved_by
                review.resolution_note = note
                break

    async def mark_rejected(
        self, review_id: UUID, resolved_by: UUID, note: str | None = None
    ) -> None:
        for review in self._s.reviews:
            if review.id == review_id:
                review.status = OrionReviewStatus.REJECTED
                review.resolved_at = datetime.now(UTC)
                review.resolved_by = resolved_by
                review.resolution_note = note
                break


# ── Chronos Fake Repositories ─────────────────────────────────────────────────
