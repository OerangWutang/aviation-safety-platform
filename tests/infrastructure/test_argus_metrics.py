"""Unit tests for atlas.infrastructure.observability.argus_metrics.

Each test builds the counters against a *fresh* ``CollectorRegistry`` so the
default registry stays unmodified across the suite — Prometheus collectors
raise ``ValueError`` on duplicate registration, and pytest test isolation
matters here more than in most modules.
"""

from __future__ import annotations

import pytest
from prometheus_client import CollectorRegistry

from atlas.domain.enums import ArgusReviewDecision, ArgusSignalType
from atlas.infrastructure.observability import argus_metrics


@pytest.fixture
def fresh_registry():
    """Rebuild the module's counters against an isolated registry.

    Restores the default-registry counters afterwards so other tests (or a
    subsequent call to the live ``/metrics`` endpoint) keep their global
    series intact.
    """
    saved = (
        argus_metrics._ARGUS_SIGNALS_CREATED,
        argus_metrics._ARGUS_SIGNAL_REVIEWS,
        argus_metrics._ARGUS_ENGINE_ERRORS,
        argus_metrics._ARGUS_DETECTION_DURATION,
    )
    registry = CollectorRegistry()
    argus_metrics._build_metrics(registry=registry)
    try:
        yield registry
    finally:
        (
            argus_metrics._ARGUS_SIGNALS_CREATED,
            argus_metrics._ARGUS_SIGNAL_REVIEWS,
            argus_metrics._ARGUS_ENGINE_ERRORS,
            argus_metrics._ARGUS_DETECTION_DURATION,
        ) = saved


def _counter_value(registry: CollectorRegistry, metric: str, labels: dict[str, str]) -> float:
    """Read a Prometheus counter sample from a registry, or 0.0 if not yet observed."""
    val = registry.get_sample_value(metric + "_total", labels)
    return val if val is not None else 0.0


def test_record_signal_created_increments_by_signal_type(fresh_registry):
    argus_metrics.record_signal_created(ArgusSignalType.NEW_SOURCE_CHANGE)
    argus_metrics.record_signal_created(ArgusSignalType.NEW_SOURCE_CHANGE)
    argus_metrics.record_signal_created(ArgusSignalType.TIMELINE_SEQUENCE_CONFLICT)

    assert (
        _counter_value(
            fresh_registry, "argus_signals_created", {"signal_type": "NEW_SOURCE_CHANGE"}
        )
        == 2.0
    )
    assert (
        _counter_value(
            fresh_registry,
            "argus_signals_created",
            {"signal_type": "TIMELINE_SEQUENCE_CONFLICT"},
        )
        == 1.0
    )
    # Unobserved labels stay 0 — they're not registered as series yet.
    assert (
        _counter_value(
            fresh_registry, "argus_signals_created", {"signal_type": "SOURCE_FETCH_FAILURE_SPIKE"}
        )
        == 0.0
    )


def test_record_signal_review_increments_by_decision(fresh_registry):
    argus_metrics.record_signal_review(ArgusReviewDecision.CONFIRMED)
    argus_metrics.record_signal_review(ArgusReviewDecision.DISMISSED)
    argus_metrics.record_signal_review(ArgusReviewDecision.CONFIRMED)

    assert _counter_value(fresh_registry, "argus_signal_reviews", {"decision": "CONFIRMED"}) == 2.0
    assert _counter_value(fresh_registry, "argus_signal_reviews", {"decision": "DISMISSED"}) == 1.0
    assert (
        _counter_value(fresh_registry, "argus_signal_reviews", {"decision": "NEEDS_MORE_REVIEW"})
        == 0.0
    )


def test_record_engine_error_increments_by_engine(fresh_registry):
    argus_metrics.record_engine_error("chronos")
    argus_metrics.record_engine_error("hermes")
    argus_metrics.record_engine_error("chronos")

    assert _counter_value(fresh_registry, "argus_engine_errors", {"engine": "chronos"}) == 2.0
    assert _counter_value(fresh_registry, "argus_engine_errors", {"engine": "hermes"}) == 1.0
    # An engine that has never errored has no time series — a reasonable
    # default for Prometheus alert rules (``rate(... [5m]) > 0`` won't fire
    # spuriously on absent series).
    assert _counter_value(fresh_registry, "argus_engine_errors", {"engine": "atlas"}) == 0.0


def test_observe_detection_duration_records_in_histogram(fresh_registry):
    argus_metrics.observe_detection_duration(0.123)
    argus_metrics.observe_detection_duration(2.0)

    sum_value = fresh_registry.get_sample_value("argus_detection_duration_seconds_sum")
    count_value = fresh_registry.get_sample_value("argus_detection_duration_seconds_count")
    assert sum_value is not None and abs(sum_value - 2.123) < 1e-9
    assert count_value == 2.0

    # Bucket counts are monotonic — every observation lands in a "<= X"
    # bucket greater than or equal to its value.  Spot-check the 2.5s bucket
    # captures both observations.
    bucket_le_2_5 = fresh_registry.get_sample_value(
        "argus_detection_duration_seconds_bucket", {"le": "2.5"}
    )
    assert bucket_le_2_5 == 2.0


def test_recorders_no_op_when_metric_is_none(monkeypatch):
    """The optional-Prometheus-dependency contract: when the metric is
    ``None`` (library missing), the recorders silently no-op instead of
    raising — so a use-case path stays correct in CI images without
    prometheus_client installed.
    """
    monkeypatch.setattr(argus_metrics, "_ARGUS_SIGNALS_CREATED", None)
    monkeypatch.setattr(argus_metrics, "_ARGUS_SIGNAL_REVIEWS", None)
    monkeypatch.setattr(argus_metrics, "_ARGUS_ENGINE_ERRORS", None)
    monkeypatch.setattr(argus_metrics, "_ARGUS_DETECTION_DURATION", None)

    # None of these should raise.
    argus_metrics.record_signal_created(ArgusSignalType.NEW_SOURCE_CHANGE)
    argus_metrics.record_signal_review(ArgusReviewDecision.CONFIRMED)
    argus_metrics.record_engine_error("chronos")
    argus_metrics.observe_detection_duration(0.1)


def test_recorders_swallow_unexpected_exceptions(monkeypatch, fresh_registry):
    """Defence in depth: if Prometheus raises (e.g. registry corruption) the
    recorder must not propagate — a metric must never break a use case.
    """

    class _Broken:
        def labels(self, **_kwargs):
            raise RuntimeError("simulated registry failure")

        def observe(self, _value):
            raise RuntimeError("simulated histogram failure")

    monkeypatch.setattr(argus_metrics, "_ARGUS_SIGNALS_CREATED", _Broken())
    monkeypatch.setattr(argus_metrics, "_ARGUS_SIGNAL_REVIEWS", _Broken())
    monkeypatch.setattr(argus_metrics, "_ARGUS_ENGINE_ERRORS", _Broken())
    monkeypatch.setattr(argus_metrics, "_ARGUS_DETECTION_DURATION", _Broken())

    argus_metrics.record_signal_created(ArgusSignalType.NEW_SOURCE_CHANGE)
    argus_metrics.record_signal_review(ArgusReviewDecision.CONFIRMED)
    argus_metrics.record_engine_error("chronos")
    argus_metrics.observe_detection_duration(0.1)
