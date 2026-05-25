"""Verify InMemoryUnitOfWork stays in sync with SqlAlchemyUnitOfWork.

The fakes package split made ``InMemoryUnitOfWork`` easier to maintain, but
introduced a new risk: adding a repository to ``SqlAlchemyUnitOfWork`` while
forgetting to wire it into ``InMemoryUnitOfWork`` now produces a confusing
``AttributeError`` deep inside a use-case test rather than a clear failure.

This test makes the divergence explicit and immediate:

* It compares the repository attributes on both UoW classes.
* It fails with a named list of what's missing — so the error message is the
  fix instruction.

No database or session required; we introspect the source code only.
"""

from __future__ import annotations

import inspect
import re


def _sql_repo_attrs() -> set[str]:
    """Extract all ``self.X = SqlY(...)`` attribute names from SqlAlchemyUnitOfWork."""
    from atlas.infrastructure.db.unit_of_work import SqlAlchemyUnitOfWork

    src = inspect.getsource(SqlAlchemyUnitOfWork.__init__)
    # Match self.<attr> = Sql<anything>(  — the Sql prefix identifies repos.
    return set(re.findall(r"self\.(\w+)\s*=\s*Sql\w+\(", src))


def _fake_repo_attrs() -> set[str]:
    """Extract all ``self.X = Fake...`` attribute names from InMemoryUnitOfWork."""
    from tests.domain.fakes import InMemoryUnitOfWork

    src = inspect.getsource(InMemoryUnitOfWork.__init__)
    # Match self.<attr> = Fake<anything>(
    return set(re.findall(r"self\.(\w+)\s*=\s*Fake\w+\(", src))


def test_fake_uow_has_all_sql_uow_repos() -> None:
    """Every Sql* repo in SqlAlchemyUnitOfWork must have a Fake* counterpart.

    If this test fails, add the missing repository to
    ``tests/domain/fakes/<domain>.py`` and wire it into
    ``tests/domain/fakes/__init__.py :: InMemoryUnitOfWork.__init__``.
    """
    sql_attrs = _sql_repo_attrs()
    fake_attrs = _fake_repo_attrs()

    missing_from_fake = sql_attrs - fake_attrs
    assert not missing_from_fake, (
        f"InMemoryUnitOfWork is missing {len(missing_from_fake)} repo(s) "
        f"present in SqlAlchemyUnitOfWork.\n"
        f"Add them to tests/domain/fakes/ and wire into InMemoryUnitOfWork:\n"
        + "\n".join(f"  - {name}" for name in sorted(missing_from_fake))
    )


def test_no_extra_fake_repos_without_sql_counterpart() -> None:
    """Every Fake* repo in InMemoryUnitOfWork should have a Sql* counterpart.

    This is a soft warning test: it documents fakes that have drifted ahead
    of the SQL layer (possible during development) but does not block CI.
    Unlike the previous test it uses ``pytest.warns`` semantics — it prints
    but does not fail, since a fake-ahead-of-sql is less dangerous than the
    reverse.
    """
    import warnings

    sql_attrs = _sql_repo_attrs()
    fake_attrs = _fake_repo_attrs()

    extra_in_fake = fake_attrs - sql_attrs
    if extra_in_fake:
        warnings.warn(
            f"InMemoryUnitOfWork has {len(extra_in_fake)} repo(s) with no "
            f"Sql* counterpart in SqlAlchemyUnitOfWork: " + ", ".join(sorted(extra_in_fake)),
            stacklevel=2,
        )
