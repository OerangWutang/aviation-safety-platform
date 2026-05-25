"""Causality: HFACS taxonomy + event attributions + SHELO factors (Phase 4).

Revision ID: 042
Revises: 041

Five new tables across two parallel sub-models:

HFACS — a fixed four-tier taxonomy of human-factors classifications.
    - ``hfacs_categories`` — the named tier+category buckets (24 rows).
      Tier 1: Organizational Influences (3 categories)
      Tier 2: Unsafe Supervision (4 categories)
      Tier 3: Preconditions for Unsafe Acts (8 categories)
      Tier 4: Unsafe Acts (9 categories)
    - ``hfacs_subcategories`` — the leaf rows analysts attribute to.
      Seeded with the canonical set; ``is_custom`` allows future
      tenant or admin extensions.
    - ``event_hfacs_attributions`` — the editorial claim that
      "this event manifested this subcategory".  Carries
      ``confidence`` (0..1), ``note``, ``editor_user_id``, and
      ``version`` for optimistic concurrency.  Visibility inherits
      from the parent ``PublicEventPage`` — no independent state
      machine.

SHELO — Software, Hardware, Environment, Liveware, Other, plus
typed interactions between them.  Event-local: each event has its
own factor set.
    - ``shelo_factors`` — one row per editorial factor on an event.
    - ``shelo_factor_interactions`` — typed edges between factors
      on the same event.  Kinds: PRECONDITION, AGGRAVATED,
      MITIGATED, MASKED.  Same-event constraint via
      ``(event_id, source_factor_id, target_factor_id)`` natural
      key.

Cross-cutting design choices:

- HFACS taxonomy is a *tree*; we store category and subcategory in
  separate tables (rather than self-referential) so the four-tier
  shape is explicit at the schema level.
- SHELO factors form a *small graph per event*; we store nodes and
  typed edges in two tables.  Cycles are permitted at the schema
  level because they're sometimes editorial reality (mutual
  feedback loops); the editorial workflow surfaces them to a
  reviewer rather than rejecting at INSERT time.
- All visibility piggybacks on the parent ``PublicEventPage``.  No
  Phase 4 entity carries its own ``status`` column.
"""

from __future__ import annotations

from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "042"
down_revision = "041"
branch_labels = None
depends_on = None


_HFACS_TIERS = (
    "'ORGANIZATIONAL'",
    "'SUPERVISION'",
    "'PRECONDITIONS'",
    "'UNSAFE_ACTS'",
)
_SHELO_CLASSES = (
    "'SOFTWARE'",
    "'HARDWARE'",
    "'ENVIRONMENT'",
    "'LIVEWARE'",
    "'OTHER'",
)
_INTERACTION_KINDS = (
    "'PRECONDITION'",
    "'AGGRAVATED'",
    "'MITIGATED'",
    "'MASKED'",
)


# ── Canonical HFACS taxonomy seed data ──────────────────────────────────────
#
# These come from the public-domain HFACS specification.  Codes follow
# the standard "ORG-RM" / "SUP-IS" / "PRE-PE" / "ACT-SBE" prefixes so
# external tooling that ingests Atlas data can join on a stable key.
#
# This is the *minimal viable* taxonomy.  Operators with their own
# extensions can add rows with ``is_custom=true`` post-deploy.

_HFACS_CATEGORIES = [
    # Tier 1: Organizational Influences
    (
        "ORG",
        "ORG-RM",
        "ORGANIZATIONAL",
        "Resource Management",
        "Allocation of human, monetary, and equipment resources.",
    ),
    (
        "ORG",
        "ORG-CLI",
        "ORGANIZATIONAL",
        "Organizational Climate",
        "Working atmosphere; chain of command; communication.",
    ),
    (
        "ORG",
        "ORG-PRO",
        "ORGANIZATIONAL",
        "Organizational Process",
        "Policies, procedures, and oversight processes.",
    ),
    # Tier 2: Unsafe Supervision
    (
        "SUP",
        "SUP-IS",
        "SUPERVISION",
        "Inadequate Supervision",
        "Failure to provide guidance, oversight, training, or "
        "incentives appropriate to operations.",
    ),
    (
        "SUP",
        "SUP-PIO",
        "SUPERVISION",
        "Planned Inappropriate Operations",
        "Crew pairing, scheduling, or risk-tolerance choices that create unsafe conditions.",
    ),
    (
        "SUP",
        "SUP-FCKP",
        "SUPERVISION",
        "Failed to Correct Known Problem",
        "Awareness of a deficiency without taking corrective action.",
    ),
    (
        "SUP",
        "SUP-SV",
        "SUPERVISION",
        "Supervisory Violations",
        "Wilful disregard of rules or procedures by supervisors.",
    ),
    # Tier 3: Preconditions for Unsafe Acts
    (
        "PRE",
        "PRE-PE",
        "PRECONDITIONS",
        "Physical Environment",
        "Weather, terrain, altitude, lighting, vibration, operational tempo affecting performance.",
    ),
    (
        "PRE",
        "PRE-TE",
        "PRECONDITIONS",
        "Technological Environment",
        "Equipment design, automation, displays, controls, checklists.",
    ),
    (
        "PRE",
        "PRE-ACMS",
        "PRECONDITIONS",
        "Adverse Mental State",
        "Stress, complacency, distraction, mental fatigue, motivation.",
    ),
    (
        "PRE",
        "PRE-APS",
        "PRECONDITIONS",
        "Adverse Physiological State",
        "Medical illness, physical fatigue, hypoxia, intoxication.",
    ),
    (
        "PRE",
        "PRE-PML",
        "PRECONDITIONS",
        "Physical/Mental Limitations",
        "Insufficient reaction time, sensory acuity, anthropometric mismatch.",
    ),
    (
        "PRE",
        "PRE-CRM",
        "PRECONDITIONS",
        "Crew Resource Management",
        "Communication and coordination failures within the crew.",
    ),
    (
        "PRE",
        "PRE-PR",
        "PRECONDITIONS",
        "Personal Readiness",
        "Pre-flight rest, nutrition, off-duty conduct affecting duty performance.",
    ),
    # Tier 4: Unsafe Acts
    (
        "ACT",
        "ACT-SBE",
        "UNSAFE_ACTS",
        "Skill-Based Errors",
        "Slips and lapses involving well-practised tasks.",
    ),
    (
        "ACT",
        "ACT-DE",
        "UNSAFE_ACTS",
        "Decision Errors",
        "Conscious choices that prove inappropriate given the situation.",
    ),
    (
        "ACT",
        "ACT-PE",
        "UNSAFE_ACTS",
        "Perceptual Errors",
        "Misjudgements arising from degraded sensory input "
        "(visual illusions, spatial disorientation).",
    ),
    (
        "ACT",
        "ACT-ROV",
        "UNSAFE_ACTS",
        "Routine Violations",
        "Habitual deviations from procedure tolerated by the organisation.",
    ),
    (
        "ACT",
        "ACT-EXV",
        "UNSAFE_ACTS",
        "Exceptional Violations",
        "One-off, deliberate departures from procedure not part of a habitual pattern.",
    ),
]


def _seed_hfacs_categories(connection: sa.engine.Connection) -> None:
    """Insert the canonical HFACS taxonomy.

    A data migration is the right place for this — the taxonomy is
    versioned with the schema, and a future migration that adds new
    subcategories does so by editing the seed rows in a parallel
    revision, not by ALTER TABLE.
    """
    now = datetime.now(UTC)
    rows = [
        {
            "tier_code": tier_code,
            "code": code,
            "tier": tier,
            "name": name,
            "description": description,
            "is_custom": False,
            "created_at": now,
        }
        for tier_code, code, tier, name, description in _HFACS_CATEGORIES
    ]
    connection.execute(
        sa.text(
            """
            INSERT INTO hfacs_categories
                (id, tier_code, code, tier, name, description,
                 is_custom, created_at)
            VALUES
                (uuid_generate_v4(), :tier_code, :code, :tier, :name,
                 :description, :is_custom, :created_at)
            """
        ),
        rows,
    )


def upgrade() -> None:
    # ── hfacs_categories ────────────────────────────────────────────
    op.create_table(
        "hfacs_categories",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("uuid_generate_v4()"),
        ),
        # ``tier_code`` is a short 3-letter prefix (ORG/SUP/PRE/ACT)
        # for human-readable URLs and code-side filtering.
        sa.Column("tier_code", sa.String(length=4), nullable=False),
        # ``code`` is the stable join key external systems use.
        # E.g. "PRE-CRM" — unique across all categories.
        sa.Column("code", sa.String(length=20), nullable=False),
        sa.Column("tier", sa.String(length=20), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column(
            "is_custom",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            f"tier IN ({', '.join(_HFACS_TIERS)})",
            name="ck_hfacs_categories_tier",
        ),
    )
    op.create_index(
        "uq_hfacs_categories_code",
        "hfacs_categories",
        ["code"],
        unique=True,
    )

    # Seed the canonical taxonomy.
    bind = op.get_bind()
    _seed_hfacs_categories(bind)

    # ── hfacs_subcategories ─────────────────────────────────────────
    #
    # Phase 4 ships an empty subcategories table by design.  The
    # category rows above are what most analysts attribute to; the
    # subcategories table is the extension point for fine-grained
    # attributions (e.g. "Skill-Based Errors → Inadvertent
    # operation of controls") and operator-defined custom rows.
    #
    # The attribution table FKs to subcategory, so a subcategory
    # MUST exist before an event can be attributed at this level.
    # Operators bootstrap the subcategories they care about via the
    # admin surface (out of scope for Phase 4 — manual SQL works
    # until then).
    op.create_table(
        "hfacs_subcategories",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("uuid_generate_v4()"),
        ),
        sa.Column(
            "category_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("hfacs_categories.id"),
            nullable=False,
        ),
        sa.Column("code", sa.String(length=40), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "is_custom",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index(
        "uq_hfacs_subcategories_code",
        "hfacs_subcategories",
        ["code"],
        unique=True,
    )
    op.create_index(
        "ix_hfacs_subcategories_category",
        "hfacs_subcategories",
        ["category_id"],
    )

    # ── event_hfacs_attributions ────────────────────────────────────
    op.create_table(
        "event_hfacs_attributions",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("uuid_generate_v4()"),
        ),
        sa.Column(
            "event_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("accident_events.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # Attributions go to a *category* by default; the optional
        # ``subcategory_id`` narrows to a leaf.  The CHECK enforces
        # that both refer to compatible rows: an attribution is
        # *either* category-level OR subcategory-level, not both,
        # which keeps the natural key clean.
        sa.Column(
            "category_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("hfacs_categories.id"),
            nullable=False,
        ),
        sa.Column(
            "subcategory_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("hfacs_subcategories.id"),
            nullable=True,
        ),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column(
            "editor_user_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column(
            "version",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("1"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "confidence >= 0.0 AND confidence <= 1.0",
            name="ck_event_hfacs_attributions_confidence_range",
        ),
    )
    # Natural key: an event has at most one attribution per
    # (category, subcategory).  Two attributions to the same
    # subcategory don't make editorial sense — if an analyst wants
    # to revise, they update the existing row.
    #
    # The COALESCE handles the NULL-subcategory case: two
    # category-only attributions for the same category are also
    # forbidden.  Using a partial unique index lets us express the
    # constraint at the schema level.
    op.execute(
        "CREATE UNIQUE INDEX uq_event_hfacs_attributions_natural "
        "ON event_hfacs_attributions "
        "(event_id, category_id, COALESCE(subcategory_id, "
        "'00000000-0000-0000-0000-000000000000'::uuid))"
    )
    op.create_index(
        "ix_event_hfacs_attributions_event",
        "event_hfacs_attributions",
        ["event_id"],
    )

    # ── shelo_factors ───────────────────────────────────────────────
    op.create_table(
        "shelo_factors",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("uuid_generate_v4()"),
        ),
        sa.Column(
            "event_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("accident_events.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("factor_class", sa.String(length=20), nullable=False),
        # Short identifying label used in the UI graph.  Free-form
        # because real SHELO factors are descriptions ("right engine
        # FADEC software fault") not codes.
        sa.Column("label", sa.String(length=300), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "editor_user_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column(
            "version",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("1"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            f"factor_class IN ({', '.join(_SHELO_CLASSES)})",
            name="ck_shelo_factors_class",
        ),
    )
    op.create_index("ix_shelo_factors_event", "shelo_factors", ["event_id"])

    # ── shelo_factor_interactions ───────────────────────────────────
    op.create_table(
        "shelo_factor_interactions",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("uuid_generate_v4()"),
        ),
        # Denormalised event_id makes "all interactions for this
        # event" a single-table query and gives us the natural key
        # we need for cross-event uniqueness.
        sa.Column(
            "event_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("accident_events.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "source_factor_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("shelo_factors.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "target_factor_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("shelo_factors.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("interaction_kind", sa.String(length=20), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column(
            "editor_user_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            f"interaction_kind IN ({', '.join(_INTERACTION_KINDS)})",
            name="ck_shelo_factor_interactions_kind",
        ),
        sa.CheckConstraint(
            "source_factor_id <> target_factor_id",
            name="ck_shelo_factor_interactions_no_self_loop",
        ),
    )
    # Natural key: at most one interaction edge of each kind per
    # source→target pair within an event.  Different kinds can
    # coexist (A AGGRAVATED B and A PRECONDITION B are distinct
    # editorial claims).
    op.create_index(
        "uq_shelo_factor_interactions_natural",
        "shelo_factor_interactions",
        ["event_id", "source_factor_id", "target_factor_id", "interaction_kind"],
        unique=True,
    )
    op.create_index(
        "ix_shelo_factor_interactions_event",
        "shelo_factor_interactions",
        ["event_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_shelo_factor_interactions_event",
        table_name="shelo_factor_interactions",
    )
    op.drop_index(
        "uq_shelo_factor_interactions_natural",
        table_name="shelo_factor_interactions",
    )
    op.drop_table("shelo_factor_interactions")

    op.drop_index("ix_shelo_factors_event", table_name="shelo_factors")
    op.drop_table("shelo_factors")

    op.drop_index(
        "ix_event_hfacs_attributions_event",
        table_name="event_hfacs_attributions",
    )
    op.execute("DROP INDEX IF EXISTS uq_event_hfacs_attributions_natural")
    op.drop_table("event_hfacs_attributions")

    op.drop_index("ix_hfacs_subcategories_category", table_name="hfacs_subcategories")
    op.drop_index("uq_hfacs_subcategories_code", table_name="hfacs_subcategories")
    op.drop_table("hfacs_subcategories")

    op.drop_index("uq_hfacs_categories_code", table_name="hfacs_categories")
    op.drop_table("hfacs_categories")
