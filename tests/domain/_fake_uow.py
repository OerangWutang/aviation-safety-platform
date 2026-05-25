"""Backward-compatible shim — imports from the split fakes package.

The implementation has moved to ``tests/domain/fakes/``.
All existing test imports continue to work unchanged.
"""

from tests.domain.fakes import InMemoryUnitOfWork, make_settings

__all__ = ["InMemoryUnitOfWork", "make_settings"]
