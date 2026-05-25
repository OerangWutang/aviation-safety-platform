from uuid import uuid4

import pytest

from atlas.domain.entities import ClaimConflict
from atlas.domain.enums import ConflictModifierReason, ConflictStatus
from atlas.domain.exceptions import ClaimNotInConflictError, ConflictAlreadyResolvedError


def test_resolve_increments_version_and_sets_winner():
    claim_id = uuid4()
    conflict = ClaimConflict(event_id=uuid4(), field_name="fatalities_total", claim_ids=[claim_id])

    conflict.resolve(claim_id, resolved_by=uuid4(), reason="official report")

    assert conflict.status == ConflictStatus.RESOLVED
    assert conflict.version == 2
    assert conflict.winning_claim_id == claim_id
    assert conflict.last_modified_reason == ConflictModifierReason.USER_RESOLVED


def test_resolve_rejects_winning_claim_outside_conflict():
    conflict = ClaimConflict(event_id=uuid4(), field_name="fatalities_total", claim_ids=[uuid4()])

    with pytest.raises(ClaimNotInConflictError):
        conflict.resolve(uuid4(), resolved_by=uuid4())


def test_resolve_rejects_already_resolved_conflict():
    claim_id = uuid4()
    conflict = ClaimConflict(event_id=uuid4(), field_name="fatalities_total", claim_ids=[claim_id])
    conflict.resolve(claim_id, resolved_by=uuid4())

    with pytest.raises(ConflictAlreadyResolvedError):
        conflict.resolve(claim_id, resolved_by=uuid4())


def test_reopen_for_new_evidence_increments_version_and_clears_winner():
    claim_id = uuid4()
    conflict = ClaimConflict(event_id=uuid4(), field_name="fatalities_total", claim_ids=[claim_id])
    conflict.resolve(claim_id, resolved_by=uuid4())

    conflict.reopen_for_new_evidence()

    assert conflict.status == ConflictStatus.OPEN
    assert conflict.version == 3
    assert conflict.winning_claim_id is None
    assert conflict.resolved_by is None
    assert conflict.last_modified_reason == ConflictModifierReason.NEW_EVIDENCE
