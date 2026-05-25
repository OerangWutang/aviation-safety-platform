"""Gunicorn production configuration for Atlas.

Gunicorn owns process supervision; Uvicorn handles ASGI inside each worker.
All values are overridable through environment variables so the same image can
run on a laptop, a VM, or Kubernetes without baking deployment policy into the
container.
"""

from __future__ import annotations

import multiprocessing
import os


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return int(raw)


bind = os.getenv("GUNICORN_BIND", "0.0.0.0:8000")
worker_class = os.getenv("GUNICORN_WORKER_CLASS", "uvicorn.workers.UvicornWorker")

# Baseline recommendation for multi-process services. Async I/O means a single
# worker can multiplex many idle connections, but multiple workers still protect
# against CPU-bound stalls during validation/serialization and give zero-downtime
# rolling restarts inside one container.
workers = _int_env("WEB_CONCURRENCY", (multiprocessing.cpu_count() * 2) + 1)
threads = _int_env("GUNICORN_THREADS", 1)

# Keep-alive should be lower than the upstream load balancer idle timeout. For
# AWS ALB's common 60s default, 55s prevents the LB from reusing a socket the app
# is about to close. Override per environment when the LB timeout differs.
keepalive = _int_env("GUNICORN_KEEPALIVE_SECONDS", 55)
timeout = _int_env("GUNICORN_TIMEOUT_SECONDS", 60)
graceful_timeout = _int_env("GUNICORN_GRACEFUL_TIMEOUT_SECONDS", 30)

# Periodically recycle workers to cap slow memory growth from Python extension
# modules or rare reference leaks. Jitter avoids all workers restarting together.
max_requests = _int_env("GUNICORN_MAX_REQUESTS", 10_000)
max_requests_jitter = _int_env("GUNICORN_MAX_REQUESTS_JITTER", 1_000)

# Container-friendly logging.
accesslog = os.getenv("GUNICORN_ACCESS_LOG", "-")
errorlog = os.getenv("GUNICORN_ERROR_LOG", "-")
loglevel = os.getenv("LOG_LEVEL", "info").lower()

# Avoid temporary heartbeat files on slow overlay filesystems.
worker_tmp_dir = os.getenv("GUNICORN_WORKER_TMP_DIR", "/dev/shm")

# Trust proxy forwarding only from configured infrastructure. "*" is convenient
# behind a private sidecar/proxy but should not be used on a directly exposed app.
forwarded_allow_ips = os.getenv("GUNICORN_FORWARDED_ALLOW_IPS", "127.0.0.1")

# Let Kubernetes/containers send SIGTERM and wait for graceful_timeout.
preload_app = os.getenv("GUNICORN_PRELOAD_APP", "false").lower() == "true"
