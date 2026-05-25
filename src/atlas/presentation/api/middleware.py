from __future__ import annotations

import time
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable
from typing import Any

import orjson

ASGIApp = Callable[
    [
        dict[str, Any],
        Callable[[], Awaitable[dict[str, Any]]],
        Callable[[dict[str, Any]], Awaitable[None]],
    ],
    Awaitable[None],
]
Receive = Callable[[], Awaitable[dict[str, Any]]]
Send = Callable[[dict[str, Any]], Awaitable[None]]


class _RequestBodyTooLarge(Exception):
    pass


def _error_body(code: str, message: str) -> dict[str, Any]:
    """Emit the canonical API error envelope used by all Atlas error responses."""
    return {"error": {"code": code, "message": message, "details": {}}}


async def _send_json(send: Send, status_code: int, payload: dict[str, Any]) -> None:
    body = orjson.dumps(payload)
    await send(
        {
            "type": "http.response.start",
            "status": status_code,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


class SecurityHeadersMiddleware:
    """Attach cheap, defense-in-depth HTTP security headers.

    This is deliberately application-level rather than relying only on a proxy:
    a misconfigured Caddy/Nginx/ALB should not silently remove basic browser
    protections from API responses.  HSTS remains opt-in because it must only
    be sent once the service is actually reachable over HTTPS.
    """

    _BASE_HEADERS: tuple[tuple[bytes, bytes], ...] = (
        (b"x-content-type-options", b"nosniff"),
        (b"x-frame-options", b"DENY"),
        (b"referrer-policy", b"no-referrer"),
        # Strict CSP: this is a JSON API. For the OpenAPI docs endpoint (when
        # enabled), this blocks inline scripts and external resources, which is
        # acceptable — docs are only enabled in non-production environments.
        # Adding CSP to JSON responses is harmless and closes the attack surface
        # if a content-type sniffing bug ever causes a response to be
        # interpreted as HTML.
        (b"content-security-policy", b"default-src 'none'"),
        (
            b"permissions-policy",
            b"accelerometer=(), camera=(), geolocation=(), gyroscope=(), "
            b"magnetometer=(), microphone=(), payment=(), usb=()",
        ),
        (b"cross-origin-opener-policy", b"same-origin"),
        (b"cross-origin-resource-policy", b"same-site"),
    )

    def __init__(
        self,
        app: ASGIApp,
        *,
        hsts_enabled: bool = False,
        hsts_max_age_seconds: int = 31_536_000,
    ) -> None:
        self.app = app
        self.hsts_enabled = hsts_enabled
        self.hsts_value = f"max-age={hsts_max_age_seconds}; includeSubDomains".encode("ascii")

    async def __call__(self, scope: dict[str, Any], receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        path = str(scope.get("path", ""))
        scheme = str(scope.get("scheme", ""))
        headers_in = {key.lower(): value for key, value in scope.get("headers", [])}
        forwarded_proto = headers_in.get(b"x-forwarded-proto", b"").decode(
            "latin1", errors="ignore"
        )
        should_hsts = self.hsts_enabled and (scheme == "https" or forwarded_proto == "https")

        async def secure_send(message: dict[str, Any]) -> None:
            if message.get("type") == "http.response.start":
                response_headers = list(message.get("headers") or [])
                present = {key.lower() for key, _value in response_headers}
                for key, value in self._BASE_HEADERS:
                    if key not in present:
                        response_headers.append((key, value))
                if should_hsts and b"strict-transport-security" not in present:
                    response_headers.append((b"strict-transport-security", self.hsts_value))
                if (
                    path.startswith("/api/") or path == "/metrics"
                ) and b"cache-control" not in present:
                    response_headers.append((b"cache-control", b"no-store"))
                message["headers"] = response_headers
            await send(message)

        await self.app(scope, receive, secure_send)


class RequestBodySizeLimitMiddleware:
    """Reject oversized HTTP request bodies before Pydantic parses JSON.

    ``Content-Length`` is rejected immediately when present.  Requests without a
    trustworthy length are counted chunk-by-chunk and aborted before the body can
    be materialised into a Python object.
    """

    def __init__(self, app: ASGIApp, *, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: dict[str, Any], receive: Receive, send: Send) -> None:
        if scope.get("type") != "http" or self.max_bytes <= 0:
            await self.app(scope, receive, send)
            return

        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        raw_length = headers.get(b"content-length")
        if raw_length is not None:
            try:
                content_length = int(raw_length.decode("ascii"))
            except ValueError:
                content_length = self.max_bytes + 1
            if content_length > self.max_bytes:
                await _send_json(
                    send,
                    413,
                    _error_body(
                        "REQUEST_BODY_TOO_LARGE",
                        f"Request body exceeds {self.max_bytes} bytes",
                    ),
                )
                return

        bytes_seen = 0
        response_started = False

        async def limited_receive() -> dict[str, Any]:
            nonlocal bytes_seen
            message = await receive()
            if message.get("type") == "http.request":
                bytes_seen += len(message.get("body", b""))
                if bytes_seen > self.max_bytes:
                    raise _RequestBodyTooLarge
            return message

        async def tracking_send(message: dict[str, Any]) -> None:
            nonlocal response_started
            if message.get("type") == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracking_send)
        except _RequestBodyTooLarge:
            if response_started:
                return
            await _send_json(
                send,
                413,
                _error_body(
                    "REQUEST_BODY_TOO_LARGE",
                    f"Request body exceeds {self.max_bytes} bytes",
                ),
            )


class InMemoryRateLimitMiddleware:
    """Small in-process sliding-window rate limiter.

    Development / single-instance protection only.

    Limitations in production:
    * State is per-process; multi-instance deployments share no state.
    * The limiter is keyed off ``scope["client"][0]``. Behind a load balancer
      or reverse proxy, requests can appear to come from the proxy IP unless
      the deployment handles trusted forwarding headers elsewhere.
    * It does not support trusted ``X-Forwarded-For`` parsing.

    For production, replace this with a Redis-backed limiter or delegate rate
    limiting to the API gateway/reverse proxy. Set ``RATE_LIMIT_REQUESTS=0`` to
    disable this middleware and rely entirely on infrastructure-level limiting.
    """

    def __init__(self, app: ASGIApp, *, requests: int, window_seconds: int) -> None:
        self.app = app
        self.requests = requests
        self.window_seconds = window_seconds
        # Asyncio-safety note: _hits and _last_sweep are mutated without a lock.
        # This is safe under CPython asyncio (single-threaded event loop, GIL
        # makes dict ops atomic).  Do not use from a thread pool without a lock.
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._last_sweep = 0.0

    async def __call__(self, scope: dict[str, Any], receive: Receive, send: Send) -> None:
        if scope.get("type") != "http" or self.requests <= 0 or self.window_seconds <= 0:
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if path in {"/health", "/ready", "/metrics"}:
            await self.app(scope, receive, send)
            return

        client = scope.get("client") or ("unknown", 0)
        client_ip = str(client[0])
        now = time.monotonic()
        cutoff = now - self.window_seconds

        self._sweep_expired(cutoff, now)

        hits = self._hits[client_ip]
        while hits and hits[0] < cutoff:
            hits.popleft()
        if not hits:
            self._hits.pop(client_ip, None)
            hits = self._hits[client_ip]
        if len(hits) >= self.requests:
            oldest_hit = hits[0]
            retry_after_seconds = max(1, int(oldest_hit + self.window_seconds - now) + 1)
            retry_after = str(retry_after_seconds)
            body = orjson.dumps(
                _error_body(
                    "RATE_LIMITED",
                    f"Too many requests. Retry after {retry_after} seconds.",
                )
            )
            await send(
                {
                    "type": "http.response.start",
                    "status": 429,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(body)).encode("ascii")),
                        (b"retry-after", retry_after.encode("ascii")),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body})
            return
        hits.append(now)

        await self.app(scope, receive, send)

    def _sweep_expired(self, cutoff: float, now: float) -> None:
        """Periodically reclaim buckets for clients that stopped sending traffic."""
        if now - self._last_sweep < self.window_seconds:
            return
        self._last_sweep = now
        for client_ip, hits in list(self._hits.items()):
            while hits and hits[0] < cutoff:
                hits.popleft()
            if not hits:
                self._hits.pop(client_ip, None)
