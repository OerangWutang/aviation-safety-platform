from __future__ import annotations

import asyncio
import ipaddress
import logging
import secrets
import time
from typing import TYPE_CHECKING, Any

from fastapi import Request, Response

from atlas.application.use_cases.query_operational_metrics import QueryOperationalMetrics
from atlas.config import get_settings
from atlas.infrastructure.db.session import async_session_factory
from atlas.infrastructure.db.unit_of_work import SqlAlchemyUnitOfWork

if TYPE_CHECKING:
    from fastapi import FastAPI

logger = logging.getLogger(__name__)
_domain_metrics_lock = asyncio.Lock()
_domain_metrics_last_refresh_monotonic = 0.0

# Optional runtime dependencies. We deliberately do not reassign the imported
# *type* symbols to ``None`` on ImportError because that fights strict typing.
# Instead we expose lower-case handles that may be ``None`` when the optional
# library is absent (CI image without prometheus extras, smoke environments).
_CONTENT_TYPE_LATEST = "text/plain; version=0.0.4; charset=utf-8"
_GaugeCls: Any = None
_Instrumentator: Any = None
_generate_latest: Any = None
try:  # pragma: no cover - availability depends on optional runtime deps in CI image
    from prometheus_client import CONTENT_TYPE_LATEST as _CT_LATEST
    from prometheus_client import Gauge as _PromGauge
    from prometheus_client import generate_latest as _prom_generate_latest
    from prometheus_fastapi_instrumentator import Instrumentator as _PromInstrumentator

    _CONTENT_TYPE_LATEST = _CT_LATEST
    _GaugeCls = _PromGauge
    _Instrumentator = _PromInstrumentator
    _generate_latest = _prom_generate_latest
except ImportError:  # pragma: no cover
    pass


_OUTBOX_EVENTS: Any = None
_CONFLICTS: Any = None
_CLAIMS_TOTAL: Any = None
_PROJECTED_EVENTS_TOTAL: Any = None
_OUTBOX_OLDEST_UNPROCESSED_AGE: Any = None
_WORKER_HEARTBEAT_PRESENT: Any = None
_WORKER_HEARTBEAT_AGE: Any = None
_WORKER_SUCCESSFUL_BATCH_PRESENT: Any = None
_WORKER_SUCCESSFUL_BATCH_AGE: Any = None
_DOMAIN_REFRESH_SUCCESS: Any = None
_DOMAIN_REFRESH_TIMESTAMP: Any = None

if _GaugeCls is not None:
    _OUTBOX_EVENTS = _GaugeCls(
        "atlas_outbox_events_total",
        "Atlas outbox events by status.",
        labelnames=("status",),
    )
    _CONFLICTS = _GaugeCls(
        "atlas_conflicts_total",
        "Atlas claim conflicts by status.",
        labelnames=("status",),
    )
    _CLAIMS_TOTAL = _GaugeCls("atlas_claims_total", "Total claim rows.")
    _PROJECTED_EVENTS_TOTAL = _GaugeCls(
        "atlas_projected_events_total",
        "Total projected accident records.",
    )
    _OUTBOX_OLDEST_UNPROCESSED_AGE = _GaugeCls(
        "atlas_outbox_oldest_unprocessed_age_seconds",
        "Age in seconds of the oldest PENDING/PROCESSING/FAILED outbox row; 0 means no backlog.",
    )
    _WORKER_HEARTBEAT_PRESENT = _GaugeCls(
        "atlas_outbox_worker_heartbeat_present",
        "1 when any outbox worker has recorded a heartbeat, 0 when no heartbeat row exists yet.",
    )
    _WORKER_HEARTBEAT_AGE = _GaugeCls(
        "atlas_outbox_worker_heartbeat_age_seconds",
        "Age in seconds since the newest outbox worker loop heartbeat; omit when no heartbeat exists (see _present gauge).",
    )
    _WORKER_SUCCESSFUL_BATCH_PRESENT = _GaugeCls(
        "atlas_outbox_worker_last_successful_batch_present",
        "1 when any outbox worker has completed a non-empty batch, 0 when none has done so yet.",
    )
    _WORKER_SUCCESSFUL_BATCH_AGE = _GaugeCls(
        "atlas_outbox_worker_successful_batch_age_seconds",
        "Age in seconds since any outbox worker last completed a non-empty batch; omit when none has done so (see _present gauge).",
    )
    _DOMAIN_REFRESH_SUCCESS = _GaugeCls(
        "atlas_operational_metrics_refresh_success",
        "1 when the last DB-backed metrics refresh succeeded, 0 otherwise.",
    )
    _DOMAIN_REFRESH_TIMESTAMP = _GaugeCls(
        "atlas_operational_metrics_last_refresh_timestamp_seconds",
        "Unix timestamp of the last successful DB-backed metrics refresh.",
    )


async def _refresh_domain_gauges() -> None:
    """Refresh Atlas-specific gauges with one short DB transaction.

    The Prometheus endpoint is intentionally not wired through the request UoW
    dependency: scrapes should not hold the same session lifecycle as API
    handlers and should fail independently of business requests. A small TTL
    prevents frequent scrape intervals from repeatedly running full table
    counts on large installations.
    """
    global _domain_metrics_last_refresh_monotonic
    if _GaugeCls is None:
        return

    settings = get_settings()
    ttl = settings.prometheus_domain_metrics_ttl_seconds
    now_monotonic = time.monotonic()
    if ttl > 0 and now_monotonic - _domain_metrics_last_refresh_monotonic < ttl:
        return

    async with _domain_metrics_lock:
        now_monotonic = time.monotonic()
        if ttl > 0 and now_monotonic - _domain_metrics_last_refresh_monotonic < ttl:
            return

        async with async_session_factory() as session:
            uow = SqlAlchemyUnitOfWork(session)
            try:
                include_expensive = settings.prometheus_expensive_domain_metrics_enabled
                metrics = await QueryOperationalMetrics(uow).execute(
                    include_expensive_totals=include_expensive
                )
                _OUTBOX_EVENTS.labels("pending").set(metrics.outbox_pending)
                _OUTBOX_EVENTS.labels("processing").set(metrics.outbox_processing)
                _OUTBOX_EVENTS.labels("failed").set(metrics.outbox_failed)
                _OUTBOX_EVENTS.labels("dead_letter").set(metrics.outbox_dead_letter)
                _CONFLICTS.labels("open").set(metrics.conflicts_open)
                _OUTBOX_OLDEST_UNPROCESSED_AGE.set(
                    metrics.outbox_oldest_unprocessed_age_seconds or 0
                )
                heartbeat_age = metrics.worker_heartbeat_age_seconds
                if heartbeat_age is None:
                    _WORKER_HEARTBEAT_PRESENT.set(0)
                    _WORKER_HEARTBEAT_AGE.set(-1)
                else:
                    _WORKER_HEARTBEAT_PRESENT.set(1)
                    _WORKER_HEARTBEAT_AGE.set(heartbeat_age)
                batch_age = metrics.worker_successful_batch_age_seconds
                if batch_age is None:
                    _WORKER_SUCCESSFUL_BATCH_PRESENT.set(0)
                    _WORKER_SUCCESSFUL_BATCH_AGE.set(-1)
                else:
                    _WORKER_SUCCESSFUL_BATCH_PRESENT.set(1)
                    _WORKER_SUCCESSFUL_BATCH_AGE.set(batch_age)
                if include_expensive:
                    _OUTBOX_EVENTS.labels("processed").set(metrics.outbox_processed)
                    _CONFLICTS.labels("resolved").set(metrics.conflicts_resolved)
                    _CLAIMS_TOTAL.set(metrics.total_claims)
                    _PROJECTED_EVENTS_TOTAL.set(metrics.total_projected_events)
                _DOMAIN_REFRESH_SUCCESS.set(1)
                _DOMAIN_REFRESH_TIMESTAMP.set(time.time())
                _domain_metrics_last_refresh_monotonic = time.monotonic()
                await uow.rollback()
            except Exception:
                _DOMAIN_REFRESH_SUCCESS.set(0)
                await uow.rollback()
                logger.exception("Failed to refresh DB-backed Prometheus gauges")


def _metrics_request_allowed(request: Request) -> bool:
    settings = get_settings()

    if settings.prometheus_bearer_token:
        expected = f"Bearer {settings.prometheus_bearer_token}"
        provided = request.headers.get("authorization", "")
        if secrets.compare_digest(provided, expected):
            return True

    client_host = request.client.host if request.client else None
    if client_host is None:
        return False

    try:
        client_ip = ipaddress.ip_address(client_host)
    except ValueError:
        return False

    for cidr in settings.prometheus_allowed_cidrs:
        try:
            if client_ip in ipaddress.ip_network(cidr, strict=False):
                return True
        except ValueError:
            logger.warning("Ignoring invalid PROMETHEUS_ALLOWED_CIDRS entry: %s", cidr)
    return False


async def prometheus_endpoint(request: Request) -> Response:
    settings = get_settings()
    if not settings.prometheus_metrics_enabled:
        return Response(status_code=404)
    if not _metrics_request_allowed(request):
        # Return 404 rather than 403 so public scanners cannot trivially
        # distinguish a protected metrics endpoint from no metrics endpoint.
        return Response(status_code=404)
    if _generate_latest is None:
        return Response(
            "prometheus dependencies are not installed\n",
            status_code=503,
            media_type="text/plain",
        )
    if settings.prometheus_domain_metrics_enabled:
        await _refresh_domain_gauges()
    return Response(content=_generate_latest(), media_type=_CONTENT_TYPE_LATEST)


def install_prometheus(app: FastAPI) -> None:
    """Install HTTP and domain metrics without exposing SQLAlchemy to routers."""
    settings = get_settings()
    if not settings.prometheus_metrics_enabled:
        return

    if _Instrumentator is not None:
        _Instrumentator(
            excluded_handlers=["/health", "/ready", "/metrics"],
            should_group_status_codes=True,
            should_ignore_untemplated=True,
        ).instrument(app)
    else:  # pragma: no cover
        logger.warning(
            "PROMETHEUS_METRICS_ENABLED=true but prometheus-fastapi-instrumentator "
            "is not installed; /metrics will return 503."
        )

    app.add_api_route(
        "/metrics",
        prometheus_endpoint,
        methods=["GET"],
        include_in_schema=False,
    )
