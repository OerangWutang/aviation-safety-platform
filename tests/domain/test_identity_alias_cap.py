from __future__ import annotations

from uuid import uuid4

import pytest

from atlas.domain.constants import MAX_REGISTRATION_ALIASES
from atlas.domain.entities import EventIdentityIndex
from tests.domain._fake_uow import InMemoryUnitOfWork

pytestmark = pytest.mark.asyncio


async def test_event_identity_index_caps_registration_norms_to_recent_unique_values():
    entry = EventIdentityIndex(
        event_id=uuid4(),
        registration_norm="reg9",
        registration_norms=["reg1", "reg2", "reg3", "reg4", "reg5", "reg6", "reg7", "reg7"],
    )

    assert len(entry.registration_norms) == MAX_REGISTRATION_ALIASES
    assert entry.registration_norms == ["reg3", "reg4", "reg5", "reg6", "reg7"]


async def test_identity_index_upsert_caps_historical_registration_aliases():
    uow = InMemoryUnitOfWork()
    event_id = uuid4()

    for idx in range(8):
        await uow.identity_index.upsert(
            EventIdentityIndex(
                event_id=event_id,
                registration_norm=f"reg{idx}",
                registration_norms=[f"reg{idx}"],
            )
        )

    entry = uow.store.identity_index[event_id]
    assert len(entry.registration_norms) == MAX_REGISTRATION_ALIASES
    assert entry.registration_norms == ["reg3", "reg4", "reg5", "reg6", "reg7"]
    assert "reg0" not in entry.registration_norms
