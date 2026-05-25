from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import ClassVar

import pytest

from atlas.application.use_cases.create_hermes_crawl_target import (
    CreateHermesCrawlTarget,
    CreateHermesCrawlTargetInput,
)
from atlas.application.use_cases.enqueue_hermes_fetch_job import (
    EnqueueHermesFetchJob,
    EnqueueHermesFetchJobInput,
)
from atlas.application.use_cases.register_hermes_source import (
    RegisterHermesSource,
    RegisterHermesSourceInput,
)
from atlas.application.use_cases.run_hermes_fetch_job import RunHermesFetchJob
from atlas.domain.enums import (
    HermesChangeType,
    HermesDocumentContentType,
    HermesFetchJobStatus,
    HermesSourceType,
)
from atlas.domain.services.hermes_content import (
    detect_content_type,
    detect_content_type_from_bytes,
    make_text_preview,
)
from tests.domain._fake_uow import InMemoryUnitOfWork


def _fake_fetch(content: bytes, ct: str = "text/html", status: int = 200, raises=None):
    def _fn(url: str):
        if raises:
            raise raises
        return url, status, ct, content

    return _fn


async def _setup(uow):
    source = await RegisterHermesSource(uow).execute(
        RegisterHermesSourceInput(name="TestSource", source_type=HermesSourceType.NEWS)
    )
    target = await CreateHermesCrawlTarget(uow).execute(
        CreateHermesCrawlTargetInput(source_id=source.id, url="https://example.com/")
    )
    job = await EnqueueHermesFetchJob(uow).execute(EnqueueHermesFetchJobInput(target_id=target.id))
    return source, target, job


@pytest.mark.asyncio
async def test_run_queued_job_creates_document():
    uow = InMemoryUnitOfWork()
    _, _, job = await _setup(uow)
    uc = RunHermesFetchJob(uow, fetch_fn=_fake_fetch(b"<html><title>T</title></html>"))
    result = await uc.execute(job.id)
    assert result.status == HermesFetchJobStatus.SUCCEEDED
    assert result.document_id is not None


@pytest.mark.asyncio
async def test_first_fetch_creates_first_seen_change():
    uow = InMemoryUnitOfWork()
    _, _, job = await _setup(uow)
    uc = RunHermesFetchJob(uow, fetch_fn=_fake_fetch(b"<html>hello</html>"))
    result = await uc.execute(job.id)
    assert result.change_type == HermesChangeType.FIRST_SEEN


@pytest.mark.asyncio
async def test_second_fetch_same_content_is_unchanged():
    uow = InMemoryUnitOfWork()
    _, target, job1 = await _setup(uow)
    content = b"<html>same</html>"
    await RunHermesFetchJob(uow, fetch_fn=_fake_fetch(content)).execute(job1.id)
    job2 = await EnqueueHermesFetchJob(uow).execute(EnqueueHermesFetchJobInput(target_id=target.id))
    result = await RunHermesFetchJob(uow, fetch_fn=_fake_fetch(content)).execute(job2.id)
    assert result.change_type == HermesChangeType.CONTENT_UNCHANGED


@pytest.mark.asyncio
async def test_changed_content_creates_content_changed():
    uow = InMemoryUnitOfWork()
    _, target, job1 = await _setup(uow)
    await RunHermesFetchJob(uow, fetch_fn=_fake_fetch(b"<html>v1</html>")).execute(job1.id)
    job2 = await EnqueueHermesFetchJob(uow).execute(EnqueueHermesFetchJobInput(target_id=target.id))
    result = await RunHermesFetchJob(uow, fetch_fn=_fake_fetch(b"<html>v2</html>")).execute(job2.id)
    assert result.change_type == HermesChangeType.CONTENT_CHANGED


@pytest.mark.asyncio
async def test_failed_fetch_creates_fetch_failed_change():
    uow = InMemoryUnitOfWork()
    _, _, job = await _setup(uow)
    uc = RunHermesFetchJob(uow, fetch_fn=_fake_fetch(b"", raises=ConnectionError("timeout")))
    result = await uc.execute(job.id)
    assert result.change_type == HermesChangeType.FETCH_FAILED


@pytest.mark.asyncio
async def test_failed_fetch_requeues_if_attempts_remain():
    uow = InMemoryUnitOfWork()
    _, _, job = await _setup(uow)
    uc = RunHermesFetchJob(uow, fetch_fn=_fake_fetch(b"", raises=OSError("err")))
    result = await uc.execute(job.id)
    assert result.status == HermesFetchJobStatus.QUEUED


@pytest.mark.asyncio
async def test_failed_fetch_marks_failed_after_max_attempts():
    uow = InMemoryUnitOfWork()
    _, _, job = await _setup(uow)
    job.max_attempts = 1
    await uow.hermes_fetch_jobs.save(job)
    uc = RunHermesFetchJob(uow, fetch_fn=_fake_fetch(b"", raises=OSError("err")))
    result = await uc.execute(job.id)
    assert result.status == HermesFetchJobStatus.FAILED


def test_content_type_detection():
    assert detect_content_type("text/html") == HermesDocumentContentType.HTML
    assert detect_content_type("application/pdf") == HermesDocumentContentType.PDF
    assert detect_content_type("text/plain") == HermesDocumentContentType.TEXT
    assert detect_content_type("application/json") == HermesDocumentContentType.JSON
    assert detect_content_type("application/xml") == HermesDocumentContentType.XML
    assert detect_content_type(None, url="page.htm") == HermesDocumentContentType.HTML
    assert detect_content_type_from_bytes(b"\x00\x01\x02", None) == HermesDocumentContentType.BINARY


def test_text_preview_is_capped():
    content = b"A" * 5000
    preview = make_text_preview(content, HermesDocumentContentType.TEXT, max_chars=2000)
    assert preview is not None
    assert len(preview) <= 2000


@pytest.mark.asyncio
async def test_future_scheduled_job_is_not_run_early():
    uow = InMemoryUnitOfWork()
    _, target, _job1 = await _setup(uow)
    # Complete the setup job so a new future-scheduled job can be enqueued.
    await RunHermesFetchJob(uow, fetch_fn=_fake_fetch(b"<html>done</html>")).execute(_job1.id)
    future = datetime.now(UTC) + timedelta(hours=1)
    job = await EnqueueHermesFetchJob(uow).execute(
        EnqueueHermesFetchJobInput(target_id=target.id, scheduled_at=future)
    )

    result = await RunHermesFetchJob(uow, fetch_fn=_fake_fetch(b"<html>too early</html>")).execute(
        job.id
    )

    assert result.status == HermesFetchJobStatus.QUEUED
    assert job.attempt_count == 0


@pytest.mark.asyncio
async def test_failed_fetch_sets_retry_backoff_schedule():
    uow = InMemoryUnitOfWork()
    _, _, job = await _setup(uow)
    before = datetime.now(UTC)

    result = await RunHermesFetchJob(
        uow, fetch_fn=_fake_fetch(b"", raises=OSError("temporary"))
    ).execute(job.id)

    persisted = await uow.hermes_fetch_jobs.get(job.id)
    assert result.status == HermesFetchJobStatus.QUEUED
    assert persisted is not None
    assert persisted.scheduled_at is not None
    assert persisted.scheduled_at >= before + timedelta(seconds=50)


@pytest.mark.asyncio
async def test_claim_is_committed_before_fetch_is_called():
    uow = InMemoryUnitOfWork()
    _, _, job = await _setup(uow)
    commits_after_setup = uow.commits

    async def _assert_claim_committed(url: str):
        assert uow.commits == commits_after_setup + 1
        return url, 200, "text/html", b"<html>ok</html>"

    result = await RunHermesFetchJob(uow, fetch_fn=_assert_claim_committed).execute(job.id)

    assert result.status == HermesFetchJobStatus.SUCCEEDED


def test_hermes_fetch_blocks_localhost_url():
    from atlas.application.use_cases.run_hermes_fetch_job import (
        HermesFetchSecurityError,
        _assert_public_fetch_url,
    )

    with pytest.raises(HermesFetchSecurityError):
        _assert_public_fetch_url("http://127.0.0.1/admin")


def test_hermes_fetch_rejects_oversized_response():
    from io import BytesIO

    from atlas.application.use_cases.run_hermes_fetch_job import (
        _MAX_CONTENT_BYTES,
        HermesFetchTooLargeError,
        _read_limited,
    )

    with pytest.raises(HermesFetchTooLargeError):
        _read_limited(BytesIO(b"x" * (_MAX_CONTENT_BYTES + 1)))


@pytest.mark.asyncio
async def test_recover_stale_running_job_requeues_and_clears_lease():
    uow = InMemoryUnitOfWork()
    _, _, job = await _setup(uow)
    now = datetime.now(UTC)
    claimed = await uow.hermes_fetch_jobs.claim_running(
        job.id,
        worker_id="test-worker",
        lease_expires_at=now - timedelta(seconds=1),
    )
    assert claimed is not None

    outcomes = await uow.hermes_fetch_jobs.recover_stale_running(now=now, limit=10)

    persisted = await uow.hermes_fetch_jobs.get(job.id)
    assert len(outcomes) == 1
    assert outcomes[0].job_id == job.id
    assert outcomes[0].target_id == job.target_id
    # Default max_attempts > 1, so this is a transient requeue.
    assert outcomes[0].final_status == HermesFetchJobStatus.QUEUED
    assert persisted is not None
    assert persisted.status == HermesFetchJobStatus.QUEUED
    assert persisted.locked_by is None
    assert persisted.lease_expires_at is None
    assert persisted.scheduled_at == now


@pytest.mark.asyncio
async def test_recover_stale_running_returns_terminal_outcomes_for_exhausted_jobs():
    """When attempts are exhausted, recovery flips the job to FAILED.

    The returned ``HermesRecoveryOutcome`` carries ``final_status=FAILED``
    so callers (the Hermes worker) can emit FETCH_FAILED source-change
    audit rows for the target's change stream.
    """
    uow = InMemoryUnitOfWork()
    _, _, job = await _setup(uow)

    # Burn through every retry attempt the fixture allows so the next
    # recovery is a terminal failure rather than a requeue.  We do this by
    # directly mutating the in-memory job's attempt_count to its cap.
    persisted = await uow.hermes_fetch_jobs.get(job.id)
    assert persisted is not None
    persisted = persisted.model_copy(update={"attempt_count": persisted.max_attempts})
    await uow.hermes_fetch_jobs.save(persisted)
    await uow.commit()

    now = datetime.now(UTC)
    claimed = await uow.hermes_fetch_jobs.claim_running(
        job.id,
        worker_id="test-worker",
        lease_expires_at=now - timedelta(seconds=1),
    )
    assert claimed is not None

    outcomes = await uow.hermes_fetch_jobs.recover_stale_running(now=now, limit=10)
    assert len(outcomes) == 1
    assert outcomes[0].final_status == HermesFetchJobStatus.FAILED
    assert outcomes[0].target_id == job.target_id


@pytest.mark.asyncio
async def test_expired_lease_prevents_stale_worker_from_writing_document():
    uow = InMemoryUnitOfWork()
    _, _, job = await _setup(uow)
    result = await RunHermesFetchJob(
        uow,
        fetch_fn=_fake_fetch(b"<html>late</html>"),
        lease_seconds=-1,
    ).execute(job.id)

    assert result.status == HermesFetchJobStatus.RUNNING
    assert uow.store.hermes.documents == []


@pytest.mark.asyncio
async def test_claim_next_running_atomically_transitions_due_job():
    uow = InMemoryUnitOfWork()
    _, _, job = await _setup(uow)
    lease_expires_at = datetime.now(UTC) + timedelta(minutes=5)

    claimed = await uow.hermes_fetch_jobs.claim_next_running(
        worker_id="worker-1",
        lease_expires_at=lease_expires_at,
    )

    assert claimed is not None
    assert claimed.id == job.id
    assert claimed.status == HermesFetchJobStatus.RUNNING
    assert claimed.locked_by == "worker-1"
    assert claimed.lease_expires_at == lease_expires_at


@pytest.mark.asyncio
async def test_custom_fetch_oversized_body_fails_permanently_without_document():
    """Oversize responses are a permanent failure: skip retry, no document.

    Before r9 these were classified as transient and the job was requeued,
    which burned the attempt budget on a configuration/content problem that
    cannot succeed on retry.  As of r9 ``HermesFetchTooLargeError`` is a
    ``HermesPermanentFetchError`` and the job moves straight to FAILED.
    """
    from atlas.application.use_cases.run_hermes_fetch_job import _MAX_CONTENT_BYTES

    uow = InMemoryUnitOfWork()
    _, _, job = await _setup(uow)
    result = await RunHermesFetchJob(
        uow,
        fetch_fn=_fake_fetch(b"x" * (_MAX_CONTENT_BYTES + 1)),
    ).execute(job.id)

    assert result.status == HermesFetchJobStatus.FAILED
    assert result.change_type == HermesChangeType.FETCH_FAILED
    assert uow.store.hermes.documents == []
    persisted = await uow.hermes_fetch_jobs.get(job.id)
    assert persisted is not None
    assert persisted.status == HermesFetchJobStatus.FAILED
    assert persisted.scheduled_at is None  # no retry queued


# --- Permanent vs transient failure classification (r9) -----------------------


@pytest.mark.asyncio
async def test_security_error_fails_permanently_does_not_retry():
    """SSRF rejection is permanent: skip retry and mark FAILED immediately.

    The previous behaviour requeued the job and let it burn through every
    attempt, which is pure noise — a blocked private IP is still blocked
    on the next fetch.
    """
    from atlas.application.use_cases.run_hermes_fetch_job import HermesFetchSecurityError

    uow = InMemoryUnitOfWork()
    _, _, job = await _setup(uow)
    # max_attempts defaults > 1, so without permanent-failure classification
    # this test would observe a QUEUED requeue.
    result = await RunHermesFetchJob(
        uow,
        fetch_fn=_fake_fetch(b"", raises=HermesFetchSecurityError("blocked")),
    ).execute(job.id)

    assert result.status == HermesFetchJobStatus.FAILED
    persisted = await uow.hermes_fetch_jobs.get(job.id)
    assert persisted is not None
    assert persisted.status == HermesFetchJobStatus.FAILED
    assert persisted.scheduled_at is None
    # Source-change audit row is still emitted for the failure.
    assert result.change_type == HermesChangeType.FETCH_FAILED


@pytest.mark.asyncio
async def test_unsupported_scheme_fails_permanently():
    from atlas.application.use_cases.run_hermes_fetch_job import (
        HermesFetchUnsupportedSchemeError,
    )

    uow = InMemoryUnitOfWork()
    _, _, job = await _setup(uow)
    result = await RunHermesFetchJob(
        uow,
        fetch_fn=_fake_fetch(b"", raises=HermesFetchUnsupportedSchemeError("ftp not allowed")),
    ).execute(job.id)

    assert result.status == HermesFetchJobStatus.FAILED
    persisted = await uow.hermes_fetch_jobs.get(job.id)
    assert persisted is not None
    assert persisted.scheduled_at is None


@pytest.mark.asyncio
async def test_transient_failure_still_requeues_with_backoff():
    """A network-style error remains retryable (regression guard).

    The new permanent-failure path must not eat the transient retry path
    that the worker depends on for flaky upstreams.  This test asserts the
    requeue branch is still chosen for a plain Exception that does not
    inherit from ``HermesPermanentFetchError``.
    """
    uow = InMemoryUnitOfWork()
    _, _, job = await _setup(uow)
    result = await RunHermesFetchJob(
        uow,
        fetch_fn=_fake_fetch(b"", raises=ConnectionResetError("upstream blip")),
    ).execute(job.id)

    persisted = await uow.hermes_fetch_jobs.get(job.id)
    assert persisted is not None
    if persisted.max_attempts > 1:
        # Attempt budget remains: must requeue, not fail outright.
        assert result.status == HermesFetchJobStatus.QUEUED
        assert persisted.scheduled_at is not None
    else:
        # If the fixture only allows one attempt, the job is FAILED but
        # via the attempts-exhausted branch, not the permanent path.
        assert result.status == HermesFetchJobStatus.FAILED


def test_permanent_error_hierarchy_is_marker_friendly():
    """All three concrete permanent errors share the marker base class.

    This is the contract callers rely on:
    ``except HermesPermanentFetchError`` is enough to catch every shape of
    permanent fetch failure without enumerating them.
    """
    from atlas.application.use_cases.run_hermes_fetch_job import (
        HermesFetchSecurityError,
        HermesFetchTooLargeError,
        HermesFetchUnsupportedSchemeError,
        HermesPermanentFetchError,
    )

    assert issubclass(HermesFetchSecurityError, HermesPermanentFetchError)
    assert issubclass(HermesFetchTooLargeError, HermesPermanentFetchError)
    assert issubclass(HermesFetchUnsupportedSchemeError, HermesPermanentFetchError)


# --- HERMES_ALLOWED_HOSTS defense-in-depth (r9) ------------------------------


def test_allowlist_host_match_exact():
    from atlas.application.use_cases.run_hermes_fetch_job import _host_matches_allowlist

    assert _host_matches_allowlist("example.com", ("example.com",))


def test_allowlist_host_match_subdomain():
    """Entry ``example.com`` covers ``news.example.com`` (dot-bounded suffix)."""
    from atlas.application.use_cases.run_hermes_fetch_job import _host_matches_allowlist

    assert _host_matches_allowlist("news.example.com", ("example.com",))


def test_allowlist_host_match_leading_dot_form():
    """``.example.com`` is treated as equivalent to ``example.com``."""
    from atlas.application.use_cases.run_hermes_fetch_job import _host_matches_allowlist

    assert _host_matches_allowlist("www.example.com", (".example.com",))


def test_allowlist_host_reject_unrelated():
    from atlas.application.use_cases.run_hermes_fetch_job import _host_matches_allowlist

    assert not _host_matches_allowlist("other.com", ("example.com",))


def test_allowlist_host_reject_lookalike_suffix():
    """``evil-example.com`` must NOT match an ``example.com`` entry.

    A naive ``endswith`` check would accept this; the dot-bounded suffix
    rule rejects it.  This is the regression guard for that bug class.
    """
    from atlas.application.use_cases.run_hermes_fetch_job import _host_matches_allowlist

    assert not _host_matches_allowlist("evil-example.com", ("example.com",))


def test_allowlist_host_case_insensitive():
    from atlas.application.use_cases.run_hermes_fetch_job import _host_matches_allowlist

    assert _host_matches_allowlist("News.Example.Com", ("example.com",))
    assert _host_matches_allowlist("example.com", ("Example.COM",))


def test_allowlist_host_trailing_dot_normalized():
    """FQDNs presented with a trailing dot still match."""
    from atlas.application.use_cases.run_hermes_fetch_job import _host_matches_allowlist

    assert _host_matches_allowlist("example.com.", ("example.com",))


def test_allowlist_empty_returns_false():
    """An empty allowlist matches nothing; callers treat it as 'no allowlist'."""
    from atlas.application.use_cases.run_hermes_fetch_job import _host_matches_allowlist

    assert not _host_matches_allowlist("example.com", ())


def test_assert_public_fetch_url_rejects_off_allowlist_before_dns():
    """The allowlist check fires before DNS resolution.

    This is important: a rebinding/typo attack should be rejected on the
    *name* alone, without us ever issuing the DNS query that an attacker
    might be trying to control.
    """
    from atlas.application.use_cases.run_hermes_fetch_job import (
        HermesFetchSecurityError,
        _assert_public_fetch_url,
    )

    # Even a hostname that does not resolve must raise the allowlist
    # error rather than the DNS error, proving the allowlist check ran
    # first.
    with pytest.raises(HermesFetchSecurityError, match="allowlist"):
        _assert_public_fetch_url(
            "https://nope.invalid.example.test/",
            allowed_hosts=("example.com",),
        )


def test_assert_public_fetch_url_rejects_unsupported_scheme_before_allowlist():
    """Unsupported-scheme classification beats the allowlist check.

    Both return permanent errors, but the scheme check is the cheapest
    rejection and the most specific error to surface to the operator.
    """
    from atlas.application.use_cases.run_hermes_fetch_job import (
        HermesFetchUnsupportedSchemeError,
        _assert_public_fetch_url,
    )

    with pytest.raises(HermesFetchUnsupportedSchemeError):
        _assert_public_fetch_url(
            "ftp://example.com/secret",
            allowed_hosts=("example.com",),
        )


def test_hermes_blocks_non_global_ip_ranges():
    import ipaddress

    from atlas.application.use_cases.run_hermes_fetch_job import _is_blocked_ip

    assert _is_blocked_ip(ipaddress.ip_address("100.64.0.1"))  # CGNAT
    assert _is_blocked_ip(ipaddress.ip_address("192.0.2.1"))  # documentation
    assert _is_blocked_ip(ipaddress.ip_address("224.0.0.1"))  # multicast
    assert _is_blocked_ip(ipaddress.ip_address("fc00::1"))  # IPv6 ULA
    assert not _is_blocked_ip(ipaddress.ip_address("8.8.8.8"))


def test_fetch_url_resolves_relative_content_location(monkeypatch):
    import socket

    import atlas.application.use_cases.run_hermes_fetch_job as hermes

    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 80))
        ],
    )

    class _DummyResponse:
        status = 200
        reason = "OK"
        headers: ClassVar[dict] = {}

        def getheader(self, name: str):
            if name == "Content-Type":
                return "text/plain"
            if name == "Content-Location":
                return "/documents/latest"
            return None

        def read(self, _size: int) -> bytes:
            return b"ok"

    class _DummyConnection:
        def __init__(self, *_args, **_kwargs):
            return None

        def request(self, *_args, **_kwargs):
            return None

        def getresponse(self):
            return _DummyResponse()

        def close(self) -> None:
            return None

    monkeypatch.setattr(hermes, "_PinnedHTTPConnection", _DummyConnection)

    result = hermes._fetch_url_with_allowlist(
        "http://example.com/safety?page=1",
        allowed_hosts=("example.com",),
    )

    assert result == ("http://example.com/documents/latest", 200, "text/plain", b"ok")


def test_fetch_url_uses_validated_ip_for_connection(monkeypatch):
    import socket

    import atlas.application.use_cases.run_hermes_fetch_job as hermes

    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 80))
        ],
    )

    captured: dict[str, object] = {}

    class _DummyResponse:
        status = 200
        reason = "OK"
        headers: ClassVar[dict] = {}

        def getheader(self, name: str):
            if name == "Content-Type":
                return "text/plain"
            return None

        def read(self, _size: int) -> bytes:
            return b"ok"

    class _DummyConnection:
        def __init__(self, logical_host: str, connect_host: str, *, port: int, timeout: float):
            captured["logical_host"] = logical_host
            captured["connect_host"] = connect_host
            captured["port"] = port
            captured["timeout"] = timeout

        def request(self, method: str, target: str, headers: dict[str, str]):
            captured["method"] = method
            captured["target"] = target
            captured["host_header"] = headers["Host"]

        def getresponse(self):
            return _DummyResponse()

        def close(self) -> None:
            captured["closed"] = True

    monkeypatch.setattr(hermes, "_PinnedHTTPConnection", _DummyConnection)

    result = hermes._fetch_url_with_allowlist(
        "http://example.com/safety?page=1",
        allowed_hosts=("example.com",),
    )

    assert result == ("http://example.com/safety?page=1", 200, "text/plain", b"ok")
    assert captured["logical_host"] == "example.com"
    assert captured["connect_host"] == "93.184.216.34"
    assert captured["host_header"] == "example.com"
    assert captured["target"] == "/safety?page=1"
    assert captured["closed"] is True


# --- SSRF redirect-chain defence -----------------------------------------------


def test_redirect_to_private_ip_is_blocked(monkeypatch):
    """A redirect that resolves to a private IP must be blocked.

    The original URL resolves to a public IP (passes validation), but the
    redirect Location header points at an RFC-1918 address.  The redirect
    re-validates the new URL; the private-IP check must fire before the
    connection attempt.

    This guards against the attack pattern:
      1.  Attacker registers ``redirect.example.com`` on the allowlist.
      2.  The origin server (or a compromised CDN) responds 301 → ``http://192.168.1.1/``.
      3.  Without re-validation, Atlas would connect to the internal address.
    """
    import socket

    import atlas.application.use_cases.run_hermes_fetch_job as hermes

    dns_calls: list[str] = []

    def _fake_getaddrinfo(host, port, *args, **kwargs):
        dns_calls.append(host)
        if host == "redirect.example.com":
            # First lookup: resolves to a legitimate public IP.
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("203.0.113.10", 80))]
        # Second lookup (after redirect) resolves to an RFC-1918 address.
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.1", 80))]

    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo)

    class _DummyConnection:
        def __init__(self, *_a, **_kw):
            pass

        def request(self, *_a, **_kw):
            pass

        def getresponse(self):
            class _Resp:
                status = 301

                def getheader(self, name: str):
                    if name == "Location":
                        return "http://internal.corp/secret"
                    return None

                def read(self, _n: int) -> bytes:
                    return b""

            return _Resp()

        def close(self) -> None:
            pass

    monkeypatch.setattr(hermes, "_PinnedHTTPConnection", _DummyConnection)

    with pytest.raises(hermes.HermesFetchSecurityError):
        hermes._fetch_url_with_allowlist(
            "http://redirect.example.com/safe",
            allowed_hosts=("redirect.example.com", "internal.corp"),
        )

    # DNS was called at least for the initial URL, confirming we got past
    # the allowlist check and into the actual fetch path.
    assert any("redirect.example.com" in c for c in dns_calls)


def test_redirect_loop_is_capped_and_raises_security_error(monkeypatch):
    """Infinite redirect loops must not block the worker indefinitely.

    After ``_MAX_REDIRECTS + 1`` hops the fetch must raise
    ``HermesFetchSecurityError`` (a permanent failure that skips retry).
    """
    import socket

    import atlas.application.use_cases.run_hermes_fetch_job as hermes

    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_a, **_kw: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 80))],
    )

    class _LoopConn:
        def __init__(self, *_a, **_kw):
            pass

        def request(self, *_a, **_kw):
            pass

        def getresponse(self):
            class _Resp:
                status = 302

                def getheader(self, name: str):
                    return "http://loop.example.com/next" if name == "Location" else None

                def read(self, _n: int) -> bytes:
                    return b""

            return _Resp()

        def close(self) -> None:
            pass

    monkeypatch.setattr(hermes, "_PinnedHTTPConnection", _LoopConn)

    with pytest.raises(hermes.HermesFetchSecurityError, match="redirects"):
        hermes._fetch_url_with_allowlist(
            "http://loop.example.com/start",
            allowed_hosts=("loop.example.com",),
        )


# --- Direct IP literal SSRF tests -----------------------------------------------


def test_hermes_fetch_blocks_ipv6_loopback():
    """``http://[::1]/`` (IPv6 loopback) must be rejected without DNS resolution."""
    from atlas.application.use_cases.run_hermes_fetch_job import (
        HermesFetchSecurityError,
        _assert_public_fetch_url,
    )

    with pytest.raises(HermesFetchSecurityError):
        _assert_public_fetch_url("http://[::1]/internal")


def test_hermes_fetch_blocks_rfc1918_ip_literal():
    """``http://192.168.1.1/`` direct RFC-1918 IP must be rejected."""
    from atlas.application.use_cases.run_hermes_fetch_job import (
        HermesFetchSecurityError,
        _assert_public_fetch_url,
    )

    with pytest.raises(HermesFetchSecurityError):
        _assert_public_fetch_url("http://192.168.1.1/secret")


def test_hermes_fetch_blocks_cgnat_ip_literal():
    """``http://100.64.0.1/`` (CGNAT range) must be rejected."""
    from atlas.application.use_cases.run_hermes_fetch_job import (
        HermesFetchSecurityError,
        _assert_public_fetch_url,
    )

    with pytest.raises(HermesFetchSecurityError):
        _assert_public_fetch_url("http://100.64.0.1/")


def test_hermes_fetch_blocks_ipv4_mapped_ipv6_literal():
    """IPv4-mapped IPv6 literals (``::ffff:192.168.1.1``) must be blocked.

    Some HTTP clients expand ``::ffff:192.168.1.1`` to the private IPv4 address
    at connection time.  The validator must block the mapped address to prevent
    an attacker from tunneling a private-IP request through the IPv6 notation.
    """
    from atlas.application.use_cases.run_hermes_fetch_job import (
        HermesFetchSecurityError,
        _assert_public_fetch_url,
    )

    with pytest.raises(HermesFetchSecurityError):
        _assert_public_fetch_url("http://[::ffff:192.168.1.1]/secret")
