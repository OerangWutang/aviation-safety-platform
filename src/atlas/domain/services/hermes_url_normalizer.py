from __future__ import annotations

from urllib.parse import urlparse, urlunparse


def normalize_url(url: str) -> str:
    """Normalize a URL: lowercase scheme+host, strip fragment and default ports,
    strip trailing slash (unless path is '/'), preserve query string."""
    parsed = urlparse(url.strip())
    scheme = parsed.scheme.lower()
    if scheme not in ("http", "https"):
        raise ValueError(f"Only http/https URLs are allowed, got: {scheme!r}")

    if not parsed.hostname:
        raise ValueError("URL must include a hostname")

    host = parsed.hostname

    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Invalid URL port") from exc

    if (scheme == "http" and port == 80) or (scheme == "https" and port == 443):
        port = None

    netloc = host if port is None else f"{host}:{port}"

    path = parsed.path
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")
    if not path:
        path = "/"

    return urlunparse((scheme, netloc, path, parsed.params, parsed.query, ""))


def validate_url(url: str) -> str:
    """Validate and return normalized URL; raise ValueError on invalid input."""
    if not url or not url.strip():
        raise ValueError("URL must not be empty")
    return normalize_url(url)
