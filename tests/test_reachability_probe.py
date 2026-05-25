"""S1 probe-core / Story-2 (TES-150) — P1 reachability probe tests.

PRD §6.2 A4 enumerates six mock branches that the P1 probe MUST cover:

    1. DNS resolution failure
    2. TCP refused
    3. TLS handshake failure (no curl recovery)
    4. HTTP 200 root reachable
    5. HTTP 4xx root reachable but client error
    6. HTTP 5xx root reachable but server error

Plus the curl fallback path (TLS handshake failed under httpx, but
``curl -sk`` reaches the host) which MUST emit the
``reachability:via-curl`` signal so downstream slices can mark this
endpoint as "TLS-degraded but functional".

The tests inject DNS / httpx / curl seams via ``monkeypatch.setattr`` so
they run hermetically and never touch the network.
"""

from __future__ import annotations

import socket

import httpx
import pytest

from api_relay_audit.probe import reachability as p1
from api_relay_audit.probe.types import ProbeError, ReachabilityResult


# ---------------------------------------------------------------------------
# Test seam helpers
# ---------------------------------------------------------------------------


class _FakeHTTPResponse:
    """Minimal stand-in for the subset of ``httpx.Response`` the probe uses."""

    def __init__(self, status_code: int):
        self.status_code = status_code


def _patch_dns_ok(monkeypatch):
    monkeypatch.setattr(p1, "_resolve_host", lambda host: True)


def _patch_dns_fail(monkeypatch):
    def _raise(host):
        raise socket.gaierror(-2, "Name or service not known")

    monkeypatch.setattr(p1, "_resolve_host", _raise)


def _patch_httpx_returns(monkeypatch, status_code: int):
    def _fake_get(url, timeout):
        return _FakeHTTPResponse(status_code=status_code)

    monkeypatch.setattr(p1, "_httpx_get_root", _fake_get)


def _patch_httpx_raises(monkeypatch, exc: Exception):
    def _fake_get(url, timeout):
        raise exc

    monkeypatch.setattr(p1, "_httpx_get_root", _fake_get)


def _patch_curl_returns(monkeypatch, status_code: int):
    def _fake_curl(url, timeout):
        return _FakeHTTPResponse(status_code=status_code)

    monkeypatch.setattr(p1, "_curl_get_root", _fake_curl)


def _patch_curl_raises(monkeypatch, exc: Exception):
    def _fake_curl(url, timeout):
        raise exc

    monkeypatch.setattr(p1, "_curl_get_root", _fake_curl)


# ---------------------------------------------------------------------------
# Branch 1: DNS resolution failure
# ---------------------------------------------------------------------------


def test_p1_dns_failure_branch(monkeypatch):
    """DNS resolution fails → status='error', dns_resolves=False, error.code='dns_resolution_failed'."""
    _patch_dns_fail(monkeypatch)

    result = p1.probe_reachability("https://no-such-host.invalid/v1", timeout_s=2)

    assert isinstance(result, ReachabilityResult)
    assert result.status == "error"
    assert result.dns_resolves is False
    assert result.tcp_ok is False
    assert result.tls_ok is False
    assert result.http_status_root is None
    assert result.fallback_to_curl is False
    assert "reachability:dns-failed" in result.signals
    assert isinstance(result.error, ProbeError)
    assert result.error.code == "dns_resolution_failed"


# ---------------------------------------------------------------------------
# Branch 2: TCP refused
# ---------------------------------------------------------------------------


def test_p1_tcp_refused_branch(monkeypatch):
    """TCP connection refused → status='error', tcp_ok=False, error.code='tcp_refused'."""
    _patch_dns_ok(monkeypatch)
    _patch_httpx_raises(
        monkeypatch,
        httpx.ConnectError("Connection refused"),
    )

    result = p1.probe_reachability("https://localhost:1/v1", timeout_s=2)

    assert result.status == "error"
    assert result.dns_resolves is True
    assert result.tcp_ok is False
    assert result.tls_ok is False
    assert result.fallback_to_curl is False
    assert "reachability:tcp-refused" in result.signals
    assert result.error is not None
    assert result.error.code == "tcp_refused"


# ---------------------------------------------------------------------------
# Branch 3: TLS handshake failure WITHOUT curl recovery
# ---------------------------------------------------------------------------


def test_p1_tls_handshake_failed_branch_no_curl_recovery(monkeypatch):
    """httpx raises ConnectError-with-SSL and curl also fails → status='error',
    tcp_ok=True (we observed an SSL error, so socket reached), tls_ok=False,
    fallback_to_curl=True (we attempted), error.code='tls_handshake_failed'.
    """
    _patch_dns_ok(monkeypatch)
    _patch_httpx_raises(
        monkeypatch,
        httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED]"),
    )
    _patch_curl_raises(monkeypatch, RuntimeError("curl failed: SSL error"))

    result = p1.probe_reachability("https://expired.badssl.test/v1", timeout_s=2)

    assert result.status == "error"
    assert result.dns_resolves is True
    assert result.tcp_ok is True
    assert result.tls_ok is False
    assert result.fallback_to_curl is True
    assert "reachability:tls-failed" in result.signals
    assert result.error is not None
    assert result.error.code == "tls_handshake_failed"


# ---------------------------------------------------------------------------
# Curl fallback recovery — emits ``reachability:via-curl`` (DoD requirement)
# ---------------------------------------------------------------------------


def test_p1_curl_fallback_emits_via_curl_signal(monkeypatch):
    """httpx raises SSL error but curl -sk recovers → status='degraded',
    tls_ok=False, fallback_to_curl=True, signals contains
    'reachability:via-curl' (PRD §6.2 A4 + design §7 Story-2 DoD).
    """
    _patch_dns_ok(monkeypatch)
    _patch_httpx_raises(
        monkeypatch,
        httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED]"),
    )
    _patch_curl_returns(monkeypatch, status_code=200)

    result = p1.probe_reachability("https://self-signed.relay.example/v1", timeout_s=2)

    assert result.status == "degraded"
    assert result.dns_resolves is True
    assert result.tcp_ok is True
    assert result.tls_ok is False
    assert result.fallback_to_curl is True
    assert result.http_status_root == 200
    assert "reachability:via-curl" in result.signals
    assert result.error is None


# ---------------------------------------------------------------------------
# Branch 4: HTTP 200
# ---------------------------------------------------------------------------


def test_p1_http_200_branch(monkeypatch):
    """Clean reachability → status='ok', tcp_ok=True, tls_ok=True,
    http_status_root=200, no signals, no error.
    """
    _patch_dns_ok(monkeypatch)
    _patch_httpx_returns(monkeypatch, status_code=200)

    result = p1.probe_reachability("https://api.openai.com/v1", timeout_s=2)

    assert result.status == "ok"
    assert result.dns_resolves is True
    assert result.tcp_ok is True
    assert result.tls_ok is True
    assert result.http_status_root == 200
    assert result.fallback_to_curl is False
    assert result.signals == []
    assert result.error is None
    assert result.latency_ms is not None
    assert result.latency_ms >= 0


# ---------------------------------------------------------------------------
# Branch 5: HTTP 4xx
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("code", [401, 403, 404])
def test_p1_http_4xx_branch(monkeypatch, code):
    """4xx root response → status='degraded' (endpoint up but client error),
    tcp_ok=True, tls_ok=True, signals=['reachability:http-4xx'], no error
    (auth-sniff P2 will follow up on 401/403 specifics).
    """
    _patch_dns_ok(monkeypatch)
    _patch_httpx_returns(monkeypatch, status_code=code)

    result = p1.probe_reachability("https://relay.example/v1", timeout_s=2)

    assert result.status == "degraded"
    assert result.tcp_ok is True
    assert result.tls_ok is True
    assert result.http_status_root == code
    assert result.fallback_to_curl is False
    assert "reachability:http-4xx" in result.signals
    assert result.error is None


# ---------------------------------------------------------------------------
# Branch 6: HTTP 5xx
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("code", [500, 502, 503, 504])
def test_p1_http_5xx_branch(monkeypatch, code):
    """5xx root response → status='error', http_status_root=code,
    signals=['reachability:http-5xx'], error.code='http_5xx'.
    """
    _patch_dns_ok(monkeypatch)
    _patch_httpx_returns(monkeypatch, status_code=code)

    result = p1.probe_reachability("https://relay.example/v1", timeout_s=2)

    assert result.status == "error"
    assert result.tcp_ok is True
    assert result.tls_ok is True
    assert result.http_status_root == code
    assert result.fallback_to_curl is False
    assert "reachability:http-5xx" in result.signals
    assert result.error is not None
    assert result.error.code == "http_5xx"


# ---------------------------------------------------------------------------
# URL normalisation: trailing /v1 stripped + scheme-less rejected
# ---------------------------------------------------------------------------


def test_p1_strips_trailing_v1_path_for_root_probe(monkeypatch):
    """The probe must hit the URL root, not ``/v1`` — relays often
    return 404 at ``/v1`` but 200 at ``/`` (PRD §3.4 / R4).
    """
    captured = {}

    def _fake_get(url, timeout):
        captured["url"] = url
        return _FakeHTTPResponse(status_code=200)

    _patch_dns_ok(monkeypatch)
    monkeypatch.setattr(p1, "_httpx_get_root", _fake_get)

    p1.probe_reachability("https://relay.example/v1", timeout_s=2)

    assert "url" in captured
    assert not captured["url"].endswith("/v1"), captured["url"]
    assert captured["url"].rstrip("/") == "https://relay.example"


def test_p1_invalid_scheme_returns_error(monkeypatch):
    """A base_url without scheme is unusable — return error rather than
    raise so audit.py Step-0 can keep going.
    """
    result = p1.probe_reachability("relay.example/v1", timeout_s=2)

    assert result.status == "error"
    assert result.error is not None
    assert result.error.code == "invalid_base_url"


# ---------------------------------------------------------------------------
# Timeout is its own clear branch (TCP-level read timeout)
# ---------------------------------------------------------------------------


def test_p1_timeout_branch(monkeypatch):
    """``httpx.TimeoutException`` → status='error', error.code='timeout'.
    Signals include 'reachability:timeout'; tcp_ok stays False because
    we cannot prove the SYN-ACK landed.
    """
    _patch_dns_ok(monkeypatch)
    _patch_httpx_raises(monkeypatch, httpx.ConnectTimeout("timed out"))

    result = p1.probe_reachability("https://slow.relay.example/v1", timeout_s=1)

    assert result.status == "error"
    assert result.tcp_ok is False
    assert "reachability:timeout" in result.signals
    assert result.error is not None
    assert result.error.code == "timeout"


# ---------------------------------------------------------------------------
# Seam helper coverage — exercise the network-touching helpers directly
# ---------------------------------------------------------------------------


def test_resolve_host_localhost_resolves():
    """``_resolve_host`` succeeds on localhost (always resolvable)."""
    assert p1._resolve_host("localhost") is True


def test_root_url_rejects_non_http_scheme():
    """``_root_url`` returns None for ftp/file/etc. (only http(s) allowed)."""
    assert p1._root_url("ftp://relay.example/v1") is None
    assert p1._root_url("file:///tmp/relay") is None
    assert p1._root_url("relay.example") is None  # no scheme


def test_root_url_strips_path_and_normalises():
    """``_root_url`` returns scheme://host[:port], discarding path / query."""
    assert p1._root_url("https://relay.example/v1") == "https://relay.example"
    assert p1._root_url("http://relay.example:8080/v1/messages?x=1") == "http://relay.example:8080"


def test_curl_get_root_parses_status_code(monkeypatch):
    """``_curl_get_root`` returns object with .status_code parsed from %{http_code}."""

    class _CompletedProcess:
        returncode = 0
        stdout = "200"
        stderr = ""

    monkeypatch.setattr(p1.subprocess, "run", lambda *a, **kw: _CompletedProcess())
    response = p1._curl_get_root("https://relay.example", timeout=2.0)
    assert response.status_code == 200


def test_curl_get_root_nonzero_rc_raises_runtimeerror(monkeypatch):
    """Non-zero curl exit → RuntimeError surfaces stderr prefix."""

    class _CompletedProcess:
        returncode = 60  # SSL cert problem
        stdout = ""
        stderr = "curl: (60) SSL certificate problem: self-signed"

    monkeypatch.setattr(p1.subprocess, "run", lambda *a, **kw: _CompletedProcess())
    with pytest.raises(RuntimeError, match="curl failed"):
        p1._curl_get_root("https://relay.example", timeout=2.0)


def test_curl_get_root_garbled_stdout_raises_runtimeerror(monkeypatch):
    """Non-integer stdout → RuntimeError (defends against curl quirk)."""

    class _CompletedProcess:
        returncode = 0
        stdout = "not-a-number"
        stderr = ""

    monkeypatch.setattr(p1.subprocess, "run", lambda *a, **kw: _CompletedProcess())
    with pytest.raises(RuntimeError, match="non-integer"):
        p1._curl_get_root("https://relay.example", timeout=2.0)


def test_httpx_get_root_disables_redirect_follow(monkeypatch):
    """``_httpx_get_root`` MUST pass ``follow_redirects=False`` so a 301
    on the root does not silently masquerade as 200 (PRD §3.4 / R4).
    """
    captured = {}

    def _fake_httpx_get(url, timeout, follow_redirects):
        captured["follow_redirects"] = follow_redirects

        class _R:
            status_code = 200

        return _R()

    monkeypatch.setattr(p1.httpx, "get", _fake_httpx_get)
    p1._httpx_get_root("https://relay.example", timeout=2.0)
    assert captured["follow_redirects"] is False

