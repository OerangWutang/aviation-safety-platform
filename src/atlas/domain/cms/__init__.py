"""CMS-like content bounded context (Phase 10).

Three content kinds — glossary, methodology, changelog — all sharing
the editorial workflow from Phase 9 (``DRAFT`` → ``IN_REVIEW`` →
``APPROVED`` → ``PUBLISHED``, with ``ARCHIVED``/``RETRACTED``
terminals).

The shared workflow lives in ``atlas.domain.publication.workflow``
and is reused unchanged.  Each content kind has its own entity,
repository, and revision audit table because the row shapes differ
enough to make a polymorphic single table a contamination risk.
"""

from __future__ import annotations

from atlas.domain.cms.entities import (
    ChangelogEntry,
    ChangelogEntryRevision,
    GlossaryTerm,
    GlossaryTermRevision,
    MethodologyPage,
    MethodologyPageRevision,
)
from atlas.domain.cms.exceptions import (
    ChangelogEntryNotFoundError,
    ChangelogEntryNotPublishedError,
    ChangelogEntryRetractedError,
    CmsContentModifiedError,
    GlossaryTermNotFoundError,
    GlossaryTermNotPublishedError,
    GlossaryTermRetractedError,
    MethodologyPageNotFoundError,
    MethodologyPageNotPublishedError,
    MethodologyPageRetractedError,
)

__all__ = [
    "ChangelogEntry",
    "ChangelogEntryNotFoundError",
    "ChangelogEntryNotPublishedError",
    "ChangelogEntryRetractedError",
    "ChangelogEntryRevision",
    "CmsContentModifiedError",
    "GlossaryTerm",
    "GlossaryTermNotFoundError",
    "GlossaryTermNotPublishedError",
    "GlossaryTermRetractedError",
    "GlossaryTermRevision",
    "MethodologyPage",
    "MethodologyPageNotFoundError",
    "MethodologyPageNotPublishedError",
    "MethodologyPageRetractedError",
    "MethodologyPageRevision",
]
