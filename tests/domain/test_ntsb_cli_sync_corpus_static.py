from __future__ import annotations

from pathlib import Path

NTSB_CLI = Path("src/atlas/presentation/cli/ntsb.py")


def test_sync_corpus_uses_session_objects_not_double_called_factories() -> None:
    source = NTSB_CLI.read_text()

    assert "async_public_session_factory()()" not in source
    assert "async_session_factory()()" not in source
    assert "async with async_public_session_factory() as pub_session" in source
    assert "async with async_session_factory() as sms_session" in source


def test_sync_corpus_copies_actual_projection_columns_and_parent_events() -> None:
    source = NTSB_CLI.read_text()

    assert '"confidence_band"' not in source
    assert '"projection_version"' in source
    assert '"completeness_score"' in source
    assert "AccidentEventModel" in source
    assert "on_conflict_do_nothing" in source
