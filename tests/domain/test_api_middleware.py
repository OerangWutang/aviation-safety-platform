from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

from atlas.presentation.api.middleware import (
    InMemoryRateLimitMiddleware,
    RequestBodySizeLimitMiddleware,
    SecurityHeadersMiddleware,
)

Send = Callable[[dict[str, Any]], Awaitable[None]]
Receive = Callable[[], Awaitable[dict[str, Any]]]


async def _ok_app(scope: dict[str, Any], receive: Receive, send: Send) -> None:
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok"})


async def _call(
    app: Callable[[dict[str, Any], Receive, Send], Awaitable[None]],
    *,
    path: str = "/limited",
    client_ip: str = "203.0.113.10",
    body: bytes = b"",
    headers: list[tuple[bytes, bytes]] | None = None,
) -> list[dict[str, Any]]:
    sent: list[dict[str, Any]] = []
    received = False

    async def receive() -> dict[str, Any]:
        nonlocal received
        if received:
            return {"type": "http.request", "body": b"", "more_body": False}
        received = True
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    await app(
        {
            "type": "http",
            "method": "GET",
            "path": path,
            "headers": headers or [],
            "client": (client_ip, 12345),
        },
        receive,
        send,
    )
    return sent


def _status(sent: list[dict[str, Any]]) -> int:
    return next(message["status"] for message in sent if message["type"] == "http.response.start")


async def test_rate_limiter_exempts_health_and_ready_paths() -> None:
    middleware = InMemoryRateLimitMiddleware(_ok_app, requests=1, window_seconds=60)

    assert _status(await _call(middleware, path="/health")) == 200
    assert _status(await _call(middleware, path="/health")) == 200
    assert _status(await _call(middleware, path="/ready")) == 200
    assert _status(await _call(middleware, path="/ready")) == 200


async def test_rate_limiter_still_limits_non_health_paths() -> None:
    middleware = InMemoryRateLimitMiddleware(_ok_app, requests=1, window_seconds=60)

    assert _status(await _call(middleware, path="/api/v1/conflicts")) == 200
    limited = await _call(middleware, path="/api/v1/conflicts")
    assert _status(limited) == 429
    start = next(message for message in limited if message["type"] == "http.response.start")
    headers = dict(start["headers"])
    assert headers[b"retry-after"] == b"60"

    body_msg = next(message for message in limited if message["type"] == "http.response.body")
    payload = json.loads(body_msg["body"])
    assert payload["error"]["code"] == "RATE_LIMITED"
    assert "message" in payload["error"]
    assert payload["error"]["details"] == {}


async def test_rate_limiter_sweeps_expired_client_buckets(monkeypatch) -> None:
    now = 1_000.0
    monkeypatch.setattr("atlas.presentation.api.middleware.time.monotonic", lambda: now)
    middleware = InMemoryRateLimitMiddleware(_ok_app, requests=10, window_seconds=60)

    assert _status(await _call(middleware, client_ip="203.0.113.1")) == 200
    assert _status(await _call(middleware, client_ip="203.0.113.2")) == 200
    assert set(middleware._hits) == {"203.0.113.1", "203.0.113.2"}

    now = 1_061.0
    assert _status(await _call(middleware, client_ip="203.0.113.3")) == 200

    assert set(middleware._hits) == {"203.0.113.3"}


async def test_body_limit_does_not_send_second_response_start_after_streaming_overflow() -> None:
    async def starts_then_reads(scope: dict[str, Any], receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await receive()

    middleware = RequestBodySizeLimitMiddleware(starts_then_reads, max_bytes=1)

    sent = await _call(middleware, body=b"too large")

    starts = [message for message in sent if message["type"] == "http.response.start"]
    assert starts == [{"type": "http.response.start", "status": 200, "headers": []}]


async def test_body_limit_rejects_with_envelope_shape() -> None:
    """REQUEST_BODY_TOO_LARGE response must use the standard error envelope."""
    middleware = RequestBodySizeLimitMiddleware(_ok_app, max_bytes=5)

    sent = await _call(
        middleware,
        body=b"this body is too large",
        headers=[(b"content-length", b"22")],
    )
    assert _status(sent) == 413

    body_msg = next(message for message in sent if message["type"] == "http.response.body")
    payload = json.loads(body_msg["body"])
    assert payload["error"]["code"] == "REQUEST_BODY_TOO_LARGE"
    assert "message" in payload["error"]
    assert payload["error"]["details"] == {}


async def test_security_headers_added_to_api_response() -> None:
    middleware = SecurityHeadersMiddleware(_ok_app)

    sent = await _call(middleware, path="/api/v1/accidents/123")
    start = next(message for message in sent if message["type"] == "http.response.start")
    headers = dict(start["headers"])

    assert headers[b"x-content-type-options"] == b"nosniff"
    assert headers[b"x-frame-options"] == b"DENY"
    assert headers[b"referrer-policy"] == b"no-referrer"
    assert b"permissions-policy" in headers
    assert headers[b"cross-origin-opener-policy"] == b"same-origin"
    assert headers[b"cross-origin-resource-policy"] == b"same-site"
    assert headers[b"cache-control"] == b"no-store"


async def test_security_headers_adds_hsts_only_for_https() -> None:
    middleware = SecurityHeadersMiddleware(_ok_app, hsts_enabled=True, hsts_max_age_seconds=123)

    http_sent = await _call(middleware, path="/api/v1/accidents/123")
    http_headers = dict(
        next(message for message in http_sent if message["type"] == "http.response.start")[
            "headers"
        ]
    )
    assert b"strict-transport-security" not in http_headers

    https_sent = await _call(
        middleware,
        path="/api/v1/accidents/123",
        headers=[(b"x-forwarded-proto", b"https")],
    )
    https_headers = dict(
        next(message for message in https_sent if message["type"] == "http.response.start")[
            "headers"
        ]
    )
    assert https_headers[b"strict-transport-security"] == b"max-age=123; includeSubDomains"
