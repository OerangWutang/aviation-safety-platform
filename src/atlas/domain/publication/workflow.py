"""Editorial workflow rules for ``PublicEventPage``.

The state machine is encoded as a single transition map plus a
validator function.  Encoding transitions as data (rather than as a
switch statement spread across use cases) means:

- the diagram in ``ARCHIVED.md`` has exactly one source of truth;
- adding a new state or transition is a one-line table edit;
- tests can iterate the table exhaustively to catch unintended
  reachability.

State diagram (Phase 9)
-----------------------

::

           create
              │
              ▼
           DRAFT ◄────────── request_changes ──── IN_REVIEW
              │  ▲                                   │
              │  │                                   │
              │  └────────── reopen ──── ARCHIVED ◄──┤  (after publish→archive)
              │                                      │
              │ submit                       approve │
              ▼                                      ▼
          IN_REVIEW ───────────────────────────► APPROVED
                                                     │
                                                     │ publish
                                                     ▼
                              ┌─────────────────► PUBLISHED ─── retract ──► RETRACTED  (terminal)
                              │                       │
                              │                       │ archive
                              │                       ▼
                              │                   ARCHIVED ──── reopen ─── (to DRAFT)
                              │                       │
                              └── re-publish ─────────┘   (ARCHIVED → PUBLISHED)

Notes
-----

- **RETRACTED is terminal.**  A retracted page cannot be brought back;
  the curator must create a new page with a new slug if the content is
  re-published later.  The retraction URL keeps returning 410 forever.

- **ARCHIVED is the soft-hide.**  Use this when content is temporarily
  withdrawn but may return.  Republish goes directly to PUBLISHED
  (keeping ``first_published_at``).

- **The create-revision uses the sentinel transition** ``None →
  DRAFT``.  Treated as a transition for revision-logging purposes;
  attempting to use it via :func:`validate_transition` raises.
"""

from __future__ import annotations

from atlas.domain.publication.entities import PublicationStatus
from atlas.domain.publication.exceptions import InvalidPublicationTransitionError

# Allowed transitions: from_status -> {to_status, ...}
#
# RETRACTED is intentionally absent from the keys: it has no outgoing
# transitions.  Pages that are retracted by mistake should be left in
# place; the audit trail then explains the retraction.
_ALLOWED_TRANSITIONS: dict[PublicationStatus, frozenset[PublicationStatus]] = {
    PublicationStatus.DRAFT: frozenset({PublicationStatus.IN_REVIEW}),
    PublicationStatus.IN_REVIEW: frozenset({PublicationStatus.APPROVED, PublicationStatus.DRAFT}),
    PublicationStatus.APPROVED: frozenset({PublicationStatus.PUBLISHED, PublicationStatus.DRAFT}),
    PublicationStatus.PUBLISHED: frozenset(
        {PublicationStatus.ARCHIVED, PublicationStatus.RETRACTED}
    ),
    PublicationStatus.ARCHIVED: frozenset({PublicationStatus.PUBLISHED, PublicationStatus.DRAFT}),
    # RETRACTED → nothing.  Terminal.
    PublicationStatus.RETRACTED: frozenset(),
}


def is_allowed(from_status: PublicationStatus, to_status: PublicationStatus) -> bool:
    """Pure predicate for "can this transition occur?"."""
    return to_status in _ALLOWED_TRANSITIONS.get(from_status, frozenset())


def validate_transition(from_status: PublicationStatus, to_status: PublicationStatus) -> None:
    """Raise :class:`InvalidPublicationTransitionError` if disallowed.

    Use cases call this *before* mutating the page row.  No
    side-effects; the caller still owns the actual mutation and the
    revision write.
    """
    if not is_allowed(from_status, to_status):
        raise InvalidPublicationTransitionError(from_status=from_status, to_status=to_status)


def allowed_next_states(
    from_status: PublicationStatus,
) -> frozenset[PublicationStatus]:
    """Return the set of legal next states from ``from_status``.

    Useful for the editorial UI to disable buttons that wouldn't
    succeed.  Pure read; safe to call from anywhere.
    """
    return _ALLOWED_TRANSITIONS.get(from_status, frozenset())


__all__ = [
    "allowed_next_states",
    "is_allowed",
    "validate_transition",
]
