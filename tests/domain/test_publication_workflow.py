"""Exhaustive tests for the editorial workflow state machine.

The transition table is the source of truth for what an editor can
do, so these tests iterate the full cartesian product of states to
catch unintended reachability.  Anything not explicitly allowed must
raise :class:`InvalidPublicationTransitionError`.
"""

from __future__ import annotations

import itertools

import pytest

from atlas.domain.publication.entities import PublicationStatus
from atlas.domain.publication.exceptions import InvalidPublicationTransitionError
from atlas.domain.publication.workflow import (
    allowed_next_states,
    is_allowed,
    validate_transition,
)

# The canonical allowed-transition set.  Duplicated here intentionally:
# the workflow module is the production source of truth and this test
# is the regression net.  Drift between the two is what these tests
# catch.
_EXPECTED_ALLOWED = {
    (PublicationStatus.DRAFT, PublicationStatus.IN_REVIEW),
    (PublicationStatus.IN_REVIEW, PublicationStatus.APPROVED),
    (PublicationStatus.IN_REVIEW, PublicationStatus.DRAFT),
    (PublicationStatus.APPROVED, PublicationStatus.PUBLISHED),
    (PublicationStatus.APPROVED, PublicationStatus.DRAFT),
    (PublicationStatus.PUBLISHED, PublicationStatus.ARCHIVED),
    (PublicationStatus.PUBLISHED, PublicationStatus.RETRACTED),
    (PublicationStatus.ARCHIVED, PublicationStatus.PUBLISHED),
    (PublicationStatus.ARCHIVED, PublicationStatus.DRAFT),
}


class TestWorkflowTransitions:
    @pytest.mark.parametrize(("from_status", "to_status"), sorted(_EXPECTED_ALLOWED))
    def test_explicit_allowed_transitions(
        self, from_status: PublicationStatus, to_status: PublicationStatus
    ) -> None:
        assert is_allowed(from_status, to_status)
        # Should not raise.
        validate_transition(from_status, to_status)

    @pytest.mark.parametrize(
        ("from_status", "to_status"),
        sorted(
            (a, b)
            for a, b in itertools.product(PublicationStatus, PublicationStatus)
            if (a, b) not in _EXPECTED_ALLOWED
        ),
    )
    def test_every_other_pair_is_forbidden(
        self, from_status: PublicationStatus, to_status: PublicationStatus
    ) -> None:
        """Default-deny: any pair not in the allowed table must raise.

        This is the exhaustive complement of ``_EXPECTED_ALLOWED``.
        Including (X, X) self-transitions and any reachability from
        RETRACTED.
        """
        assert not is_allowed(from_status, to_status)
        with pytest.raises(InvalidPublicationTransitionError):
            validate_transition(from_status, to_status)

    def test_retracted_is_terminal(self) -> None:
        """RETRACTED has no outgoing transitions.

        Pinned as its own test because it's the contract that gives
        ARCHIVED a coherent "soft-hide" role.  If a future PR adds
        an unretract path, this test fails loudly.
        """
        assert allowed_next_states(PublicationStatus.RETRACTED) == frozenset()

    @pytest.mark.parametrize("status", list(PublicationStatus))
    def test_no_self_loops(self, status: PublicationStatus) -> None:
        """No state transitions to itself.

        Editorial edits within DRAFT use the update use case, not a
        DRAFT->DRAFT transition; this contract makes the update use
        case's "must be DRAFT" check unambiguous.
        """
        assert not is_allowed(status, status)

    def test_invalid_transition_error_carries_both_states(self) -> None:
        with pytest.raises(InvalidPublicationTransitionError) as excinfo:
            validate_transition(PublicationStatus.RETRACTED, PublicationStatus.PUBLISHED)
        assert excinfo.value.from_status == PublicationStatus.RETRACTED
        assert excinfo.value.to_status == PublicationStatus.PUBLISHED
