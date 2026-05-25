"""Echo operational metrics — Prometheus counters, gauges, and histograms.

Follows the same pattern as ``argus_metrics``:

- Module-level singletons, None when prometheus_client is unavailable.
- Recorder functions accept plain Python types; callers don't import
  prometheus_client.
- Gracefully no-ops rather than failing a use case for a metric.
- Tests inject a fresh CollectorRegistry to avoid global state leakage.

Metrics exposed
---------------
echo_crossref_runs_total{status}
    Total completed cross-reference runs labelled by terminal status
    (``complete`` or ``failed``).  Counter, never decrements.

echo_corpus_size
    Number of PrecedentRecords in the most recently loaded corpus.
    Gauge, updated on every cache miss (i.e. every fresh load).

echo_corpus_load_duration_seconds
    Histogram of time spent loading the public precedent corpus
    (the ~8 s full-table scan).  Only observed on cache misses.

echo_matching_duration_seconds
    Histogram of time spent in PrecedentMatcher.rank() — the pure
    in-memory matching step after the corpus is available.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# ── Optional Prometheus runtime dependency ───────────────────────────────────
_CounterCls: Any = None
_GaugeCls: Any = None
_HistogramCls: Any = None
try:  # pragma: no cover - optional runtime dep
    from prometheus_client import Counter as _PromCounter
    from prometheus_client import Gauge as _PromGauge
    from prometheus_client import Histogram as _PromHistogram

    _CounterCls = _PromCounter
    _GaugeCls = _PromGauge
    _HistogramCls = _PromHistogram
except ImportError:  # pragma: no cover
    pass

# ── Metric instances ─────────────────────────────────────────────────────────
_ECHO_RUNS: Any = None
_ECHO_CORPUS_SIZE: Any = None
_ECHO_CORPUS_LOAD_DURATION: Any = None
_ECHO_MATCHING_DURATION: Any = None


def _build_metrics(registry: Any = None) -> None:
    """Create metric instances against ``registry`` (or the default).

    Exposed for tests so they can rebuild against a fresh CollectorRegistry.
    """
    global _ECHO_RUNS, _ECHO_CORPUS_SIZE
    global _ECHO_CORPUS_LOAD_DURATION, _ECHO_MATCHING_DURATION

    if _CounterCls is None or _GaugeCls is None or _HistogramCls is None:
        return

    kwargs: dict[str, Any] = {}
    if registry is not None:
        kwargs["registry"] = registry

    _ECHO_RUNS = _CounterCls(
        "echo_crossref_runs_total",
        "Total completed Echo cross-reference runs by terminal status.",
        labelnames=("status",),
        **kwargs,
    )
    _ECHO_CORPUS_SIZE = _GaugeCls(
        "echo_corpus_size",
        "Number of PrecedentRecords in the most recently loaded corpus.",
        **kwargs,
    )
    # Corpus load buckets: the full table scan takes ~8 s for 30 k events on
    # a warm DB; buckets cover from a fast warmed-cache case (unlikely, corpus
    # loader bypasses cache) up to a slow cold-cache scan.
    _ECHO_CORPUS_LOAD_DURATION = _HistogramCls(
        "echo_corpus_load_duration_seconds",
        "Time spent loading the public precedent corpus (cache-miss path only).",
        buckets=(1.0, 2.5, 5.0, 7.5, 10.0, 15.0, 30.0),
        **kwargs,
    )
    # Matching buckets: pure in-memory ranking; should be sub-second.
    _ECHO_MATCHING_DURATION = _HistogramCls(
        "echo_matching_duration_seconds",
        "Time spent in PrecedentMatcher.rank() for one cross-reference run.",
        buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0),
        **kwargs,
    )


_build_metrics()


# ── Recorder API ─────────────────────────────────────────────────────────────


def record_run_complete() -> None:
    """Increment ``echo_crossref_runs_total{status='complete'}``."""
    if _ECHO_RUNS is None:
        return
    try:
        _ECHO_RUNS.labels(status="complete").inc()
    except Exception:  # pragma: no cover
        logger.exception("Failed to record echo_crossref_runs_total{complete}")


def record_run_failed() -> None:
    """Increment ``echo_crossref_runs_total{status='failed'}``."""
    if _ECHO_RUNS is None:
        return
    try:
        _ECHO_RUNS.labels(status="failed").inc()
    except Exception:  # pragma: no cover
        logger.exception("Failed to record echo_crossref_runs_total{failed}")


def record_corpus_loaded(size: int, duration_seconds: float) -> None:
    """Update the corpus-size gauge and observe a corpus-load duration.

    Only call this on a genuine cache miss (fresh DB load), not on cache hits.
    """
    if _ECHO_CORPUS_SIZE is not None:
        try:
            _ECHO_CORPUS_SIZE.set(size)
        except Exception:  # pragma: no cover
            logger.exception("Failed to set echo_corpus_size")
    if _ECHO_CORPUS_LOAD_DURATION is not None:
        try:
            _ECHO_CORPUS_LOAD_DURATION.observe(duration_seconds)
        except Exception:  # pragma: no cover
            logger.exception("Failed to observe echo_corpus_load_duration_seconds")


def observe_matching_duration(seconds: float) -> None:
    """Observe a value on ``echo_matching_duration_seconds``."""
    if _ECHO_MATCHING_DURATION is None:
        return
    try:
        _ECHO_MATCHING_DURATION.observe(seconds)
    except Exception:  # pragma: no cover
        logger.exception("Failed to observe echo_matching_duration_seconds")
