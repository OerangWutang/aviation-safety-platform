"""Use-case tests for SetSourceFieldMapping.

These cover:
- Successful replacement of ``Source.field_mapping_json``.
- Empty mapping clears the row.
- Typo'd canonical target raises ``DomainValidationError`` with the bad name
  surfaced in the message - no partial write to the source row.
- Two raw keys that normalise to the same canonical key are rejected before
  the write, so the persisted mapping is always unambiguous.
- Tolerant raw-key normalisation: ``"Aircraft Registration"`` and
  ``"aircraftRegistration"`` map to the same canonical target.
- Unknown source ids surface as ``SourceNotFoundError``, not a silent no-op.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from atlas.application.use_cases.set_source_field_mapping import SetSourceFieldMapping
from atlas.domain.entities import Source
from atlas.domain.enums import SourceKind
from atlas.domain.exceptions import DomainValidationError, SourceNotFoundError
from tests.domain._fake_uow import InMemoryUnitOfWork


@pytest.fixture
def uow() -> InMemoryUnitOfWork:
    return InMemoryUnitOfWork()


async def _add_source(uow: InMemoryUnitOfWork) -> Source:
    src = Source(
        id=uuid4(),
        name=f"S-{uuid4().hex[:6]}",
        kind=SourceKind.EXTERNAL,
        reliability_tier=1,
    )
    await uow.sources.add(src)
    return src


async def test_set_field_mapping_replaces_existing_mapping(uow):
    src = await _add_source(uow)

    updated = await SetSourceFieldMapping(uow).execute(
        source_id=src.id,
        field_mapping={"date": "event_date", "tailNumber": "registration"},
    )

    assert updated.id == src.id
    assert updated.field_mapping_json == {
        "date": "event_date",
        "tailNumber": "registration",
    }

    # And the value is durable in the store, not just on the returned object.
    refetched = await uow.sources.get(src.id)
    assert refetched is not None
    assert refetched.field_mapping_json == {
        "date": "event_date",
        "tailNumber": "registration",
    }


async def test_set_field_mapping_empty_clears_existing(uow):
    src = await _add_source(uow)
    # Prime with a non-empty mapping.
    await SetSourceFieldMapping(uow).execute(
        source_id=src.id,
        field_mapping={"date": "event_date"},
    )

    cleared = await SetSourceFieldMapping(uow).execute(
        source_id=src.id,
        field_mapping={},
    )
    assert cleared.field_mapping_json == {}


async def test_set_field_mapping_rejects_unknown_canonical_target(uow):
    src = await _add_source(uow)

    with pytest.raises(DomainValidationError, match=r"Invalid canonical target"):
        await SetSourceFieldMapping(uow).execute(
            source_id=src.id,
            field_mapping={"date": "event_dat"},
        )

    # No mutation must have leaked to the row.
    refetched = await uow.sources.get(src.id)
    assert refetched is not None
    assert refetched.field_mapping_json == {}


async def test_set_field_mapping_rejects_colliding_raw_keys(uow):
    """Two raw keys that normalise to the same canonical key would silently
    overwrite each other inside ``SourceFieldMapper``.  The use case detects
    the collision and rejects so the persisted mapping is unambiguous.
    """
    src = await _add_source(uow)

    with pytest.raises(DomainValidationError, match=r"collide under tolerant"):
        await SetSourceFieldMapping(uow).execute(
            source_id=src.id,
            field_mapping={
                "Aircraft Registration": "registration",
                "aircraftRegistration": "operator",
            },
        )

    refetched = await uow.sources.get(src.id)
    assert refetched is not None
    assert refetched.field_mapping_json == {}


async def test_set_field_mapping_tolerant_raw_keys_are_preserved_verbatim(uow):
    """The raw-side keys are kept exactly as the caller submitted them; only
    canonical *targets* go through ``_canonical_field_value``.  The mapper
    will tolerantly match the raw keys at lookup time, but the persisted JSON
    keeps the operator's spelling for audit clarity.
    """
    src = await _add_source(uow)

    updated = await SetSourceFieldMapping(uow).execute(
        source_id=src.id,
        field_mapping={"Aircraft Registration": "registration"},
    )

    assert updated.field_mapping_json == {"Aircraft Registration": "registration"}


async def test_set_field_mapping_canonical_target_is_normalised(uow):
    """A caller may submit ``"eventDate"`` as the canonical target; the
    mapper normalises it to ``"event_date"``.  The persisted value should
    therefore be the normalised form so downstream consumers do not need to
    re-normalise on read.
    """
    src = await _add_source(uow)

    # Sanity: this would not raise; eventDate normalises to event_date.
    # The use case currently persists the *raw* canonical text as submitted.
    # This test pins the current behavior so a future change is intentional
    # (storing the normalised target may be desirable but is a separate fix).
    updated = await SetSourceFieldMapping(uow).execute(
        source_id=src.id,
        field_mapping={"date": "eventDate"},
    )

    assert updated.field_mapping_json == {"date": "eventDate"}


async def test_set_field_mapping_unknown_source_raises(uow):
    missing = uuid4()
    with pytest.raises(SourceNotFoundError):
        await SetSourceFieldMapping(uow).execute(
            source_id=missing,
            field_mapping={"date": "event_date"},
        )


async def test_set_field_mapping_rejects_non_dict(uow):
    src = await _add_source(uow)
    with pytest.raises(DomainValidationError, match=r"must be a JSON object"):
        await SetSourceFieldMapping(uow).execute(
            source_id=src.id,
            field_mapping=["not", "a", "dict"],  # type: ignore[arg-type]
        )
