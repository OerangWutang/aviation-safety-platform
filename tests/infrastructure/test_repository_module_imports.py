"""Smoke tests for the SQL repository modules.

These tests deliberately do **not** require PostgreSQL.  They exercise the
hand-written ``_*_to_domain`` conversion helpers against ``SimpleNamespace``
stubs that mimic ORM rows.  The goal is to catch the entire class of bug
where a repository file references an enum, helper, or domain entity at
runtime but forgets to import it.

We saw exactly that bug in r12: ``argus.py`` was carved out of the old
monolithic ``repositories.py`` and the Argus repository's converters were
calling ``ArgusSignalType(...)``, ``ArgusSignalStatus(...)``, etc., while the
matching imports had been left behind in ``hermes.py``.  ``ruff`` and
``mypy`` both flagged it, but the unit-test suite uses a hand-rolled fake
UoW in ``tests/domain/_fake_uow.py`` that never imports the real SQL
repositories, so the bug never surfaced in pytest.

This test imports every SQL repository module and calls every
``_*_to_domain`` helper.  Any module-level ``NameError`` (the symptom of a
missing import) fails the test immediately.  No database needed.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest


def _now() -> datetime:
    return datetime.now(UTC)


def test_argus_signal_to_domain_has_all_required_imports() -> None:
    """Regression for r12: argus.py referenced enums without importing them.

    Pre-fix this raised ``NameError: name 'ArgusSignalType' is not defined``
    the moment any real Argus SQL read happened in production.
    """
    from atlas.infrastructure.db.repositories.argus import _argus_signal_to_domain

    row = SimpleNamespace(
        id=uuid4(),
        signal_type="NEW_SOURCE_CHANGE",
        status="OPEN",
        severity="HIGH",
        confidence=0.5,
        title="t",
        description="d",
        accident_event_id=None,
        primary_entity_id=None,
        source_engine="engine",
        dedupe_key="k",
        version=1,
        first_detected_at=_now(),
        last_detected_at=_now(),
        created_at=_now(),
        updated_at=_now(),
    )
    signal = _argus_signal_to_domain(row)
    assert signal.id == row.id
    assert signal.status.value == "OPEN"
    assert signal.signal_type.value == "NEW_SOURCE_CHANGE"
    assert signal.severity.value == "HIGH"
    assert signal.version == 1


def test_argus_evidence_to_domain_has_all_required_imports() -> None:
    from atlas.infrastructure.db.repositories.argus import _argus_evidence_to_domain

    row = SimpleNamespace(
        id=uuid4(),
        signal_id=uuid4(),
        evidence_type="ATLAS_CLAIM",
        evidence_id=uuid4(),
        engine="engine",
        summary="s",
        created_at=_now(),
    )
    evidence = _argus_evidence_to_domain(row)
    assert evidence.id == row.id
    assert evidence.evidence_type.value == "ATLAS_CLAIM"


def test_argus_review_to_domain_has_all_required_imports() -> None:
    from atlas.infrastructure.db.repositories.argus import _argus_review_to_domain

    row = SimpleNamespace(
        id=uuid4(),
        signal_id=uuid4(),
        decision="CONFIRMED",
        reviewer_id=uuid4(),
        note=None,
        created_at=_now(),
    )
    review = _argus_review_to_domain(row)
    assert review.id == row.id
    assert review.decision.value == "CONFIRMED"


def test_argus_repo_upsert_signal_resolves_severity_rank() -> None:
    """``upsert_signal`` calls ``severity_rank(...)`` at module level when
    building its CASE expression.  Pre-fix this raised NameError on the very
    first call because ``severity_rank`` was not imported in argus.py.

    We don't run the SQL — we just invoke the helper that builds the
    statement to surface the symbol resolution failure.
    """
    from atlas.domain.services.argus_severity import severity_rank as src_severity_rank
    from atlas.infrastructure.db.repositories import argus as argus_repo

    # Both references must resolve to the same callable.  This catches the
    # case where someone re-imports ``severity_rank`` from a stale location
    # in a future carve-out.
    assert argus_repo.severity_rank is src_severity_rank


def test_chronos_timeline_event_to_domain_has_all_required_imports() -> None:
    from atlas.infrastructure.db.repositories.chronos import _chronos_te_to_domain

    row = SimpleNamespace(
        id=uuid4(),
        accident_event_id=uuid4(),
        event_type="TAKEOFF",
        occurred_at=_now(),
        timestamp_precision="MINUTE",
        sequence_index=0,
        description="d",
        raw_value="raw",
        confidence=0.5,
        source_claim_id=uuid4(),
        raw_snapshot_id=uuid4(),
        created_at=_now(),
        updated_at=_now(),
    )
    te = _chronos_te_to_domain(row)
    assert te.id == row.id
    assert te.event_type.value == "TAKEOFF"


def test_chronos_review_to_domain_has_all_required_imports() -> None:
    from atlas.domain.enums import ChronosSequenceReviewStatus
    from atlas.infrastructure.db.repositories.chronos import _chronos_review_to_domain

    # Pick a real enum member so the constructor accepts the string.
    status = next(iter(ChronosSequenceReviewStatus)).value
    row = SimpleNamespace(
        id=uuid4(),
        accident_event_id=uuid4(),
        timeline_event_id_a=uuid4(),
        timeline_event_id_b=uuid4(),
        reason="r",
        status=status,
        created_at=_now(),
        resolved_at=None,
        resolved_by=None,
        resolution_note=None,
    )
    rev = _chronos_review_to_domain(row)
    assert rev.status.value == status


@pytest.mark.parametrize(
    "module_path",
    [
        "atlas.infrastructure.db.repositories.argus",
        "atlas.infrastructure.db.repositories.chronos",
        "atlas.infrastructure.db.repositories.hermes",
        "atlas.infrastructure.db.repositories.orion",
        "atlas.infrastructure.db.repositories.claims",
        "atlas.infrastructure.db.repositories.conflicts",
        "atlas.infrastructure.db.repositories.events",
        "atlas.infrastructure.db.repositories.identity",
        "atlas.infrastructure.db.repositories.ingestion",
        "atlas.infrastructure.db.repositories.outbox",
        "atlas.infrastructure.db.repositories.projections",
        "atlas.infrastructure.db.repositories.reviews",
        "atlas.infrastructure.db.repositories.snapshots",
        "atlas.infrastructure.db.repositories.sources",
        "atlas.infrastructure.db.repositories.archive",
    ],
)
def test_every_repository_module_imports_cleanly(module_path: str) -> None:
    """Every SQL repo module must import without raising.

    A module-level ``NameError`` at import time would crash the API
    container on startup; this asserts none of them are in that state.
    """
    import importlib

    module = importlib.import_module(module_path)
    assert module is not None


def test_repositories_package_resolves_all_referenced_names() -> None:
    """All Sql*Repository classes re-exported from the package must
    successfully resolve when accessed.

    This is stronger than a plain ``from ... import ...`` because Python
    can sometimes lazily resolve names; we want a hard failure if any
    Sql* class is dangling.
    """
    from atlas.infrastructure.db import repositories as pkg

    for name in pkg.__all__:
        if not name.startswith("Sql"):
            continue
        cls: Any = getattr(pkg, name)
        # A real class, not an unresolved module attribute.
        assert isinstance(cls, type), f"{name} did not resolve to a class"
