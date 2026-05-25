from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from atlas.application.use_cases.query_provenance import QueryProvenance
from atlas.application.use_cases.verify_projection_consistency import VerifyProjectionConsistency
from atlas.domain.entities import (
    AccidentEvent,
    AccidentProjectionHistory,
    Claim,
    ClaimConflict,
    ClaimHistory,
    ConflictActivityLogEntry,
    ProjectedAccidentRecord,
    Source,
)
from atlas.domain.enums import ClaimType, ConflictStatus, ModifierType, SourceKind
from tests.domain._fake_uow import InMemoryUnitOfWork

_REPOSITORIES_PKG = Path("src/atlas/infrastructure/db/repositories")


def _read_repositories_text() -> str:
    """Return the concatenated source of every file in the repositories package.

    The repository code was split out of a single ``repositories.py`` file
    in r9.  Static-shape tests that used to ``Path(...).read_text()`` the
    monolith now read every ``.py`` file in the package directory so the
    same assertions cover the split code without changing.
    """
    return "\n".join(p.read_text(encoding="utf-8") for p in sorted(_REPOSITORIES_PKG.glob("*.py")))


def _ts(offset: int) -> datetime:
    return datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=offset)


async def test_query_provenance_paginates_all_high_cardinality_sections() -> None:
    uow = InMemoryUnitOfWork()
    event = AccidentEvent(id=uuid4())
    source = Source(id=uuid4(), name="Source", kind=SourceKind.EXTERNAL, reliability_tier=1)
    await uow.events.add(event)
    await uow.sources.add(source)

    for index in range(3):
        claim = Claim(
            id=uuid4(),
            event_id=event.id,
            source_id=source.id,
            field_name=f"field_{index}",
            field_value={"payload": index},
            claim_type=ClaimType.RAW,
            created_at=_ts(index),
        )
        await uow.claims.add(claim)
        await uow.claim_history.add(
            ClaimHistory(
                id=uuid4(),
                claim_id=claim.id,
                event_id=event.id,
                to_claim_type=ClaimType.RAW,
                action="created",
                reason=f"history {index}",
                modifier_type=ModifierType.INGESTION,
                created_at=_ts(index),
            )
        )

        conflict = ClaimConflict(
            id=uuid4(),
            event_id=event.id,
            field_name=f"field_{index}",
            status=ConflictStatus.OPEN,
            created_at=_ts(index),
            updated_at=_ts(index),
        )
        await uow.conflicts.add(conflict)
        await uow.conflict_activity.add(
            ConflictActivityLogEntry(
                id=uuid4(),
                conflict_id=conflict.id,
                event_id=event.id,
                sequence=1,
                to_status=ConflictStatus.OPEN,
                modifier_type=ModifierType.SYSTEM,
                reason=f"log {index}",
                version_at_moment=1,
                created_at=_ts(index),
            )
        )

        await uow.projection_history.add(
            AccidentProjectionHistory(
                id=uuid4(),
                accident_event_id=event.id,
                projection_version=index + 1,
                projected_record_snapshot={"payload": index},
                projected_record_hash=f"hash-{index}",
                changed_fields=[f"field_{index}"],
                created_at=_ts(index),
            )
        )

    await uow.projections.upsert(
        ProjectedAccidentRecord(event_id=event.id, fields={"event_date": "2026-01-01"})
    )

    first_page = await QueryProvenance(uow).execute(event.id, limit=2)

    assert len(first_page["claims"]) == 2
    assert len(first_page["claim_histories"]) == 2
    assert len(first_page["conflicts"]) == 2
    assert len(first_page["conflict_activity_logs"]) == 2
    assert len(first_page["projection_history"]) == 2
    assert first_page["pagination"]["has_more"] is True
    assert all(first_page["pagination"]["next_cursors"].values())

    next_cursors = first_page["pagination"]["next_cursors"]
    second_page = await QueryProvenance(uow).execute(
        event.id,
        limit=2,
        claims_cursor=next_cursors["claims"],
        claim_history_cursor=next_cursors["claim_histories"],
        conflicts_cursor=next_cursors["conflicts"],
        conflict_activity_cursor=next_cursors["conflict_activity_logs"],
        projection_history_cursor=next_cursors["projection_history"],
    )

    assert len(second_page["claims"]) == 1
    assert len(second_page["claim_histories"]) == 1
    assert len(second_page["conflicts"]) == 1
    assert len(second_page["conflict_activity_logs"]) == 1
    assert len(second_page["projection_history"]) == 1
    assert second_page["pagination"]["has_more"] is False


def test_sql_projection_matching_uses_case_guarded_date_cast() -> None:
    text = _read_repositories_text()

    assert "AND CASE" in text
    assert "WHEN (p.fields->>'event_date') ~" in text
    assert "THEN (p.fields->>'event_date')::date" in text
    assert "AND (p.fields->>'event_date')::date" not in text


def test_sql_advisory_locks_are_namespaced() -> None:
    text = _read_repositories_text()

    assert "ADVISORY_LOCK_SOURCE_RECORD_CORRECTION = 1" in text
    assert "ADVISORY_LOCK_REPROJECTION = 2" in text
    assert "ADVISORY_LOCK_IDENTITY_RESOLUTION = 3" in text
    assert "pg_advisory_xact_lock(CAST(:namespace AS integer), hashtext(:k))" in text
    assert "hashtextextended" not in text


def test_provenance_history_pagination_uses_event_denormalized_indexes() -> None:
    orm_text = Path("src/atlas/infrastructure/db/orm_models.py").read_text()
    repo_text = _read_repositories_text()
    migration_text = Path("alembic/versions/021_provenance_pagination_indexes.py").read_text()

    assert "event_id: Mapped[uuid.UUID]" in orm_text
    assert 'Index("ix_claim_history_event_created_id", "event_id", "created_at", "id")' in orm_text
    assert (
        'Index("ix_conflict_activity_event_created_id", "event_id", "created_at", "id")' in orm_text
    )
    assert (
        "event_id: Mapped[uuid.UUID] = mapped_column(\n"
        '        UUID(as_uuid=True), ForeignKey("accident_events.id"), nullable=False\n'
        "    )"
    ) in orm_text
    assert (
        "accident_event_id: Mapped[uuid.UUID] = mapped_column(\n"
        '        UUID(as_uuid=True), ForeignKey("accident_events.id"), nullable=False\n'
        "    )"
    ) in orm_text
    assert ".join(ClaimModel, ClaimHistoryModel.claim_id == ClaimModel.id)" not in repo_text
    assert ".join(ClaimConflictModel, ConflictActivityLogModel.conflict_id" not in repo_text
    assert ".where(ClaimHistoryModel.event_id == event_id)" in repo_text
    assert ".where(ConflictActivityLogModel.event_id == event_id)" in repo_text
    assert "ix_claim_history_event_created_id" in migration_text
    assert "ix_conflict_activity_event_created_id" in migration_text
    assert 'op.drop_index("ix_claims_event_id", table_name="claims")' in migration_text
    assert (
        'op.drop_index("ix_claim_conflicts_event_id", table_name="claim_conflicts")'
        in migration_text
    )
    assert "ix_claim_history_claim_created_id" not in migration_text
    assert "ix_conflict_activity_conflict_created_id" not in migration_text


def test_resolved_conflict_winning_claim_index_is_declared() -> None:
    orm_text = Path("src/atlas/infrastructure/db/orm_models.py").read_text()
    migration_text = Path("alembic/versions/027_conflict_winning_claim_index.py").read_text()

    assert "ix_claim_conflicts_resolved_winning_claim" in orm_text
    assert "winning_claim_id" in orm_text
    assert "status = 'RESOLVED' AND winning_claim_id IS NOT NULL" in orm_text
    assert "ix_claim_conflicts_resolved_winning_claim" in migration_text
    assert 'down_revision = "026"' in migration_text
    assert 'op.drop_index("ix_claim_conflicts_resolved_winning_claim"' in migration_text


async def test_admin_projection_verify_accepts_missing_projection_for_merged_event() -> None:
    uow = InMemoryUnitOfWork()
    target_id = uuid4()
    source_id = uuid4()
    await uow.events.add(AccidentEvent(id=target_id))
    await uow.events.add(AccidentEvent(id=source_id, merged_into_event_id=target_id))

    payload = await VerifyProjectionConsistency(uow).execute(source_id)

    assert payload is not None
    assert payload["status"] == "consistent"
    assert payload["stored_version"] is None
    assert payload["projection_absent_expected"] is True
    assert payload["field_diff"] == {}


async def test_admin_projection_verify_compares_existing_merged_tombstone() -> None:
    uow = InMemoryUnitOfWork()
    target_id = uuid4()
    source_id = uuid4()
    await uow.events.add(AccidentEvent(id=target_id))
    await uow.events.add(AccidentEvent(id=source_id, merged_into_event_id=target_id))
    await uow.projections.upsert(
        ProjectedAccidentRecord(
            event_id=source_id,
            projection_version=3,
            fields={
                "is_merged": True,
                "merged_into_event_id": str(target_id),
            },
            completeness_score=0.0,
            unresolved_conflict_fields=[],
        )
    )

    payload = await VerifyProjectionConsistency(uow).execute(source_id)

    assert payload is not None
    assert payload["status"] == "consistent"
    assert payload["stored_version"] == 3
    assert payload["projection_absent_expected"] is False
    assert payload["field_diff"] == {}


def test_readme_declares_current_alembic_head() -> None:
    """README migration head must match the actual Alembic head revision.

    This test is the in-process twin of the CI shell check.  Running it
    locally catches the mismatch before pushing, rather than waiting for CI.
    It dynamically computes the actual head so it never needs updating again
    when a new migration is added — only the README does.
    """
    import re as _re
    from pathlib import Path as _Path

    readme = _Path("README.md").read_text()
    match = _re.search(r"current head is `([^`]+)`", readme)
    assert match, (
        "README must declare the current migration head in the form `current head is `...``"
    )
    declared = match.group(1)

    versions_dir = _Path("alembic/versions")
    revisions: dict[str, str] = {}
    down_revisions: set[str] = set()
    for path in versions_dir.glob("*.py"):
        text = path.read_text()
        rev = _re.search(r'^revision\s*=\s*[\'"]([^\'"]+)[\'"]', text, _re.M)
        down = _re.search(r'^down_revision\s*=\s*[\'"]?([^\'")\n]+)', text, _re.M)
        if rev:
            revisions[rev.group(1)] = path.stem
        if down and down.group(1).strip() not in {"None", "null"}:
            down_revisions.add(down.group(1).strip())

    heads = sorted(set(revisions) - down_revisions)
    assert len(heads) == 1, f"Expected exactly one Alembic head, found: {heads}"
    actual = revisions[heads[0]]
    assert declared == actual, (
        f"README declares head {declared!r} but the actual Alembic head is {actual!r}. "
        "Update README.md to match."
    )


def test_history_event_id_is_required_at_domain_boundary() -> None:
    event_id = uuid4()
    claim_id = uuid4()
    conflict_id = uuid4()

    with pytest.raises(ValidationError):
        ClaimHistory(
            id=uuid4(),
            claim_id=claim_id,
            to_claim_type=ClaimType.RAW,
            action="created",
            reason="missing event id",
            modifier_type=ModifierType.SYSTEM,
        )

    with pytest.raises(ValidationError):
        ConflictActivityLogEntry(
            id=uuid4(),
            conflict_id=conflict_id,
            sequence=1,
            to_status=ConflictStatus.OPEN,
            modifier_type=ModifierType.SYSTEM,
            reason="missing event id",
            version_at_moment=1,
        )

    assert (
        ClaimHistory(
            id=uuid4(),
            claim_id=claim_id,
            event_id=event_id,
            to_claim_type=ClaimType.RAW,
            action="created",
            reason="has event id",
            modifier_type=ModifierType.SYSTEM,
        ).event_id
        == event_id
    )
    assert (
        ConflictActivityLogEntry(
            id=uuid4(),
            conflict_id=conflict_id,
            event_id=event_id,
            sequence=1,
            to_status=ConflictStatus.OPEN,
            modifier_type=ModifierType.SYSTEM,
            reason="has event id",
            version_at_moment=1,
        ).event_id
        == event_id
    )


def test_history_repositories_do_not_lookup_parent_event_id_on_add() -> None:
    text = _read_repositories_text()

    claim_add = text[
        text.index("class SqlClaimHistoryRepository") : text.index("class SqlConflictRepository")
    ]
    activity_add = text[
        text.index("class SqlConflictActivityLogRepository") : text.index(
            "class SqlProjectionRepository"
        )
    ]
    assert "ClaimModel.event_id" not in claim_add
    assert "ClaimConflictModel.event_id" not in activity_add


def test_keyset_cursor_is_resolved_before_page_query() -> None:
    text = _read_repositories_text()

    assert "async def _apply_created_at_uuid_cursor" in text
    assert "await session.scalar" in text
    assert ".scalar_subquery()" not in text
    # The cursor tuple may be wrapped through ``literal()`` for strict-mode
    # typing - what matters is that it consumes the resolved scalar values,
    # not an in-line subquery.
    assert "cursor_key = tuple_(" in text
    assert "cursor_created_at" in text
    assert "after_id" in text


def test_capped_alias_union_has_deterministic_tie_breakers() -> None:
    text = _read_repositories_text()

    assert "ORDER BY MAX(ord) DESC, elem ASC" in text
    assert "jsonb_agg(value ORDER BY last_pos, value)" in text


def test_bulk_claim_state_transitions_chunk_large_id_sets() -> None:
    text = _read_repositories_text()
    bulk_block = text[
        text.index("async def bulk_supersede") : text.index(
            "async def count_total", text.index("async def bulk_unsupersede")
        )
    ]

    assert "BULK_ID_CHUNK_SIZE = 10_000" in text
    assert "def _chunked(items: Iterable[T], size: int = BULK_ID_CHUNK_SIZE)" in text
    assert "unique_claim_ids = list(dict.fromkeys(claim_ids))" in bulk_block
    assert "for chunk in _chunked(unique_claim_ids):" in bulk_block
    assert "ClaimModel.id.in_(chunk)" in bulk_block
    assert "for chunk in _chunked(active_ids):" in bulk_block
    assert "for chunk in _chunked([obj.id for obj in claim_models]):" in bulk_block
    assert "ClaimModel.id.in_(claim_ids)" not in bulk_block
    assert "ClaimHistoryModel.claim_id.in_([obj.id for obj in claim_models])" not in bulk_block


def test_projection_history_uniqueness_uses_single_covering_index() -> None:
    orm_text = Path("src/atlas/infrastructure/db/orm_models.py").read_text()
    migration_text = Path("alembic/versions/021_provenance_pagination_indexes.py").read_text()

    projection_model_block = orm_text[
        orm_text.index("class AccidentProjectionHistoryModel") : orm_text.index(
            "class ArchiveManifestModel"
        )
    ]
    assert "UniqueConstraint(" not in projection_model_block
    assert 'Index(\n            "uq_projection_history_version"' in projection_model_block
    assert 'unique=True,\n            postgresql_include=["id"]' in projection_model_block
    assert "ix_projection_history_event_version_id" not in projection_model_block

    assert 'op.drop_constraint(\n        "uq_projection_history_version"' in migration_text
    assert 'op.create_index(\n        "uq_projection_history_version"' in migration_text
    assert 'unique=True,\n        postgresql_include=["id"]' in migration_text
    assert "ix_projection_history_event_version_id" not in migration_text


async def test_admin_projection_verify_detects_completeness_mismatch() -> None:
    uow = InMemoryUnitOfWork()
    event = AccidentEvent(id=uuid4())
    source = Source(id=uuid4(), name="Source", kind=SourceKind.EXTERNAL, reliability_tier=1)
    await uow.events.add(event)
    await uow.sources.add(source)
    claim = Claim(
        id=uuid4(),
        event_id=event.id,
        source_id=source.id,
        field_name="event_date",
        field_value="2024-01-01",
        claim_type=ClaimType.RAW,
    )
    uow.store.claims[claim.id] = claim
    await uow.projections.upsert(
        ProjectedAccidentRecord(
            event_id=event.id,
            projection_version=1,
            fields={"event_date": "2024-01-01"},
            completeness_score=0.99,
            unresolved_conflict_fields=[],
        )
    )

    payload = await VerifyProjectionConsistency(uow).execute(event.id)

    assert payload is not None
    assert payload["status"] == "inconsistent"
    assert payload["field_diff"] == {}
    assert payload["unresolved_conflict_fields_diff"] == {}
    assert payload["completeness_score_diff"] == {
        "stored": 0.99,
        "recomputed": pytest.approx(1 / 9),
    }
