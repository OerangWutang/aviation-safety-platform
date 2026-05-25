from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import ValidationError

from atlas.presentation.api.schemas.admin import RebuildRequest


def test_rebuild_request_rejects_negative_max_events_other_than_unlimited() -> None:
    with pytest.raises(ValidationError, match="max_events must be -1"):
        RebuildRequest(all=True, max_events=-5)


def test_rebuild_request_rejects_both_all_and_event_id() -> None:
    with pytest.raises(ValidationError, match="Use either all=true or event_id"):
        RebuildRequest(all=True, event_id=uuid4(), max_events=-1)


def test_rebuild_request_rejects_neither_all_nor_event_id() -> None:
    with pytest.raises(ValidationError, match="Provide event_id"):
        RebuildRequest(all=False, event_id=None)


def test_rebuild_request_all_unlimited_is_valid() -> None:
    req = RebuildRequest(all=True, max_events=-1)
    assert req.max_events == -1


def test_rebuild_request_event_id_only_is_valid() -> None:
    event_id = uuid4()
    req = RebuildRequest(event_id=event_id)
    assert req.event_id == event_id
