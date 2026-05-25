"""Map index over PUBLISHED public event pages (Phase 3).

Revision ID: 039
Revises: 038

Materialised geo-index for the public map surface.  One row per
PUBLISHED page that has parseable coordinates in its projection.
Lifecycle is driven by the Phase 9 publication hooks — same shape
as the Phase 2 search index — so the invariant "map contains only
PUBLISHED" is enforced by the use cases that own state.

Design notes
------------

- ``geom`` is a ``GEOGRAPHY(POINT, 4326)`` so distance queries and
  ``ST_Intersects`` work cleanly at any latitude.  We don't need
  metric-projected geometry for Phase 3's bounding-box + grid-
  cluster workload, and GEOGRAPHY keeps the planner honest about
  great-circle math.
- GiST index on ``geom`` is the canonical PostGIS index shape.
- B-tree indexes on the small set of equality-keyable filter facets
  mirror the search index — keeps query plans aligned across the
  two surfaces.
- Confidence band is pre-computed, identical to the search index,
  so map filtering doesn't need to evaluate completeness_score
  thresholds.
- Pages without parseable coordinates are simply absent.  The
  indexer is fail-soft: it logs and skips, rather than failing a
  publish.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "039"
down_revision = "038"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "map_index_entries",
        sa.Column(
            "page_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("public_event_pages.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("slug", sa.String(length=160), nullable=False),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("operator", sa.String(length=300), nullable=True),
        sa.Column("aircraft_type", sa.String(length=300), nullable=True),
        sa.Column("country", sa.String(length=300), nullable=True),
        sa.Column("event_date", sa.Date(), nullable=True),
        sa.Column("fatalities_total", sa.Integer(), nullable=True),
        sa.Column("confidence_band", sa.String(length=10), nullable=False),
        sa.Column(
            "last_published_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "indexed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        # GEOGRAPHY(POINT, 4326): WGS84 lat/lng on the geoid.  We
        # explicitly use GEOGRAPHY rather than GEOMETRY because most
        # of our spatial questions are great-circle ("show me events
        # near this airport") rather than planar.  Cell-bucketed
        # clustering is computed in lat/lng on the application side,
        # so the GEOGRAPHY/GEOMETRY choice doesn't affect that path.
        sa.Column(
            "geom",
            postgresql.BYTEA(),  # placeholder; replaced below
            nullable=False,
        ),
        sa.CheckConstraint(
            "confidence_band IN ('high', 'medium', 'low', 'unknown')",
            name="ck_map_index_entries_confidence_band",
        ),
    )

    # Replace the BYTEA placeholder with the real PostGIS geography
    # column.  Doing it via raw SQL keeps the Alembic column types
    # honest without dragging in a hard dependency on the
    # GeoAlchemy2 package — the application code doesn't need
    # GeoAlchemy2 either; PostGIS expressions go through SQL text or
    # ``func.*``.
    op.execute(
        "ALTER TABLE map_index_entries "
        "ALTER COLUMN geom TYPE geography(Point, 4326) "
        "USING ST_SetSRID(ST_MakePoint(0,0), 4326)::geography"
    )

    # GiST is the canonical spatial index.  We use the GIST operator
    # class explicitly so the index is usable by both bounding-box
    # ``ST_Intersects`` and great-circle ``ST_DWithin`` predicates.
    op.execute("CREATE INDEX ix_map_index_entries_geom ON map_index_entries USING GIST (geom)")

    op.create_index(
        "ix_map_index_entries_operator",
        "map_index_entries",
        ["operator"],
    )
    op.create_index(
        "ix_map_index_entries_aircraft_type",
        "map_index_entries",
        ["aircraft_type"],
    )
    op.create_index(
        "ix_map_index_entries_event_date",
        "map_index_entries",
        ["event_date"],
    )
    # Recency cursor support for the list endpoint.
    op.create_index(
        "ix_map_index_entries_pub_id",
        "map_index_entries",
        [sa.text("last_published_at DESC"), sa.text("page_id DESC")],
    )


def downgrade() -> None:
    op.drop_index("ix_map_index_entries_pub_id", table_name="map_index_entries")
    op.drop_index("ix_map_index_entries_event_date", table_name="map_index_entries")
    op.drop_index(
        "ix_map_index_entries_aircraft_type",
        table_name="map_index_entries",
    )
    op.drop_index("ix_map_index_entries_operator", table_name="map_index_entries")
    op.execute("DROP INDEX IF EXISTS ix_map_index_entries_geom")
    op.drop_table("map_index_entries")
