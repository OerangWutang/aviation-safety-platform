"""Orion v0.1 canonical entity registry and relationship layer.

Revision ID: 028
Revises: 027
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import JSONB

revision = "028"
down_revision = "027"
branch_labels = None
depends_on = None

_ENTITY_TYPES = (
    "AIRCRAFT",
    "OPERATOR",
    "AIRPORT",
    "AIRCRAFT_TYPE",
    "MANUFACTURER",
    "INVESTIGATION_AGENCY",
    "COUNTRY",
)

_REL_TYPES = (
    "INVOLVED_AIRCRAFT",
    "OPERATED_BY",
    "AIRCRAFT_TYPE",
    "MANUFACTURED_BY",
    "OCCURRED_AT",
    "LOCATED_IN",
    "INVESTIGATED_BY",
)

_REVIEW_STATUSES = ("PENDING", "MERGED", "REJECTED", "AUTO_MERGED")
_UUID = postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    op.create_table(
        "orion_entities",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column("entity_type", sa.String(50), nullable=False),
        sa.Column("canonical_name", sa.String(500), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="ACTIVE"),
        sa.Column("confidence", sa.Float, nullable=False, server_default="1.0"),
        sa.Column("merged_into_entity_id", _UUID, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["merged_into_entity_id"], ["orion_entities.id"]),
        sa.CheckConstraint(
            "status IN ('ACTIVE', 'MERGED', 'DEPRECATED')", name="ck_orion_entities_status"
        ),
        sa.CheckConstraint(
            f"entity_type IN ({', '.join(repr(t) for t in _ENTITY_TYPES)})",
            name="ck_orion_entities_entity_type",
        ),
    )
    op.create_index("ix_orion_entities_entity_type", "orion_entities", ["entity_type"])
    op.create_index("ix_orion_entities_canonical_name", "orion_entities", ["canonical_name"])
    op.create_index(
        "ix_orion_entities_merged_into_entity_id", "orion_entities", ["merged_into_entity_id"]
    )

    op.create_table(
        "orion_entity_identifiers",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column("entity_id", _UUID, nullable=False),
        sa.Column("identifier_type", sa.String(100), nullable=False),
        sa.Column("identifier_value", sa.String(500), nullable=False),
        sa.Column("normalized_value", sa.String(500), nullable=False),
        sa.Column("source_claim_id", _UUID, nullable=True),
        sa.Column("raw_snapshot_id", _UUID, nullable=True),
        sa.Column("confidence", sa.Float, nullable=False, server_default="1.0"),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=True),
        sa.Column("valid_to", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["entity_id"], ["orion_entities.id"]),
        sa.ForeignKeyConstraint(["source_claim_id"], ["claims.id"]),
        sa.ForeignKeyConstraint(["raw_snapshot_id"], ["raw_snapshots.id"]),
        sa.UniqueConstraint(
            "entity_id",
            "identifier_type",
            "normalized_value",
            name="uq_orion_entity_identifiers_entity_type_norm",
        ),
    )
    op.create_index(
        "ix_orion_entity_identifiers_entity_id", "orion_entity_identifiers", ["entity_id"]
    )
    op.create_index(
        "ix_orion_entity_identifiers_type_norm",
        "orion_entity_identifiers",
        ["identifier_type", "normalized_value"],
    )

    op.create_table(
        "orion_relationships",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column("subject_entity_id", _UUID, nullable=True),
        sa.Column("relationship_type", sa.String(50), nullable=False),
        sa.Column("object_entity_id", _UUID, nullable=False),
        sa.Column("accident_event_id", _UUID, nullable=False),
        sa.Column("source_claim_id", _UUID, nullable=True),
        sa.Column("raw_snapshot_id", _UUID, nullable=True),
        sa.Column("confidence", sa.Float, nullable=False, server_default="1.0"),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=True),
        sa.Column("valid_to", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["subject_entity_id"], ["orion_entities.id"]),
        sa.ForeignKeyConstraint(["object_entity_id"], ["orion_entities.id"]),
        sa.ForeignKeyConstraint(["accident_event_id"], ["accident_events.id"]),
        sa.ForeignKeyConstraint(["source_claim_id"], ["claims.id"]),
        sa.ForeignKeyConstraint(["raw_snapshot_id"], ["raw_snapshots.id"]),
        sa.CheckConstraint(
            f"relationship_type IN ({', '.join(repr(t) for t in _REL_TYPES)})",
            name="ck_orion_relationships_type",
        ),
        sa.CheckConstraint(
            "accident_event_id IS NOT NULL",
            name="ck_orion_relationships_event_required",
        ),
    )
    op.create_index(
        "ix_orion_relationships_subject_entity_id", "orion_relationships", ["subject_entity_id"]
    )
    op.create_index(
        "ix_orion_relationships_object_entity_id", "orion_relationships", ["object_entity_id"]
    )
    op.create_index(
        "ix_orion_relationships_accident_event_id", "orion_relationships", ["accident_event_id"]
    )
    op.create_index(
        "ix_orion_relationships_relationship_type", "orion_relationships", ["relationship_type"]
    )
    op.create_index(
        "uq_orion_relationships_event_level",
        "orion_relationships",
        ["relationship_type", "object_entity_id", "accident_event_id"],
        unique=True,
        postgresql_where=sa.text("subject_entity_id IS NULL"),
    )
    op.create_index(
        "uq_orion_relationships_entity_level",
        "orion_relationships",
        ["subject_entity_id", "relationship_type", "object_entity_id", "accident_event_id"],
        unique=True,
        postgresql_where=sa.text("subject_entity_id IS NOT NULL"),
    )

    op.create_table(
        "orion_entity_claim_links",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column("entity_id", _UUID, nullable=False),
        sa.Column("claim_id", _UUID, nullable=False),
        sa.Column("raw_snapshot_id", _UUID, nullable=True),
        sa.Column("source_id", _UUID, nullable=False),
        sa.Column("accident_event_id", _UUID, nullable=False),
        sa.Column("confidence", sa.Float, nullable=False, server_default="1.0"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["entity_id"], ["orion_entities.id"]),
        sa.ForeignKeyConstraint(["claim_id"], ["claims.id"]),
        sa.ForeignKeyConstraint(["raw_snapshot_id"], ["raw_snapshots.id"]),
        sa.ForeignKeyConstraint(["source_id"], ["sources.id"]),
        sa.ForeignKeyConstraint(["accident_event_id"], ["accident_events.id"]),
        sa.UniqueConstraint(
            "entity_id",
            "claim_id",
            "accident_event_id",
            name="uq_orion_entity_claim_links_entity_claim_event",
        ),
    )
    op.create_index(
        "ix_orion_entity_claim_links_entity_id", "orion_entity_claim_links", ["entity_id"]
    )
    op.create_index(
        "ix_orion_entity_claim_links_claim_id", "orion_entity_claim_links", ["claim_id"]
    )
    op.create_index(
        "ix_orion_entity_claim_links_accident_event_id",
        "orion_entity_claim_links",
        ["accident_event_id"],
    )

    op.create_table(
        "orion_entity_reviews",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column("candidate_entity_id_a", _UUID, nullable=False),
        sa.Column("candidate_entity_id_b", _UUID, nullable=False),
        sa.Column("entity_type", sa.String(50), nullable=False),
        sa.Column("match_score", sa.Float, nullable=False),
        sa.Column("matched_identifiers", JSONB, nullable=False, server_default="[]"),
        sa.Column("status", sa.String(20), nullable=False, server_default="PENDING"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_by", _UUID, nullable=True),
        sa.Column("resolution_note", sa.String(1000), nullable=True),
        sa.ForeignKeyConstraint(["candidate_entity_id_a"], ["orion_entities.id"]),
        sa.ForeignKeyConstraint(["candidate_entity_id_b"], ["orion_entities.id"]),
        sa.CheckConstraint(
            f"status IN ({', '.join(repr(s) for s in _REVIEW_STATUSES)})",
            name="ck_orion_entity_reviews_status",
        ),
        sa.CheckConstraint(
            f"entity_type IN ({', '.join(repr(t) for t in _ENTITY_TYPES)})",
            name="ck_orion_entity_reviews_entity_type",
        ),
    )
    op.create_index("ix_orion_entity_reviews_status", "orion_entity_reviews", ["status"])
    op.create_index("ix_orion_entity_reviews_entity_type", "orion_entity_reviews", ["entity_type"])
    op.execute(
        """
        CREATE UNIQUE INDEX uq_orion_entity_reviews_pending_pair
        ON orion_entity_reviews (
            LEAST(candidate_entity_id_a::text, candidate_entity_id_b::text),
            GREATEST(candidate_entity_id_a::text, candidate_entity_id_b::text)
        )
        WHERE status = 'PENDING'
        """
    )


def downgrade() -> None:
    op.drop_index("uq_orion_entity_reviews_pending_pair", table_name="orion_entity_reviews")
    op.drop_table("orion_entity_reviews")
    op.drop_table("orion_entity_claim_links")
    op.drop_table("orion_relationships")
    op.drop_table("orion_entity_identifiers")
    op.drop_table("orion_entities")
