"""Root pytest plugin - registers the ``--run-integration`` CLI option.

This file lives at ``tests/conftest.py`` so pytest discovers it before
collecting any sub-package (including ``tests/integration/``).  Moving
``pytest_addoption`` here is the reason ``pytest --run-integration`` works
from the repo root without extra flags.

``tests/integration/conftest.py`` keeps the fixtures and the skip logic
(``pytest_configure`` / ``pytest_collection_modifyitems``) because those
must run during the integration-package collection phase.

Ref: https://docs.pytest.org/en/stable/reference/fixtures.html#conftest-py-sharing-fixtures-across-files
"""

from __future__ import annotations


def pytest_addoption(parser) -> None:
    parser.addoption(
        "--run-integration",
        action="store_true",
        default=False,
        help="Run integration tests against a live PostgreSQL instance",
    )
