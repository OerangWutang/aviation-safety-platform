"""Unit tests for Echo operational metrics.

Follows the same pattern as ``test_argus_metrics.py``:
- Each test uses a fresh ``CollectorRegistry`` so the global registry is
  never polluted across the suite.
- Tests verify the recorder functions increment/observe the right series.
- Tests verify graceful no-op when prometheus_client is unavailable
  (simulated by temporarily setting module globals to None).
"""

from __future__ import annotations

import pytest

pytest.importorskip("prometheus_client")
from prometheus_client import CollectorRegistry

from atlas.infrastructure.observability import echo_metrics


@pytest.fixture
def fresh_registry():
    """Rebuild Echo counters against an isolated registry; restore afterwards."""
    saved = (
        echo_metrics._ECHO_RUNS,
        echo_metrics._ECHO_CORPUS_SIZE,
        echo_metrics._ECHO_CORPUS_LOAD_DURATION,
        echo_metrics._ECHO_MATCHING_DURATION,
    )
    registry = CollectorRegistry()
    echo_metrics._build_metrics(registry=registry)
    try:
        yield registry
    finally:
        (
            echo_metrics._ECHO_RUNS,
            echo_metrics._ECHO_CORPUS_SIZE,
            echo_metrics._ECHO_CORPUS_LOAD_DURATION,
            echo_metrics._ECHO_MATCHING_DURATION,
        ) = saved


def _counter(registry: CollectorRegistry, name: str, labels: dict) -> float:
    val = registry.get_sample_value(name + "_total", labels)
    return val if val is not None else 0.0


def _gauge(registry: CollectorRegistry, name: str) -> float:
    val = registry.get_sample_value(name, {})
    return val if val is not None else 0.0


def test_record_run_complete_increments_counter(fresh_registry):
    assert _counter(fresh_registry, "echo_crossref_runs", {"status": "complete"}) == 0.0
    echo_metrics.record_run_complete()
    echo_metrics.record_run_complete()
    assert _counter(fresh_registry, "echo_crossref_runs", {"status": "complete"}) == 2.0
    assert _counter(fresh_registry, "echo_crossref_runs", {"status": "failed"}) == 0.0


def test_record_run_failed_increments_counter(fresh_registry):
    assert _counter(fresh_registry, "echo_crossref_runs", {"status": "failed"}) == 0.0
    echo_metrics.record_run_failed()
    assert _counter(fresh_registry, "echo_crossref_runs", {"status": "failed"}) == 1.0
    assert _counter(fresh_registry, "echo_crossref_runs", {"status": "complete"}) == 0.0


def test_record_corpus_loaded_sets_gauge_and_histogram(fresh_registry):
    echo_metrics.record_corpus_loaded(size=30516, duration_seconds=8.3)
    assert _gauge(fresh_registry, "echo_corpus_size") == 30516.0
    # Histogram sum should reflect the observed duration.
    sum_val = fresh_registry.get_sample_value("echo_corpus_load_duration_seconds_sum", {})
    assert sum_val is not None
    assert abs(sum_val - 8.3) < 0.01


def test_observe_matching_duration_populates_histogram(fresh_registry):
    echo_metrics.observe_matching_duration(0.18)
    sum_val = fresh_registry.get_sample_value("echo_matching_duration_seconds_sum", {})
    assert sum_val is not None
    assert abs(sum_val - 0.18) < 0.001


def test_recorders_noop_when_metrics_unavailable(fresh_registry):
    """All recorders must survive None globals without raising."""
    saved = (
        echo_metrics._ECHO_RUNS,
        echo_metrics._ECHO_CORPUS_SIZE,
        echo_metrics._ECHO_CORPUS_LOAD_DURATION,
        echo_metrics._ECHO_MATCHING_DURATION,
    )
    try:
        echo_metrics._ECHO_RUNS = None
        echo_metrics._ECHO_CORPUS_SIZE = None
        echo_metrics._ECHO_CORPUS_LOAD_DURATION = None
        echo_metrics._ECHO_MATCHING_DURATION = None

        # None of these should raise.
        echo_metrics.record_run_complete()
        echo_metrics.record_run_failed()
        echo_metrics.record_corpus_loaded(100, 1.0)
        echo_metrics.observe_matching_duration(0.05)
    finally:
        (
            echo_metrics._ECHO_RUNS,
            echo_metrics._ECHO_CORPUS_SIZE,
            echo_metrics._ECHO_CORPUS_LOAD_DURATION,
            echo_metrics._ECHO_MATCHING_DURATION,
        ) = saved
