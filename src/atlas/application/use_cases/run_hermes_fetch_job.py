from __future__ import annotations

import asyncio
import http.client
import inspect
import ipaddress
import socket
import ssl
import urllib.error
import urllib.parse
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from atlas.application.unit_of_work import UnitOfWork
from atlas.config import get_settings
from atlas.domain.entities import (
    HermesFetchedDocument,
    HermesFetchJob,
    HermesFetchResult,
    HermesSourceChange,
)
from atlas.domain.enums import HermesChangeType, HermesFetchJobStatus, HermesTargetStatus
from atlas.domain.services.hermes_content import (
    detect_content_type_from_bytes,
    extract_html_title,
    make_text_preview,
    sha256_bytes,
)

_MAX_CONTENT_BYTES = 10 * 1024 * 1024  # 10 MB
_FETCH_TIMEOUT = 20
_USER_AGENT = "Atlas-Hermes/0.1"
_MAX_RETRY_DELAY_SECONDS = 60 * 60
_DEFAULT_LEASE_SECONDS = 5 * 60
_MAX_REDIRECTS = 5

FetchResultTuple = tuple[str, int, str | None, bytes]
FetchFn = Callable[[str], FetchResultTuple | Awaitable[FetchResultTuple]]


class HermesPermanentFetchError(Exception):
    """Marker for fetch failures that should NOT be retried.

    A permanent failure is one whose cause is in the URL/target/response
    *content* rather than transient network behaviour: a blocked private IP,
    an unsupported scheme, an over-cap response.  Retrying such failures
    burns the attempt budget without any chance of success and delays the
    final FAILED state visible to operators.

    The job runner uses ``isinstance(exc, HermesPermanentFetchError)`` to
    decide whether to skip retry and move the job straight to FAILED, so any
    new permanent-failure subclass picks up that behaviour for free.
    """


class HermesFetchSecurityError(ValueError, HermesPermanentFetchError):
    """Raised when a Hermes URL is unsafe to fetch server-side."""


class HermesFetchTooLargeError(ValueError, HermesPermanentFetchError):
    """Raised when a Hermes response exceeds the configured in-memory cap."""


class HermesFetchUnsupportedSchemeError(ValueError, HermesPermanentFetchError):
    """Raised when a Hermes URL uses an unsupported scheme.

    Distinct from ``HermesFetchSecurityError`` so callers/tests can match the
    specific failure even though both are permanent and skip retry.
    """


def _now() -> datetime:
    return datetime.now(UTC)


_CGNAT_RANGE = ipaddress.ip_network("100.64.0.0/10")


def _is_blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(ip, ipaddress.IPv4Address) and ip in _CGNAT_RANGE:
        return True
    # ``is_global`` is the allow criterion: it rejects private, loopback,
    # link-local, documentation/reserved, multicast, unspecified, ULA and other
    # non-public ranges that individual boolean checks can miss.
    return ip.is_multicast or not ip.is_global


def _normalize_host_pattern(pattern: str) -> str:
    """Normalize a host or domain entry from the allowlist for matching.

    Strips whitespace, lowercases, and drops a leading ``.`` so callers can
    write ``.example.com`` or ``example.com`` interchangeably to mean
    "example.com and any subdomain of it".
    """
    return pattern.strip().lower().lstrip(".")


def _host_matches_allowlist(host: str, allowlist: tuple[str, ...]) -> bool:
    """Return True if ``host`` matches any entry in ``allowlist``.

    An entry matches ``host`` if it equals it exactly or is a parent domain
    of it (suffix match on a dot-bounded label).  This avoids false matches
    where ``evil-example.com`` would otherwise be accepted by an
    ``example.com`` entry.

    The empty allowlist returns False because callers only invoke this
    function after deciding an allowlist is in effect.
    """
    if not allowlist:
        return False
    h = host.strip().lower().rstrip(".")
    for raw in allowlist:
        entry = _normalize_host_pattern(raw)
        if not entry:
            continue
        if h == entry or h.endswith("." + entry):
            return True
    return False


def _load_allowed_hosts_from_settings() -> tuple[str, ...]:
    """Read HERMES_ALLOWED_HOSTS via the application Settings singleton.

    Using ``get_settings()`` ensures the value goes through the same
    pydantic parsing and validation as every other setting (comma-split,
    strip, dedup).  Reading ``os.environ`` directly bypasses that path and
    can return a value that diverges from what Settings validated at startup
    if the environment is mutated after the process starts.

    Returns an empty tuple when unset/empty, which means "no allowlist" and
    falls back to the IP-range deny-list.  When set, every fetch must
    target a host that matches an entry (defense in depth against DNS
    rebinding and accidental targeting of internal services).
    """
    return tuple(get_settings().hermes_allowed_hosts)


def _parse_and_validate_fetch_url(
    url: str,
    *,
    allowed_hosts: tuple[str, ...] | None = None,
) -> tuple[urllib.parse.ParseResult, str]:
    """Validate a fetch URL and return the parsed URL plus a pinned public IP.

    The returned IP is the one the client must connect to.  This closes the
    classic DNS-rebinding gap where validation resolves a public address but
    the HTTP client performs a second lookup and connects to a private address.
    """
    if allowed_hosts is None:
        allowed_hosts = _load_allowed_hosts_from_settings()

    parsed = urllib.parse.urlparse(url)
    if parsed.scheme.lower() not in {"http", "https"}:
        raise HermesFetchUnsupportedSchemeError(
            f"Hermes fetch only allows http/https URLs (got scheme={parsed.scheme!r})"
        )
    if not parsed.hostname:
        raise HermesFetchSecurityError("Hermes fetch URL must include a hostname")

    host = parsed.hostname

    if allowed_hosts and not _host_matches_allowlist(host, allowed_hosts):
        raise HermesFetchSecurityError(
            f"Hermes fetch target {host!r} is not on HERMES_ALLOWED_HOSTS allowlist"
        )

    port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)

    try:
        explicit_ip = ipaddress.ip_address(host)
    except ValueError:
        explicit_ip = None
    if explicit_ip is not None:
        if _is_blocked_ip(explicit_ip):
            raise HermesFetchSecurityError("Hermes fetch target resolves to a blocked network")
        return parsed, str(explicit_ip)

    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise HermesFetchSecurityError(f"Hermes fetch target could not be resolved: {exc}") from exc
    if not infos:
        raise HermesFetchSecurityError("Hermes fetch target did not resolve")

    chosen_ip: str | None = None
    for info in infos:
        sockaddr = info[4]
        if not sockaddr:
            continue
        ip = ipaddress.ip_address(sockaddr[0])
        if _is_blocked_ip(ip):
            raise HermesFetchSecurityError("Hermes fetch target resolves to a blocked network")
        chosen_ip = str(ip)

    if chosen_ip is None:
        raise HermesFetchSecurityError("Hermes fetch target did not resolve to a usable address")
    return parsed, chosen_ip


def _assert_public_fetch_url(
    url: str,
    *,
    allowed_hosts: tuple[str, ...] | None = None,
) -> None:
    """Validate that ``url`` is safe to fetch from a server-side crawler."""
    _parse_and_validate_fetch_url(url, allowed_hosts=allowed_hosts)


class _PinnedHTTPConnection(http.client.HTTPConnection):
    """HTTPConnection that connects to a validated IP while preserving Host."""

    def __init__(
        self,
        logical_host: str,
        connect_host: str,
        *,
        port: int,
        timeout: float,
    ) -> None:
        self._connect_host = connect_host
        super().__init__(logical_host, port=port, timeout=timeout)

    def connect(self) -> None:
        self.sock = socket.create_connection(
            (self._connect_host, self.port),
            self.timeout,
            self.source_address,  # type: ignore[attr-defined]  # stdlib HTTPConnection attr
        )


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPSConnection that pins the TCP peer but keeps original SNI/Host."""

    def __init__(
        self,
        logical_host: str,
        connect_host: str,
        *,
        port: int,
        timeout: float,
        context: ssl.SSLContext | None = None,
    ) -> None:
        self._connect_host = connect_host
        super().__init__(logical_host, port=port, timeout=timeout, context=context)

    def connect(self) -> None:
        sock = socket.create_connection(
            (self._connect_host, self.port),
            self.timeout,
            self.source_address,  # type: ignore[attr-defined]  # stdlib HTTPConnection attr
        )
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)  # type: ignore[attr-defined]  # stdlib HTTPSConnection attr


def _host_header(parsed: urllib.parse.ParseResult) -> str:
    host = parsed.hostname or ""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    default_port = 443 if parsed.scheme.lower() == "https" else 80
    if parsed.port and parsed.port != default_port:
        return f"{host}:{parsed.port}"
    return host


def _request_target(parsed: urllib.parse.ParseResult) -> str:
    path = parsed.path or "/"
    if parsed.params:
        path += f";{parsed.params}"
    if parsed.query:
        path += f"?{parsed.query}"
    return path


def _read_limited(resp) -> bytes:  # type: ignore[no-untyped-def]
    # All call sites use HTTPResponse-like objects in bytes mode.  Narrow at
    # the boundary instead of leaking ``Any`` into FetchResultTuple.
    body: bytes = resp.read(_MAX_CONTENT_BYTES + 1)
    if len(body) > _MAX_CONTENT_BYTES:
        raise HermesFetchTooLargeError(
            f"Hermes response exceeded {_MAX_CONTENT_BYTES} bytes and was not stored"
        )
    return body


def _fetch_once_pinned(
    url: str,
    *,
    allowed_hosts: tuple[str, ...],
) -> tuple[FetchResultTuple | None, str | None]:
    parsed, connect_ip = _parse_and_validate_fetch_url(url, allowed_hosts=allowed_hosts)
    port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
    conn_cls = _PinnedHTTPSConnection if parsed.scheme.lower() == "https" else _PinnedHTTPConnection

    conn = conn_cls(parsed.hostname or "", connect_ip, port=port, timeout=_FETCH_TIMEOUT)
    try:
        conn.request(
            "GET",
            _request_target(parsed),
            headers={
                "Host": _host_header(parsed),
                "User-Agent": _USER_AGENT,
                "Accept": "*/*",
            },
        )
        resp = conn.getresponse()
        status = int(resp.status)
        content_type_header = resp.getheader("Content-Type")
        if status in {301, 302, 303, 307, 308}:
            location = resp.getheader("Location")
            if not location:
                raise HermesFetchSecurityError("Hermes redirect response was missing Location")
            return None, urllib.parse.urljoin(url, location)
        if status >= 400:
            raise urllib.error.HTTPError(url, status, resp.reason, resp.headers, None)
        body = _read_limited(resp)
        content_location = resp.getheader("Content-Location")
        # RFC 9110 allows Content-Location to be relative.  Resolve it against
        # the requested URL before applying the same public-URL validation used
        # for redirects; otherwise valid responses such as
        # ``Content-Location: /documents/latest`` are incorrectly rejected as
        # scheme-less URLs.
        final_url = urllib.parse.urljoin(url, content_location) if content_location else url
        _assert_public_fetch_url(final_url, allowed_hosts=allowed_hosts)
        return (final_url, status, content_type_header, body), None
    finally:
        conn.close()


def _fetch_url(url: str) -> FetchResultTuple:
    """Fetch with the allowlist loaded lazily from Settings at call time.

    This is the fallback used when ``RunHermesFetchJob`` is constructed without
    an explicit ``allowed_hosts`` argument (e.g. in tests or one-off scripts).
    Production paths — both the CLI worker and the API ``run_job`` endpoint —
    pass an explicit allowlist captured at startup/request time via
    ``_make_fetch_url(allowed_hosts)``.  If you are adding a new call site,
    prefer passing ``allowed_hosts`` explicitly so the allowlist is stable
    across Settings reloads.
    """
    return _fetch_url_with_allowlist(url, allowed_hosts=_load_allowed_hosts_from_settings())


def _fetch_url_with_allowlist(url: str, *, allowed_hosts: tuple[str, ...]) -> FetchResultTuple:
    """Core fetch implementation with an explicit allowlist and pinned DNS result."""
    current_url = url
    for _ in range(_MAX_REDIRECTS + 1):
        result, redirect_url = _fetch_once_pinned(current_url, allowed_hosts=allowed_hosts)
        if result is not None:
            return result
        if redirect_url is None:
            raise HermesFetchSecurityError("Hermes redirect did not provide a next URL")
        current_url = redirect_url
    raise HermesFetchSecurityError(f"Hermes fetch exceeded {_MAX_REDIRECTS} redirects")


def _make_fetch_url(allowed_hosts: tuple[str, ...] | None) -> FetchFn:
    """Return a fetch function that uses the given allowlist.

    When ``allowed_hosts`` is ``None``, the function falls back to reading
    ``hermes_allowed_hosts`` from the Settings singleton at call time
    (backward-compatible with the pre-Settings path used in tests and ad-hoc
    calls).  When an explicit tuple is supplied (from
    ``Settings.hermes_allowed_hosts``), it is captured once at construction
    time so the fetcher is stable across Settings reloads.
    """
    if allowed_hosts is None:
        return _fetch_url  # reads env lazily at call time

    def _fetch(url: str) -> FetchResultTuple:
        return _fetch_url_with_allowlist(url, allowed_hosts=allowed_hosts)

    return _fetch


def _retry_delay(attempt_count: int) -> timedelta:
    """Return bounded exponential backoff for the already-recorded attempt."""

    exponent = max(0, attempt_count - 1)
    seconds = min(60 * (2**exponent), _MAX_RETRY_DELAY_SECONDS)
    return timedelta(seconds=seconds)


def _make_worker_id(prefix: str) -> str:
    return f"{prefix}:{uuid4()}"


class RunHermesFetchJob:
    def __init__(
        self,
        uow: UnitOfWork,
        fetch_fn: FetchFn | None = None,
        *,
        worker_id_prefix: str = "hermes-run",
        lease_seconds: int = _DEFAULT_LEASE_SECONDS,
        allowed_hosts: tuple[str, ...] | None = None,
    ) -> None:
        self._uow = uow
        self._fetch_fn: FetchFn = fetch_fn or _make_fetch_url(allowed_hosts)
        self._worker_id_prefix = worker_id_prefix
        self._lease_seconds = lease_seconds

    async def _fetch(self, url: str) -> FetchResultTuple:
        """Fetch without blocking the event loop for synchronous clients."""

        # We accept both bare coroutine functions and callable-class instances
        # whose ``__call__`` is async.  ``iscoroutinefunction(x)`` doesn't pick
        # up the latter on its own; we have to introspect the class's
        # ``__call__`` because bound methods on instances mask the underlying
        # function for ``iscoroutinefunction``'s purposes.
        # Need the method object, not just a boolean, so B004 is intentional.
        cls_call = getattr(type(self._fetch_fn), "__call__", None)  # noqa: B004
        if inspect.iscoroutinefunction(self._fetch_fn) or inspect.iscoroutinefunction(cls_call):
            result = await self._fetch_fn(url)  # type: ignore[misc]
            final_url, status, content_type, body = result
            if len(body) > _MAX_CONTENT_BYTES:
                raise HermesFetchTooLargeError(
                    f"Hermes response exceeded {_MAX_CONTENT_BYTES} bytes and was not stored"
                )
            return final_url, status, content_type, body

        result = await asyncio.to_thread(self._fetch_fn, url)
        if inspect.isawaitable(result):
            result = await result
        final_url, status, content_type, body = result
        if len(body) > _MAX_CONTENT_BYTES:
            raise HermesFetchTooLargeError(
                f"Hermes response exceeded {_MAX_CONTENT_BYTES} bytes and was not stored"
            )
        return final_url, status, content_type, body

    def _lease_expires_at(self) -> datetime:
        return _now() + timedelta(seconds=self._lease_seconds)

    async def execute(self, job_id: UUID) -> HermesFetchResult:
        worker_id = _make_worker_id(self._worker_id_prefix)
        job = await self._uow.hermes_fetch_jobs.claim_running(
            job_id,
            worker_id=worker_id,
            lease_expires_at=self._lease_expires_at(),
        )
        if job is None:
            current = await self._uow.hermes_fetch_jobs.get(job_id)
            if current is None:
                raise ValueError(f"HermesFetchJob {job_id} not found")
            return HermesFetchResult(
                job_id=current.id,
                target_id=current.target_id,
                status=current.status,
                error_message=current.error_message,
            )
        return await self.execute_claimed(job)

    async def execute_claimed(self, job: HermesFetchJob) -> HermesFetchResult:
        if job.status != HermesFetchJobStatus.RUNNING or not job.locked_by:
            raise ValueError("execute_claimed requires a RUNNING Hermes job with a live owner")

        target = await self._uow.hermes_crawl_targets.get(job.target_id)
        if target is None or target.status != HermesTargetStatus.ACTIVE:
            job.status = HermesFetchJobStatus.FAILED
            job.finished_at = _now()
            job.error_message = "Target missing or not ACTIVE"
            job.scheduled_at = None
            job.locked_by = None
            job.locked_at = None
            job.lease_expires_at = None
            await self._uow.hermes_fetch_jobs.save(job)
            await self._uow.commit()
            return HermesFetchResult(
                job_id=job.id,
                target_id=job.target_id,
                status=HermesFetchJobStatus.FAILED,
                error_message=job.error_message,
            )

        # Release the claim transaction before external I/O.  Finalization below
        # re-locks and fences the claim by worker_id + attempt_count + live lease.
        await self._uow.commit()

        try:
            fetch_tuple = await self._fetch(target.normalized_url)
        except HermesPermanentFetchError as exc:
            # SSRF rejection, over-cap response, unsupported scheme, etc.
            # These are content/target-shape failures; retrying just burns the
            # attempt budget without any chance of success, so skip retry and
            # move the job straight to FAILED.
            return await self._finalize_failure(job, str(exc)[:1000], permanent=True)
        except Exception as exc:
            return await self._finalize_failure(job, str(exc)[:1000])

        return await self._finalize_success(job, fetch_tuple)

    async def _lock_for_finalization(self, job: HermesFetchJob) -> HermesFetchJob | None:
        if not job.locked_by:
            return None
        return await self._uow.hermes_fetch_jobs.lock_claim_for_finalization(
            job.id,
            worker_id=job.locked_by,
            attempt_count=job.attempt_count,
            now=_now(),
        )

    async def _lost_claim_result(self, job: HermesFetchJob) -> HermesFetchResult:
        current = await self._uow.hermes_fetch_jobs.get(job.id)
        if current is None:
            return HermesFetchResult(
                job_id=job.id,
                target_id=job.target_id,
                status=HermesFetchJobStatus.FAILED,
                error_message="Hermes job disappeared before finalization",
            )
        return HermesFetchResult(
            job_id=current.id,
            target_id=current.target_id,
            status=current.status,
            error_message=current.error_message or "Hermes claim lease lost before finalization",
        )

    async def _finalize_success(
        self,
        claimed_job: HermesFetchJob,
        fetch_tuple: FetchResultTuple,
    ) -> HermesFetchResult:
        job = await self._lock_for_finalization(claimed_job)
        if job is None:
            return await self._lost_claim_result(claimed_job)

        target = await self._uow.hermes_crawl_targets.get(job.target_id)
        if target is None or target.status != HermesTargetStatus.ACTIVE:
            return await self._finalize_failure(job, "Target missing or not ACTIVE")

        final_url, http_status, ct_header, body = fetch_tuple
        document_id: UUID | None = None
        change_id: UUID | None = None
        change_type: HermesChangeType | None = None
        content_sha256: str | None = None

        content_type = detect_content_type_from_bytes(body, ct_header, target.normalized_url)
        content_sha256 = sha256_bytes(body)
        preview = make_text_preview(body, content_type, max_chars=2000)
        title = extract_html_title(body) if content_type.value == "HTML" else None

        previous_doc = await self._uow.hermes_fetched_documents.get_latest_for_target(target.id)
        previous_sha = target.last_content_sha256

        existing_doc = await self._uow.hermes_fetched_documents.find_by_target_and_hash(
            target.id, content_sha256
        )

        if existing_doc is not None:
            document_id = existing_doc.id
            change_type = HermesChangeType.CONTENT_UNCHANGED
            change = HermesSourceChange(
                target_id=target.id,
                fetch_job_id=job.id,
                previous_document_id=existing_doc.id,
                new_document_id=existing_doc.id,
                change_type=change_type,
                previous_sha256=content_sha256,
                new_sha256=content_sha256,
                detected_at=_now(),
            )
        else:
            doc = HermesFetchedDocument(
                target_id=target.id,
                fetch_job_id=job.id,
                url=target.url,
                final_url=final_url,
                http_status=http_status,
                content_type=content_type,
                content_sha256=content_sha256,
                content_length=len(body),
                title=title,
                storage_path=None,
                raw_text_preview=preview,
                fetched_at=_now(),
            )
            await self._uow.hermes_fetched_documents.add(doc)
            document_id = doc.id

            if previous_doc is None and previous_sha is None:
                change_type = HermesChangeType.FIRST_SEEN
            else:
                change_type = HermesChangeType.CONTENT_CHANGED

            change = HermesSourceChange(
                target_id=target.id,
                fetch_job_id=job.id,
                previous_document_id=previous_doc.id if previous_doc else None,
                new_document_id=doc.id,
                change_type=change_type,
                previous_sha256=previous_sha,
                new_sha256=content_sha256,
                detected_at=_now(),
            )

        await self._uow.hermes_source_changes.add(change)
        change_id = change.id

        target.last_fetch_job_id = job.id
        target.last_fetched_document_id = document_id
        target.last_content_sha256 = content_sha256
        target.last_http_status = http_status
        target.last_fetched_at = _now()
        await self._uow.hermes_crawl_targets.save(target)

        job.status = HermesFetchJobStatus.SUCCEEDED
        job.finished_at = _now()
        job.error_message = None
        job.scheduled_at = None
        job.locked_by = None
        job.locked_at = None
        job.lease_expires_at = None
        await self._uow.hermes_fetch_jobs.save(job)
        await self._uow.commit()

        return HermesFetchResult(
            job_id=job.id,
            target_id=job.target_id,
            status=job.status,
            document_id=document_id,
            change_id=change_id,
            change_type=change_type,
            content_sha256=content_sha256,
            error_message=None,
        )

    async def _finalize_failure(
        self,
        claimed_job: HermesFetchJob,
        error_message: str,
        *,
        permanent: bool = False,
    ) -> HermesFetchResult:
        """Finalize a failed Hermes fetch.

        ``permanent=True`` short-circuits the retry policy: the job moves
        straight to FAILED regardless of remaining attempts.  This applies
        to ``HermesPermanentFetchError`` (SSRF rejection, oversize response,
        unsupported scheme, redirect-loop / missing-Location, host-allowlist
        rejection) where retrying provably cannot succeed.

        Transient failures (network timeout, connection reset, transient
        DNS, 5xx) still use the existing exponential-backoff requeue path.
        """
        job = await self._lock_for_finalization(claimed_job)
        if job is None:
            return await self._lost_claim_result(claimed_job)

        target = await self._uow.hermes_crawl_targets.get(job.target_id)
        now = _now()
        if permanent or job.attempt_count >= job.max_attempts:
            job.status = HermesFetchJobStatus.FAILED
            job.scheduled_at = None
        else:
            job.status = HermesFetchJobStatus.QUEUED
            job.scheduled_at = now + _retry_delay(job.attempt_count)
        job.error_message = error_message
        job.finished_at = now
        job.locked_by = None
        job.locked_at = None
        job.lease_expires_at = None
        await self._uow.hermes_fetch_jobs.save(job)

        change_id: UUID | None = None
        if target is not None:
            fail_change = HermesSourceChange(
                target_id=target.id,
                fetch_job_id=job.id,
                change_type=HermesChangeType.FETCH_FAILED,
                detected_at=now,
            )
            await self._uow.hermes_source_changes.add(fail_change)
            change_id = fail_change.id

        await self._uow.commit()

        return HermesFetchResult(
            job_id=job.id,
            target_id=job.target_id,
            status=job.status,
            change_id=change_id,
            change_type=HermesChangeType.FETCH_FAILED,
            error_message=error_message,
        )
