"""Static migration/ORM consistency checks.

These don't require a database - they statically inspect the latest migration
file and the ORM declarative metadata, looking for the kinds of drift that
would silently break Alembic upgrades from an empty database.

What we check (non-exhaustive but high-value):
- Every ORM table exists in some migration's ``CREATE TABLE`` text.
- Critical unique constraints declared on ORM models also appear in some
  migration.
- Outbox status server_default matches the StrEnum case.

We deliberately do NOT diff every column type - a full Alembic
autogenerate-diff is the right tool for that and is out of scope here.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from atlas.domain.enums import OutboxStatus
from atlas.infrastructure.db.orm_models import Base

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "alembic" / "versions"


def _all_migration_text() -> str:
    return "\n".join(p.read_text() for p in sorted(MIGRATIONS_DIR.glob("*.py")))


@pytest.fixture(scope="module")
def migrations_text() -> str:
    return _all_migration_text()


def test_every_orm_table_appears_in_a_migration(migrations_text):
    orm_tables = set(Base.metadata.tables.keys())
    missing = []
    for table in orm_tables:
        # Match either a positional or keyword create_table call.
        pattern = rf'create_table\(\s*["\']{re.escape(table)}["\']'
        if not re.search(pattern, migrations_text):
            missing.append(table)
    assert not missing, f"ORM tables not created by any migration: {missing}"


def test_unique_raw_snapshot_ingestion_key_is_in_migrations(migrations_text):
    # This is the constraint that powers idempotent ingestion (see
    # ``RawSnapshotRepository.try_add_unique``). Migration 005 added it.
    assert "uq_raw_snapshot_ingestion_key" in migrations_text


def test_unique_projection_history_outbox_event_index_is_in_migrations(migrations_text):
    # Powers ReProjectEvent's idempotency check.
    assert "uq_projection_history_outbox_event" in migrations_text


def test_outbox_status_default_matches_uppercase_enum(migrations_text):
    # OutboxStatus.PENDING == "PENDING". Migration 007 normalises the default
    # to uppercase. The ORM model must agree.
    from atlas.infrastructure.db.orm_models import OutboxEventModel

    status_col = OutboxEventModel.__table__.c.status
    # SQLAlchemy keeps the python-level default as the ``default`` kwarg; the
    # server-side default we set on creation is "PENDING".
    assert OutboxStatus.PENDING.value == "PENDING"
    assert status_col.default is not None
    assert status_col.default.arg == "PENDING"


def test_curator_override_seed_uses_valid_reliability_tier(migrations_text):
    # Source.reliability_tier has a Pydantic ge=1 constraint. The seed in
    # migration 003 must respect that or the entity will fail to round-trip.
    seed_match = re.search(
        r"INSERT INTO sources[^']*'CuratorOverride'[^,]*,\s*'INTERNAL'\s*,\s*(\d+)",
        migrations_text,
        flags=re.DOTALL,
    )
    assert seed_match, "Could not locate CuratorOverride seed insert"
    tier = int(seed_match.group(1))
    assert tier >= 1, f"CuratorOverride seed has invalid tier {tier} (must be >= 1)"


def test_api_keys_table_has_unique_key_hash(migrations_text):
    # Auth correctness depends on this - without uniqueness, two rows could
    # claim the same hash and ``scalar_one_or_none`` would raise.
    assert "ix_api_keys_key_hash" in migrations_text
    # Migration 002 declares unique=True on the index.
    assert re.search(
        r'create_index\(\s*["\']ix_api_keys_key_hash["\'][^)]*unique=True',
        migrations_text,
    )


def test_conflict_activity_sequence_constraint_in_migrations(migrations_text):
    """The unique constraint preventing concurrent sequence duplicates must be
    declared in a migration (migration 001 creates it inline)."""
    assert "uq_conflict_activity_sequence" in migrations_text
    """The repository filters active claims using ``ClaimType.active_values()``;
    that set must exclude SUPERSEDED and include the other three."""
    from atlas.domain.enums import ClaimType

    active = ClaimType.active_values()
    assert ClaimType.SUPERSEDED.value not in active
    assert active == frozenset({"RAW", "CONFIRMED", "MANUAL_OVERRIDE"})


def test_partial_unique_index_for_open_conflicts_exists(migrations_text):
    """Migration 008 must add the partial unique index that prevents duplicate
    OPEN conflicts under concurrent ingestion. The app-level check in
    ``ingest_source_data`` uses ``try_add_open``; the DB index is the safety net."""
    assert "uq_open_conflict_event_field" in migrations_text


def test_outbox_next_attempt_at_column_exists(migrations_text):
    """Migration 008 must add ``next_attempt_at`` so the retry state machine
    can schedule exponential-backoff retries."""
    assert "next_attempt_at" in migrations_text


def test_conflict_activity_sequence_uniqueness_constraint_in_orm():
    """The ORM declares ``uq_conflict_activity_sequence`` to enforce per-conflict
    sequence uniqueness at the DB level. Verify it is present in the table args."""
    from atlas.infrastructure.db.orm_models import ConflictActivityLogModel

    constraints = {
        c.name
        for c in ConflictActivityLogModel.__table__.constraints
        if hasattr(c, "name") and c.name
    }
    assert "uq_conflict_activity_sequence" in constraints, (
        "uq_conflict_activity_sequence missing from ConflictActivityLogModel"
    )


def test_sources_field_mapping_json_column_in_both_migration_and_orm(migrations_text):
    """Migration 017 adds ``sources.field_mapping_json``; the ORM
    ``SourceModel`` must declare it with the same NOT NULL + ``'{}'::jsonb``
    server_default semantics.  This is a static drift check - without it, a
    future autogenerate could silently propose dropping the column on the next
    migration round-trip if someone removed it from the ORM.
    """
    from atlas.infrastructure.db.orm_models import SourceModel

    # Migration text mentions both the column and the empty-object server default.
    assert "field_mapping_json" in migrations_text
    assert "'{}'::jsonb" in migrations_text

    col = SourceModel.__table__.c.field_mapping_json
    assert col is not None, "SourceModel.field_mapping_json missing"
    assert not col.nullable, "field_mapping_json must be NOT NULL"
    # ORM declares server_default=text("'{}'::jsonb"); SQLAlchemy stores the
    # underlying TextClause string when one is supplied.
    assert col.server_default is not None
    assert "'{}'::jsonb" in str(col.server_default.arg)


def test_raw_snapshots_audit_pair_check_constraint_in_both_migration_and_orm(migrations_text):
    """Migration 018 adds a CHECK constraint asserting that the audit-column
    pair ``(raw_payload_hash, submission_fingerprint_json)`` is populated
    together.  The ORM must declare the matching constraint so Alembic
    autogenerate stays consistent.
    """
    from atlas.infrastructure.db.orm_models import RawSnapshotModel

    assert "ck_raw_snapshots_audit_pair_consistent" in migrations_text
    constraint_names = {
        c.name for c in RawSnapshotModel.__table__.constraints if hasattr(c, "name") and c.name
    }
    assert "ck_raw_snapshots_audit_pair_consistent" in constraint_names, (
        "ck_raw_snapshots_audit_pair_consistent missing from RawSnapshotModel"
    )


def test_claim_hot_path_indexes_in_both_migration_and_orm(migrations_text):
    """Claim queries used by ingestion/projection/reopen paths should have
    explicit DB indexes and matching ORM metadata so Alembic does not drift.
    """
    from atlas.infrastructure.db.orm_models import ClaimModel

    expected = {
        "ix_claims_active_event",
        "ix_claims_active_event_field",
        "ix_claims_raw_snapshot_id",
        "ix_claims_superseded_by_claim_id",
    }
    for index_name in expected:
        assert index_name in migrations_text

    orm_indexes = {index.name for index in ClaimModel.__table__.indexes}
    assert expected <= orm_indexes


def test_argus_signals_dedupe_unique_index_in_both_migration_and_orm(migrations_text):
    """The unique index on ``argus_signals.dedupe_key`` is what makes
    ``upsert_signal`` race-safe under concurrent detection runs.  Drift here
    would let two callers create duplicate signals for the same dedupe key.
    """
    from atlas.infrastructure.db.orm_models import ArgusSignalModel

    assert "uq_argus_signals_dedupe_key" in migrations_text
    orm_index_names = {idx.name for idx in ArgusSignalModel.__table__.indexes}
    assert "uq_argus_signals_dedupe_key" in orm_index_names


def test_argus_signal_evidence_uniqueness_in_both_migration_and_orm(migrations_text):
    """The unique constraint on (signal_id, evidence_type, evidence_id) makes
    ``upsert_evidence`` idempotent — fundamental to safe re-runs."""
    from atlas.infrastructure.db.orm_models import ArgusSignalEvidenceModel

    assert "uq_argus_signal_evidence_link" in migrations_text
    constraint_names = {
        c.name
        for c in ArgusSignalEvidenceModel.__table__.constraints
        if hasattr(c, "name") and c.name
    }
    assert "uq_argus_signal_evidence_link" in constraint_names


def test_argus_signals_check_constraints_match_between_migration_and_orm(migrations_text):
    """Every Argus check-constraint declared on the ORM must also exist in
    the migration text.  These constraints duplicate the same string list in
    two places (a known design wart) so a static drift check is worth its
    weight."""
    from atlas.infrastructure.db.orm_models import ArgusSignalModel

    expected = {
        "ck_argus_signals_signal_type",
        "ck_argus_signals_status",
        "ck_argus_signals_severity",
        "ck_argus_signals_confidence",
    }
    for name in expected:
        assert name in migrations_text, f"{name} missing from migrations"
    orm_check_names = {
        c.name for c in ArgusSignalModel.__table__.constraints if hasattr(c, "name") and c.name
    }
    assert expected <= orm_check_names


def test_argus_signals_ordering_index_in_both_migration_and_orm(migrations_text):
    """Migration 032 adds the composite index that powers stable ordering for
    ``GET /argus/signals``.  Without it, offset pagination silently skips or
    duplicates rows whenever two signals share ``last_detected_at``."""
    from atlas.infrastructure.db.orm_models import ArgusSignalModel

    assert "ix_argus_signals_last_detected_id_desc" in migrations_text
    orm_index_names = {idx.name for idx in ArgusSignalModel.__table__.indexes}
    assert "ix_argus_signals_last_detected_id_desc" in orm_index_names


def test_argus_signals_version_column_in_both_migration_and_orm(migrations_text):
    """Migration 033 adds the optimistic-concurrency ``version`` column.
    Without it, ``ReviewArgusSignal.execute`` would silently accept stale
    expected_version values."""
    from atlas.infrastructure.db.orm_models import ArgusSignalModel

    # The migration must mention adding the version column.
    assert "add_column" in migrations_text
    assert '"version"' in migrations_text or "'version'" in migrations_text
    assert "argus_signals" in migrations_text

    # The ORM must declare the column.
    assert "version" in ArgusSignalModel.__table__.c
    version_col = ArgusSignalModel.__table__.c["version"]
    assert not version_col.nullable, "version must be NOT NULL"

    # And the matching CHECK constraint must exist in both layers.
    assert "ck_argus_signals_version_positive" in migrations_text
    constraint_names = {
        c.name for c in ArgusSignalModel.__table__.constraints if hasattr(c, "name") and c.name
    }
    assert "ck_argus_signals_version_positive" in constraint_names


def test_chronos_sequence_review_pending_pair_index_in_both_migration_and_orm(migrations_text):
    """The partial expression index ``uq_chronos_sequence_reviews_pending_pair``
    was created in migration 029 via raw SQL.  It must now also be declared
    in ``ChronosSequenceReviewModel.__table_args__`` so Alembic autogenerate
    does not suggest recreating it.

    This test pins both sides of that contract so future drift is caught early.
    """
    from atlas.infrastructure.db.orm_models import ChronosSequenceReviewModel

    # The index must be named in the migration text.
    assert "uq_chronos_sequence_reviews_pending_pair" in migrations_text

    # And declared in the ORM metadata.
    orm_index_names = {idx.name for idx in ChronosSequenceReviewModel.__table__.indexes}
    assert "uq_chronos_sequence_reviews_pending_pair" in orm_index_names


def test_migration_034_has_duplicate_preflight_with_bypass_env_var(migrations_text):
    """Migration 034's preflight check is the safety net for upgrades against
    databases that may already contain duplicate active Orion identifiers.

    The contract this test guards:
      * the migration runs a preflight SELECT before altering the schema,
      * operators have a documented bypass env var
        (``ALEMBIC_034_SKIP_DUPLICATE_PREFLIGHT``) for cases where
        duplicates have already been cleaned out of band,
      * the preflight runs BEFORE the index-creating DDL so a failure
        leaves the DB in a clean, re-runnable state.
    """
    m34 = (MIGRATIONS_DIR / "034_hermes_leases_and_orion_identity_keys.py").read_text(
        encoding="utf-8"
    )
    assert "ALEMBIC_034_SKIP_DUPLICATE_PREFLIGHT" in m34, (
        "The bypass env var documented in the migration docstring must "
        "actually be read by the preflight code."
    )
    # The preflight call must appear before the unique-index creation,
    # so a failure cannot leave the partial index half-created.  Look at
    # the actual ``op.create_index(...`` call, not docstring references
    # to the index name.
    preflight_pos = m34.find("_check_for_duplicate_active_identifiers()")
    index_ddl_pos = m34.find(
        'op.create_index(\n        "uq_orion_entity_identifiers_active_strong_identity"'
    )
    assert preflight_pos > 0, "Preflight call must appear in the upgrade body."
    assert index_ddl_pos > 0, "Active-identifier unique index DDL must still exist."
    assert preflight_pos < index_ddl_pos, (
        "Preflight check must run BEFORE the partial unique index DDL. "
        "Otherwise a duplicate-driven failure stops mid-schema-change "
        "rather than before the Orion alterations begin."
    )


def _check_constraint_sql(model, name: str) -> str:
    for constraint in model.__table__.constraints:
        if getattr(constraint, "name", None) == name:
            return str(constraint.sqltext)
    raise AssertionError(f"{name} missing from {model.__name__}")


def test_usage_metric_kind_constraints_include_echo_crossref_run():
    from atlas.infrastructure.db.orm_models import UsageDailyRollupModel, UsageEventModel

    for model in (UsageEventModel, UsageDailyRollupModel):
        sql = _check_constraint_sql(model, f"ck_{model.__tablename__}_metric_kind")
        assert "ECHO_CROSSREF_RUN" in sql


def test_outbox_event_type_constraint_allows_echo_crossref_requested(migrations_text):
    from atlas.infrastructure.db.orm_models import OutboxEventModel

    sql = _check_constraint_sql(OutboxEventModel, "ck_outbox_events_event_type")
    assert "ECHO_CROSSREF_REQUESTED" in sql
    assert "ECHO_CROSSREF_REQUESTED" in migrations_text


def test_fk_covering_indexes_migration_exists(migrations_text: str) -> None:
    """Migration 049 must exist and cover the FK columns identified in the audit."""
    assert (
        "049_fk_covering_indexes" in migrations_text
        or "ix_accident_events_merged_into" in migrations_text
    ), "Migration 049 (FK covering indexes) must be present"
    # Spot-check a few of the specific index names
    for idx_name in [
        "ix_accident_events_merged_into",
        "ix_ingestion_runs_source_id",
        "ix_raw_snapshots_ingestion_run_id",
        "ix_tenant_ingestion_runs_source_id",
        "ix_tenant_claims_ingestion_run_id",
        "ix_tenant_claims_source_id",
        "ix_tenant_crossref_results_claim_id",
    ]:
        assert idx_name in migrations_text, (
            f"Expected FK index {idx_name!r} to be created by migration 049"
        )


def test_tenant_crossref_results_rls_documented_in_045(migrations_text: str) -> None:
    """Migration 045 must document that tenant_crossref_results is covered by 046."""
    mig_045 = next(
        (
            f
            for f in __import__("pathlib").Path("alembic/versions").glob("*.py")
            if f.name.startswith("045")
        ),
        None,
    )
    assert mig_045 is not None
    text = mig_045.read_text()
    assert "tenant_crossref_results" in text, (
        "Migration 045 must document that tenant_crossref_results RLS is handled in 046"
    )


def test_all_tenant_payload_tables_have_rls_policy(migrations_text: str) -> None:
    """Every tenant_* payload table must have RLS enabled in the migrations.

    Bootstrap tables (tenants, tenant_memberships) are intentionally excluded.
    Migration 045 loops over _TENANT_PAYLOAD_TABLES; migration 046 applies RLS
    inline.  We detect coverage via ALTER TABLE ... ENABLE ROW LEVEL SECURITY.
    """
    import re as _re
    from pathlib import Path as _Path

    rls_enabled: set = set()
    for f in sorted(_Path("alembic/versions").glob("*.py")):
        src = f.read_text()
        # Direct ALTER TABLE <name> ENABLE ROW LEVEL SECURITY
        for m in _re.finditer(r"ALTER TABLE (\w+) ENABLE ROW LEVEL SECURITY", src):
            rls_enabled.add(m.group(1))
        # Loop-based pattern in 045: table name is in _TENANT_PAYLOAD_TABLES tuple
        if "ENABLE ROW LEVEL SECURITY" in src and "_TENANT_PAYLOAD_TABLES" in src:
            m = _re.search(r"_TENANT_PAYLOAD_TABLES[^=]*=[^(]*\(([^)]+)\)", src, _re.S)
            if m:
                tables = _re.findall(r'["\'](\w+)["\'"]', m.group(1))
                rls_enabled.update(tables)

    bootstrap = {"tenants", "tenant_memberships"}
    all_tenant_tables = set(_re.findall(r'op\.create_table\(\s*["\'](\w+)["\'"]', migrations_text))
    tenant_payload = {t for t in all_tenant_tables if t.startswith("tenant_")} - bootstrap

    unprotected = tenant_payload - rls_enabled
    assert not unprotected, (
        f"Tenant payload tables without RLS: {sorted(unprotected)}. "
        "Add ENABLE ROW LEVEL SECURITY for each."
    )
