"""Argus operational metrics — Prometheus counters and a duration histogram.

Why this lives in ``infrastructure``
------------------------------------
Use cases (in ``application/``) emit observability events; the implementation
of those events is an infrastructure concern.  Putting the Prometheus
counters here keeps the clean-architecture direction intact:
``presentation → application → infrastructure``.  Use cases call into a
thin recorder API that knows nothing about HTTP and minimal about Prometheus
— it gracefully no-ops if ``prometheus_client`` is not installed (matching
the existing pattern in ``atlas.presentation.api.metrics``).

Design notes
------------
- Counters are global (one per metric, label values applied at observation
  time) so the Prometheus registry sees a single time series per
  ``(metric, label set)`` tuple.  ``prometheus_client`` raises on duplicate
  registration, which is why we guard module-level construction.
- Labels are deliberately low-cardinality:
  ``signal_type`` is an ``ArgusSignalType`` enum (six values),
  ``decision`` is an ``ArgusReviewDecision`` enum (three values),
  ``engine`` is the engine name (currently four: chronos, hermes, atlas,
  orion).  Total cardinality stays under twenty active series.
- The recorder functions accept domain enums and stringify them at the
  call site, so callers don't import ``prometheus_client``.
- Tests use ``CollectorRegistry`` injection rather than the default registry
  so they don't pollute global state across test runs.
"""

from __future__ import annotations

import logging
from typing import Any

from atlas.domain.enums import ArgusReviewDecision, ArgusSignalType

logger = logging.getLogger(__name__)


# ── Optional Prometheus runtime dependency ───────────────────────────────────
_CounterCls: Any = None
_HistogramCls: Any = None
try:  # pragma: no cover - depends on optional runtime deps in CI image
    from prometheus_client import Counter as _PromCounter
    from prometheus_client import Histogram as _PromHistogram

    _CounterCls = _PromCounter
    _HistogramCls = _PromHistogram
except ImportError:  # pragma: no cover
    pass


# ── Metric instances ─────────────────────────────────────────────────────────
# All three are module-level singletons.  ``None`` when the prometheus_client
# library is unavailable; callers must handle that with ``if metric is not None``
# (the recorder helpers below do exactly that).

_ARGUS_SIGNALS_CREATED: Any = None
_ARGUS_SIGNAL_REVIEWS: Any = None
_ARGUS_DETECTION_DURATION: Any = None
_ARGUS_ENGINE_ERRORS: Any = None


def _build_metrics(registry: Any = None) -> None:
    """Create the metric instances against ``registry`` (or the default).

    Called once on module import.  Exposed for tests so they can rebuild
    against a fresh ``CollectorRegistry`` without contaminating the default
    registry across the suite.
    """
    global _ARGUS_SIGNALS_CREATED
    global _ARGUS_SIGNAL_REVIEWS
    global _ARGUS_DETECTION_DURATION
    global _ARGUS_ENGINE_ERRORS

    if _CounterCls is None or _HistogramCls is None:
        return

    kwargs: dict[str, Any] = {}
    if registry is not None:
        kwargs["registry"] = registry

    _ARGUS_SIGNALS_CREATED = _CounterCls(
        "argus_signals_created_total",
        "Total number of Argus signals created (not counting reuse/upsert).",
        labelnames=("signal_type",),
        **kwargs,
    )
    _ARGUS_SIGNAL_REVIEWS = _CounterCls(
        "argus_signal_reviews_total",
        "Total number of Argus signal review decisions recorded.",
        labelnames=("decision",),
        **kwargs,
    )
    _ARGUS_ENGINE_ERRORS = _CounterCls(
        "argus_engine_errors_total",
        "Number of Argus detection engine failures (one per engine per run).",
        labelnames=("engine",),
        **kwargs,
    )
    # Bucket choice: detection should be fast (single read-only DB pass per
    # engine; no external I/O).  The buckets cover sub-millisecond up to a
    # cold-cache p99; anything past 5s is alert-worthy.  We intentionally do
    # NOT pre-aggregate by engine — the use case observes a single histogram
    # for the whole run.  Per-engine timing can be added later by labelling
    # if it becomes a triage need.
    _ARGUS_DETECTION_DURATION = _HistogramCls(
        "argus_detection_duration_seconds",
        "Time spent in one RunArgusSignalDetection.execute() call.",
        buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
        **kwargs,
    )


_build_metrics()


# ── Recorder API ─────────────────────────────────────────────────────────────


def record_signal_created(signal_type: ArgusSignalType) -> None:
    """Increment ``argus_signals_created_total{signal_type=...}`` by 1.

    Called once per *newly-created* signal (not on reuse/upsert), so the
    counter reflects the rate at which Argus is discovering new problems.
    """
    if _ARGUS_SIGNALS_CREATED is None:
        return
    try:
        _ARGUS_SIGNALS_CREATED.labels(signal_type=signal_type.value).inc()
    except Exception:  # pragma: no cover - defensive: never fail a use case for a metric
        logger.exception("Failed to record argus_signals_created_total")


def record_signal_review(decision: ArgusReviewDecision) -> None:
    """Increment ``argus_signal_reviews_total{decision=...}`` by 1.

    Called by ``ReviewArgusSignal.execute`` after a successful version-
    checked update.  Race-losers are NOT counted (the review row was rolled
    back and the reviewer will retry).
    """
    if _ARGUS_SIGNAL_REVIEWS is None:
        return
    try:
        _ARGUS_SIGNAL_REVIEWS.labels(decision=decision.value).inc()
    except Exception:  # pragma: no cover
        logger.exception("Failed to record argus_signal_reviews_total")


def record_engine_error(engine: str) -> None:
    """Increment ``argus_engine_errors_total{engine=...}`` by 1.

    Mirrors the ``engines_errored`` list in ``ArgusDetectionResult`` so a
    1-to-1 correspondence holds between API responses and metrics.
    """
    if _ARGUS_ENGINE_ERRORS is None:
        return
    try:
        _ARGUS_ENGINE_ERRORS.labels(engine=engine).inc()
    except Exception:  # pragma: no cover
        logger.exception("Failed to record argus_engine_errors_total")


def observe_detection_duration(seconds: float) -> None:
    """Observe a value on ``argus_detection_duration_seconds``."""
    if _ARGUS_DETECTION_DURATION is None:
        return
    try:
        _ARGUS_DETECTION_DURATION.observe(seconds)
    except Exception:  # pragma: no cover
        logger.exception("Failed to observe argus_detection_duration_seconds")
